"""Settings (v0.8.4): described once, moved from config.yaml at a start,
applied without a restart, changed through an API that checks
everything first and tells no secret."""

import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import service_fixture  # noqa: F401  (sets MOONLAN_CONFIG first)
from asgi_client import call
from moonlan import auth, server, settings, signin
from moonlan.alarms import AlarmEngine
from moonlan.config import Config, load_config
from moonlan.db import Database
from moonlan.notify import Notifier
from web_source import strings


class RegistryTest(unittest.TestCase):
    def test_every_key_once(self):
        keys = [s.key for s in settings.SETTINGS]
        self.assertEqual(len(keys), len(set(keys)))
        for key in keys:
            with self.subTest(key):
                settings.current(Config(), key)  # a field of Config

    def test_the_default_is_configs_own(self):
        # no number of its own: the default is read from Config
        self.assertNotIn("default", settings.Setting.__dataclass_fields__)
        for s in settings.SETTINGS:
            with self.subTest(s.key):
                self.assertEqual(s.default(), settings.current(Config(), s.key))
                if s.minimum is not None:
                    self.assertLessEqual(s.minimum, s.default())
                    self.assertLessEqual(s.default(), s.maximum)
        with mock.patch.object(Config, "__init__", autospec=True,
                               side_effect=lambda self: setattr(self, "unmanaged_threshold", 9)):
            self.assertEqual(settings.BY_KEY["unmanaged_threshold"].default(), 9)

    def test_words_in_both_languages(self):
        for lang in ("en", "ru"):
            words = strings(lang)
            for s in settings.SETTINGS:
                with self.subTest(lang=lang, key=s.key):
                    self.assertTrue(words.get(s.label_key))
                    self.assertTrue(words.get(s.hint_key))
                    self.assertTrue(words.get("applies_" + s.applies))
                    self.assertTrue(words.get("setSection_" + s.section))
                    if s.unit:
                        self.assertTrue(words.get("unit_" + s.unit))

    def test_the_loader_still_reads_them(self):
        # that is how they are moved: a key it no longer read would be
        # "unknown" and lost
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "config.yaml"
            empty.write_text("", encoding="utf-8")
            with mock.patch.dict(os.environ, {"MOONLAN_CONFIG": str(empty)}):
                read = {key for key, _, _ in load_config().report.values}
        self.assertEqual(settings.MOVED_PATHS - read, set())

    def test_what_stays_in_config_yaml(self):
        for key in ("listen.port", "db_path", "oui.path", "snmp.community",
                    "switches", "routers", "known_bridges", "uplink_ports",
                    "context_menu.allowed_schemes", "loop_detection.enabled",
                    "notifications.email.password", "notifications.email.smtp_host",
                    "notifications.email.enabled", "notifications.telegram.bot_token",
                    "notifications.syslog.host", "auth.session_idle_hours"):
            with self.subTest(key):
                self.assertNotIn(key, settings.MOVED_PATHS)


class Yaml:
    """A config.yaml of the test's own, and a database"""

    def __init__(self, test):
        self.tmp = tempfile.TemporaryDirectory()
        test.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "config.yaml"
        self.db = Database(Path(self.tmp.name) / "moonlan.db")
        test.addCleanup(self.db.close)

    def start(self, text):
        """What a start does: load config.yaml, then the settings"""
        self.path.write_text(text, encoding="utf-8")
        with mock.patch.dict(os.environ, {"MOONLAN_CONFIG": str(self.path)}):
            cfg = load_config()
        return cfg, settings.load(cfg, self.db)


class MigrationTest(unittest.TestCase):
    def setUp(self):
        self.yaml = Yaml(self)

    def test_in_neither(self):
        cfg, report = self.yaml.start("")
        self.assertEqual(cfg.unmanaged_threshold, 3)
        self.assertEqual((report.moved, report.unused, report.invalid), ([], [], []))
        self.assertEqual(self.yaml.db.settings(), {})

    def test_in_yaml_only_it_is_moved(self):
        cfg, report = self.yaml.start("unmanaged_threshold: 5\n")
        self.assertEqual(cfg.unmanaged_threshold, 5)
        self.assertEqual(report.moved, ["unmanaged_threshold"])
        self.assertEqual(report.unused, ["unmanaged_threshold"])
        row = self.yaml.db.settings()["unmanaged_threshold"]
        self.assertEqual((json.loads(row["value"]), row["updated_by"]), (5, "config.yaml"))
        event = self.yaml.db.journal(5)[0]
        self.assertEqual(event["event"], "settings_moved")
        self.assertEqual(json.loads(event["details"])["changes"],
                         [{"key": "unmanaged_threshold", "from": 3, "to": 5}])

    def test_in_both_the_database_wins(self):
        self.yaml.db.save_settings({"unmanaged_threshold": "7"}, time.time(), "anna")
        cfg, report = self.yaml.start("unmanaged_threshold: 5\n")
        self.assertEqual(cfg.unmanaged_threshold, 7)
        self.assertEqual(report.moved, [])
        self.assertEqual(report.unused, ["unmanaged_threshold"])

    def test_in_the_database_only(self):
        self.yaml.db.save_settings({"unmanaged_threshold": "7"}, time.time(), "anna")
        cfg, report = self.yaml.start("")
        self.assertEqual(cfg.unmanaged_threshold, 7)
        self.assertEqual((report.moved, report.unused), ([], []))

    def test_a_second_start_moves_nothing(self):
        self.yaml.start("offline_group_threshold: 4\nscan_interval_minutes: 0\n")
        cfg, report = self.yaml.start("offline_group_threshold: 4\nscan_interval_minutes: 0\n")
        self.assertEqual(report.moved, [])
        self.assertEqual(sorted(report.unused),
                         ["offline_group_threshold", "scan_interval_minutes"])
        self.assertEqual((cfg.offline_group_threshold, cfg.scan_interval_minutes), (4, 0))

    def test_out_of_range_is_not_moved(self):
        cfg, report = self.yaml.start("offline_group_threshold: 99999\n"
                                      "thresholds:\n  port_utilization_percent: 0\n")
        self.assertEqual(cfg.offline_group_threshold, 2)
        self.assertEqual(cfg.thresholds.port_utilization_percent, 90.0)
        self.assertEqual(sorted(k for k, _, _ in report.invalid),
                         ["offline_group_threshold", "thresholds.port_utilization_percent"])
        self.assertEqual(report.invalid[0][2]["code"], "range")
        self.assertEqual(self.yaml.db.settings(), {})

    def test_one_bad_alarm_type_keeps_its_default(self):
        cfg, report = self.yaml.start(
            "alarm_notify:\n  port_util: [syslog]\n  new_mac: [sms]\n")
        self.assertEqual(cfg.alarm_notify["port_util"], ["syslog"])
        self.assertEqual(cfg.alarm_notify["new_mac"], ["syslog"])
        self.assertEqual([k for k, _, _ in report.invalid], ["alarm_notify.new_mac"])
        self.assertEqual(report.moved, ["alarm_notify"])

    def test_read_only_for_diag(self):
        self.yaml.start("unmanaged_threshold: 5\n")
        cfg = Config()
        self.assertEqual(settings.overlay(cfg, settings.read_rows(self.yaml.db.path)), [])
        self.assertEqual(cfg.unmanaged_threshold, 5)
        self.assertEqual(settings.read_rows("/nonexistent/moonlan.db"), {})


class LiveTest(unittest.TestCase):
    def test_the_alarm_engine_and_the_notifier_see_a_change(self):
        cfg = Config()
        db = Database(":memory:")
        self.addCleanup(db.close)
        notifier = Notifier(cfg)
        engine = AlarmEngine(db, notifier, cfg.thresholds, cfg.notifications)
        cfg.notifications.email.enabled = True
        cfg.notifications.email.smtp_host = "mail.example.local"
        _, errors = settings.save(cfg, db, {
            "thresholds.mass_down_hosts": 7,
            "notifications.cooldown_seconds": 11,
            "alarm_notify": {"port_util": ["email"]},
            "notifications.email.mail_to": ["noc@example.local"],
        }, "anna")
        self.assertEqual(errors, {})
        self.assertEqual(engine._thresholds.mass_down_hosts, 7)
        self.assertEqual(engine._notif_cfg.cooldown_seconds, 11)
        self.assertEqual(notifier._cfg.cooldown_seconds, 11)
        self.assertEqual([r.address for r in notifier.plan("port_util")["email"]],
                         ["noc@example.local"])

    def test_a_new_interval_wakes_a_sleeping_loop(self):
        async def run():
            interval = [600.0]
            task = asyncio.create_task(server.sleep_interval(
                "scan_interval_minutes", lambda: interval[0]))
            await asyncio.sleep(0.05)
            self.assertFalse(task.done())
            interval[0] = 0.01  # due already, counted from the same start
            server.interval_changed["scan_interval_minutes"].set()
            await asyncio.wait_for(task, 1)
            # only on request: asleep until the interval changes
            interval[0] = None
            task = asyncio.create_task(server.sleep_interval(
                "scan_interval_minutes", lambda: interval[0]))
            await asyncio.sleep(0.05)
            self.assertFalse(task.done())
            interval[0] = 0.0
            server.interval_changed["scan_interval_minutes"].set()
            await asyncio.wait_for(task, 1)

        asyncio.run(run())


class Snapshot:
    """The service's settings as they were, put back after a test"""

    def __init__(self, test):
        self.rows = server.db.settings()
        self.values = {s.key: settings.current(server.config, s.key)
                       for s in settings.SETTINGS}
        test.addCleanup(self.restore)

    def restore(self):
        with server.db._lock, server.db._conn:
            server.db._conn.execute("DELETE FROM settings")
        for key, row in self.rows.items():
            server.db.save_settings({key: row["value"]}, row["updated_at"],
                                    row["updated_by"])
        for key, value in self.values.items():
            settings.apply(server.config, key, value)
        server.flap_tracker.window_seconds = (
            server.config.thresholds.flap_window_minutes * 60)


class ApiTest(unittest.TestCase):
    def setUp(self):
        Snapshot(self)
        self.accounts = server.accounts
        self.clean()
        self.addCleanup(self.clean)

    def clean(self):
        with self.accounts._lock, self.accounts._conn:
            self.accounts._conn.execute("DELETE FROM sessions")
            self.accounts._conn.execute("DELETE FROM users")

    def account(self, name, role):
        row = self.accounts.add_user(name, role, auth.hash_password("x" * 12))
        token = auth.new_token()
        self.accounts.add_session(auth.token_hash(token), row["id"], time.time(),
                                  second_factor=True)
        return f"{signin.SESSION_COOKIE}={token}"

    def put(self, body, cookie=None):
        return call(server.app, "PUT", "/api/settings", json_body=body, cookie=cookie)

    def test_no_secret_in_the_answer(self):
        email = server.config.notifications.email
        telegram = server.config.notifications.telegram
        with mock.patch.multiple(email, password="SMTP-PASSWORD-1", username="mailer-acct",
                                 smtp_host="mail.example.local", enabled=True), \
                mock.patch.multiple(telegram, bot_token="1:BOT-TOKEN-2", enabled=True):
            answer = call(server.app, "GET", "/api/settings")
        self.assertEqual(answer.status, 200)
        for secret in ("SMTP-PASSWORD-1", "mailer-acct", "mail.example.local", "BOT-TOKEN-2"):
            self.assertNotIn(secret, answer.body.decode())
        self.assertEqual(answer.json()["channels"]["email"], {"ready": True})
        self.assertEqual(answer.json()["channels"]["syslog"], {"ready": False, "why": "off"})

    def test_ranges(self):
        answer = self.put({"scan_interval_minutes": 5000, "monitored_by_default": 1,
                           "notifications.email.mail_to": ["anna@"]})
        self.assertEqual(answer.status, 400)
        self.assertEqual(answer.json()["errors"], {
            "scan_interval_minutes": {"code": "range", "min": 0, "max": 1440},
            "monitored_by_default": {"code": "type", "kind": "bool"},
            "notifications.email.mail_to": {"code": "email", "value": "anna@"},
        })

    def test_an_unknown_key_is_an_error(self):
        answer = self.put({"no_such_setting": 1})
        self.assertEqual(answer.status, 400)
        self.assertEqual(answer.json()["errors"], {"no_such_setting": {"code": "unknown"}})

    def test_all_or_nothing(self):
        before = server.config.unmanaged_threshold
        answer = self.put({"unmanaged_threshold": before + 1, "ping_interval_seconds": 1})
        self.assertEqual(answer.status, 400)
        self.assertEqual(server.config.unmanaged_threshold, before)
        self.assertNotIn("unmanaged_threshold", server.db.settings())

    def test_saved_applied_journalled(self):
        server.interval_changed["scan_interval_minutes"].clear()
        before = len(server.db.journal(10000))
        answer = self.put({"offline_group_threshold": 3, "scan_interval_minutes": 7,
                           "thresholds.flap_window_minutes": 20})
        self.assertEqual(answer.status, 200)
        self.assertEqual(server.config.offline_group_threshold, 3)
        self.assertTrue(server.interval_changed["scan_interval_minutes"].is_set())
        self.assertEqual(server.flap_tracker.window_seconds, 1200)
        event = server.db.journal(5)[0]
        self.assertEqual(event["event"], "settings_changed")
        changes = json.loads(event["details"])["changes"]
        self.assertIn({"key": "offline_group_threshold", "from": 2, "to": 3}, changes)
        self.assertEqual(len(changes), 3)
        # one entry for one save
        self.assertEqual(len(server.db.journal(10000)) - before, 1)

    def test_back_to_the_default(self):
        self.put({"offline_group_threshold": 5})
        answer = call(server.app, "DELETE", "/api/settings/offline_group_threshold")
        self.assertEqual(answer.status, 200)
        self.assertEqual(server.config.offline_group_threshold, 2)
        self.assertNotIn("offline_group_threshold", server.db.settings())
        self.assertEqual(call(server.app, "DELETE", "/api/settings/nope").status, 404)

    def test_roles(self):
        admin = self.account("anna", "admin")
        user = self.account("olga", "user")
        viewer = self.account("vera", "viewer")
        self.assertEqual(self.put({"offline_group_threshold": 3}, admin).status, 200)
        self.assertEqual(self.put({"offline_group_threshold": 4}, user).status, 403)
        self.assertEqual(call(server.app, "DELETE", "/api/settings/offline_group_threshold",
                              cookie=viewer).status, 403)
        seen = call(server.app, "GET", "/api/settings", cookie=viewer)
        self.assertEqual(seen.status, 200)
        self.assertFalse(seen.json()["can_edit"])
        self.assertTrue(call(server.app, "GET", "/api/settings", cookie=admin).json()["can_edit"])
        self.assertEqual(server.config.offline_group_threshold, 3)

    def test_people_are_not_shown_to_a_viewer(self):
        self.put({"notifications.email.mail_to": ["noc@example.local"]})
        admin = self.account("anna", "admin")
        viewer = self.account("vera", "viewer")

        def recipients(cookie):
            answer = call(server.app, "GET", "/api/settings", cookie=cookie).json()
            return next(e for e in answer["settings"]
                        if e["key"] == "notifications.email.mail_to")

        self.assertEqual(recipients(admin)["value"], ["noc@example.local"])
        hidden = recipients(viewer)
        self.assertEqual((hidden["value"], hidden["count"], hidden["hidden"]), (None, 1, True))
        answer = call(server.app, "GET", "/api/settings", cookie=viewer)
        self.assertNotIn("noc@example.local", answer.body.decode())

    def test_a_test_to_the_shared_recipients(self):
        self.assertEqual(call(server.app, "POST", "/api/settings/test/email").json(),
                         {"error": "channel_off", "why": "off"})
        email = server.config.notifications.email
        with mock.patch.multiple(email, enabled=True, smtp_host="mail.example.local"), \
                mock.patch.object(server.notifier, "send_each", return_value=[]) as sent:
            self.assertEqual(call(server.app, "POST", "/api/settings/test/email").json(),
                             {"error": "no_recipients"})
            self.put({"notifications.email.mail_to": ["a@example.local", "b@example.local"]})
            answer = call(server.app, "POST", "/api/settings/test/email")
        self.assertEqual(answer.json(), {"sent": 2, "failed": 0})
        self.assertEqual([r.address for r in sent.call_args.args[1]],
                         ["a@example.local", "b@example.local"])


if __name__ == "__main__":
    unittest.main()
