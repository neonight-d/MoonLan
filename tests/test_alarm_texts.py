"""Alarm texts that stay true while the alarm stands (v0.8.9).

`_raise` leaves an active alarm alone, so a text put together at the
raise went on saying it after the facts had moved: "missed 3 pings in a
row" while the count grew, "1 of 2 LAG members are up" after the second
went too, "14 day(s) left" a week later. v0.8.8 fixed switch_stale;
now every type is one of two kinds:

- (a) stable — written to stay true: the moment instead of a count, a
  measurement with the time it was made, a date. Never rewritten, so a
  later evaluation writes nothing — except once, to replace a text an
  older version left in the database;
- (b) restated — the facts change while it stands, and a later
  evaluation brings the text up to date in place: one row in alarms,
  one notification, the journal keeps the raise with its first words,
  and it is not a raise for flap damping.

Run with:  python -m unittest discover -s tests
"""

import logging
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import service_fixture  # noqa: E402,F401  (a config for server)

from moonlan import server  # noqa: E402
from moonlan.alarms import RESTATED_TYPES, STABLE_TYPES, SEVERITIES, AlarmEngine  # noqa: E402
from moonlan.config import Config  # noqa: E402
from moonlan.db import Database  # noqa: E402

SW = "10.0.0.21"
MAC = "aa:bb:cc:00:05:01"
LAG = f"{SW}:lag[Gi0/23+Gi0/24]"


class Notifier:
    def __init__(self):
        self.sent = []

    async def notify(self, alarm_type, subject, severity, message, **kwargs):
        self.sent.append((alarm_type, subject, message, kwargs.get("cleared", False)))

    def __getattr__(self, name):
        async def anything(*args, **kwargs):
            pass
        return anything


class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cfg = Config()
        self.cfg.notifications.flap_count = 3
        self.db = Database(":memory:")
        self.addCleanup(self.db.close)
        self.notifier = Notifier()
        self.engine = AlarmEngine(self.db, self.notifier, self.cfg.thresholds,
                                  self.cfg.notifications)
        self.updates = 0
        real = self.db.refresh_alarm_message

        def counted(*args):
            self.updates += 1
            return real(*args)
        self.db.refresh_alarm_message = counted

    def active(self, alarm_type):
        return [a for a in self.db.alarms(True, 50) if a["type"] == alarm_type]

    def raised(self, alarm_type):
        return [e for e in self.db.journal(200)
                if e["event"] == "alarm_raised" and alarm_type in e["details"]]

    def sent(self, alarm_type):
        return [n for n in self.notifier.sent if n[0] == alarm_type and not n[3]]

    def assert_restated(self, alarm_type, first_words, now_words):
        rows = self.active(alarm_type)
        self.assertEqual(len(rows), 1)
        self.assertIn(now_words, rows[0]["message"])
        self.assertEqual(len(self.sent(alarm_type)), 1)
        raised = self.raised(alarm_type)
        self.assertEqual(len(raised), 1)
        self.assertIn(first_words, raised[0]["details"])


class KindsTest(unittest.TestCase):
    def test_every_type_is_one_kind_or_left_alone_on_purpose(self):
        self.assertFalse(RESTATED_TYPES & set(STABLE_TYPES))
        # escalated in place (port_frame_corruption), never active
        # (new_mac), or not raised by the engine (passkey_clone_suspected)
        left = {"port_frame_corruption", "new_mac", "passkey_clone_suspected"}
        self.assertEqual(set(SEVERITIES) - RESTATED_TYPES - set(STABLE_TYPES), left)


class StableTest(Base):
    """(a): the text stays, and nothing is written as the count grows"""

    async def test_host_down_says_since_when(self):
        last_ok = time.mktime((2026, 11, 24, 9, 15, 0, 0, 0, -1))
        meta = {MAC: {"name": "nas.local", "monitored": 1, "last_ping_ok": last_ok}}
        with self.assertLogs("moonlan", logging.WARNING):
            for _ in range(3):
                await self.engine.on_ping({MAC: False}, meta)
        text = self.active("host_down")[0]["message"]
        self.assertEqual(text, "nas.local has not answered ping since 2026-11-24 09:15")
        for _ in range(5):
            await self.engine.on_ping({MAC: False}, meta)
        self.assertEqual(self.active("host_down")[0]["message"], text)
        self.assertEqual(self.updates, 0)

    async def test_switch_down_says_since_when(self):
        with self.assertLogs("moonlan", logging.WARNING):
            for _ in range(2):
                await self.engine.on_scan({SW: False}, {SW: "access-sw-1"})
        text = self.active("switch_down")[0]["message"]
        self.assertTrue(text.startswith("access-sw-1 has not answered SNMP since 20"), text)
        self.assertNotIn("in a row", text)
        for _ in range(4):
            await self.engine.on_scan({SW: False}, {SW: "access-sw-1"})
        self.assertEqual(self.active("switch_down")[0]["message"], text)
        self.assertEqual(self.updates, 0)

    async def test_a_measurement_has_its_time_and_threshold(self):
        def metrics(load):
            return [{"port": "Gi0/2", "speed_mbps": 1000, "in_mbps": load, "out_mbps": 1.0,
                     "in_errors_per_min": 0.0, "out_errors_per_min": 0.0,
                     "in_discards_per_min": 0.0, "out_discards_per_min": 0.0,
                     "error_ratio": None}]
        with self.assertLogs("moonlan", logging.WARNING):
            for _ in range(3):
                await self.engine.on_counters(SW, metrics(950.0))
        text = self.active("port_util")[0]["message"]
        self.assertRegex(text, r"^utilization 95% of 1000 Mbit/s at 20\d\d-\d\d-\d\d \d\d:\d\d "
                               r"\(threshold 90%\)$")
        for load in (990.0, 930.0, 970.0):
            await self.engine.on_counters(SW, metrics(load))
        self.assertEqual(self.active("port_util")[0]["message"], text)
        self.assertEqual(self.updates, 0)

    async def test_topology_changes_at_a_moment(self):
        def report(changes):
            return {"switches": [{"ip": SW, "name": "access-sw-1", "operating": True,
                                  "designated_root": "8000.aa", "top_changes": changes}],
                    "verdict": {"verdict": "single"}}
        await self.engine.on_stp(report(10))
        with self.assertLogs("moonlan", logging.WARNING):
            await self.engine.on_stp(report(20))
        text = self.active("stp_topology_change")[0]["message"]
        self.assertRegex(text, r"^access-sw-1 at 20.* 10 topology changes in one scan \(limit 3\)$")
        await self.engine.on_stp(report(40))
        self.assertEqual(self.active("stp_topology_change")[0]["message"], text)
        self.assertEqual(self.updates, 0)

    async def test_an_older_versions_text_is_replaced_once(self):
        raised = time.mktime((2026, 11, 20, 8, 0, 0, 0, 0, -1))
        self.db.raise_alarm("switch_down", SW, "critical",
                            "access-sw-1 missed 2 SNMP polls in a row", raised)
        await self.engine.load()
        await self.engine.on_scan({SW: False}, {SW: "access-sw-1"})
        await self.engine.on_scan({SW: False}, {SW: "access-sw-1"})
        text = self.active("switch_down")[0]["message"]
        # the start of it is the old raise: this process saw only its end
        self.assertEqual(text, "access-sw-1 has not answered SNMP since 2026-11-20 08:00")
        self.assertEqual(self.updates, 1)
        await self.engine.on_scan({SW: False}, {SW: "access-sw-1"})
        self.assertEqual(self.updates, 1)


class CertificateTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_date_not_a_count_of_days(self):
        ends = time.mktime((2026, 12, 3, 12, 0, 0, 0, 0, -1))
        calls = []

        async def service_alarm(alarm_type, subject, active, message):
            calls.append((alarm_type, active, message))
        with mock.patch.object(server.rt, "certificate", SimpleNamespace(not_after=ends)), \
                mock.patch.object(server.rt.alarm_engine, "service_alarm", service_alarm):
            await server.check_certificate_end()
        (alarm_type, active, message), = calls
        self.assertEqual((alarm_type, active), ("tls_cert_expiring", True))
        self.assertIn("expires on 2026-12-03", message)
        self.assertNotIn("day(s)", message)


class RestatedTest(Base):
    """(b): the facts move, the text follows, in place"""

    async def test_lag_one_of_two_then_none(self):
        with self.assertLogs("moonlan", logging.WARNING):
            for _ in range(3):
                await self.engine._lag_rule(LAG, 1, 2)
        await self.engine._lag_rule(LAG, 0, 2)
        self.assert_restated("lag_degraded", "1 of 2 LAG members are up",
                             "0 of 2 LAG members are up")
        self.assertEqual(self.updates, 1)
        # the same facts again write nothing
        await self.engine._lag_rule(LAG, 0, 2)
        self.assertEqual(self.updates, 1)

    async def test_part_of_a_port_comes_back(self):
        macs = [f"aa:bb:cc:00:06:0{n}" for n in range(1, 5)]
        meta = {mac: {"name": f"pc-{n}", "switch_ip": SW, "port": "Gi0/5"}
                for n, mac in enumerate(macs, 1)}
        for _ in range(2):
            await self.engine.on_ping({mac: True for mac in macs}, meta)
        with self.assertLogs("moonlan", logging.WARNING):
            await self.engine.on_ping({mac: False for mac in macs}, meta)
        first = self.active("port_hosts_down")[0]["message"]
        self.assertRegex(first, r"^4 devices went silent at once at 20.*; still silent: 4 of 4: "
                                r"pc-1, pc-2, pc-3, pc-4$")
        # one of four answers again: not half yet, still standing
        await self.engine.on_ping({macs[0]: True, **{m: False for m in macs[1:]}}, meta)
        self.assert_restated("port_hosts_down", "still silent: 4 of 4",
                             "still silent: 3 of 4: pc-2, pc-3, pc-4")
        self.assertTrue(self.active("port_hosts_down")[0]["message"].startswith(first[:40]))

    async def test_the_root_changes_again(self):
        def report(root):
            return {"switches": [{"ip": SW, "name": "access-sw-1", "operating": True,
                                  "designated_root": root, "top_changes": 1}],
                    "verdict": {"verdict": "single"}}
        await self.engine.on_stp(report("8000.p"))
        with self.assertLogs("moonlan", logging.WARNING):
            await self.engine.on_stp(report("8000.r"))
        await self.engine.on_stp(report("8000.r2"))
        self.assert_restated("stp_root_changed", "now follows root 8000.r (was 8000.p)",
                             "now follows root 8000.r2 (was 8000.p)")

    async def test_a_bridge_that_moves(self):
        def bridge(port):
            return [{"chassis_id": "18:fd:74:00:00:01", "switch": "access-sw-1", "port": port,
                     "name": "RouterOS-A", "mgmt_ip": "10.1.6.4"}]
        with self.assertLogs("moonlan", logging.WARNING):
            await self.engine.on_bridges(bridge("Gi0/14"), set())
        await self.engine.on_bridges(bridge("Gi0/9"), set())
        self.assert_restated("unmanaged_bridge_detected", "behind access-sw-1 port Gi0/14",
                             "behind access-sw-1 port Gi0/9")

    async def test_the_reason_of_loop_detection_disabled(self):
        data = SimpleNamespace(supported=True, enabled=False, ports={}, recover_time=0)
        with self.assertLogs("moonlan", logging.WARNING):
            await self.engine.on_loop_detection(SW, "access-sw-1", data, {}, stp_operating=None)
        await self.engine.on_loop_detection(SW, "access-sw-1", data, {}, stp_operating=False)
        self.assert_restated("loop_detection_disabled", "is not visible over SNMP",
                             "spanning tree is not running on this switch")

    async def test_flapping_counts_up(self):
        last = time.time()
        with self.assertLogs("moonlan", logging.WARNING):
            await self.engine.on_flaps(SW, [{"port": "Gi0/3", "flaps": 4, "last": last}])
        await self.engine.on_flaps(SW, [{"port": "Gi0/3", "flaps": 7, "last": last + 60}])
        self.assert_restated("port_flapping", "changed state 4 times",
                             "changed state 7 times")

    async def test_restating_is_not_raising(self):
        """Not a raise for flap damping, and nothing is cleared.
        lag_degraded is one of the flap types: three raises inside the
        window would mute it — its restatements do not count."""
        key = ("lag_degraded", LAG)
        with self.assertLogs("moonlan", logging.WARNING):
            for _ in range(3):
                await self.engine._lag_rule(LAG, 1, 2)
        for up in (0, 1, 0, 1, 0):
            await self.engine._lag_rule(LAG, up, 2)
        self.assertEqual(len(self.engine._raise_times[key]), 1)
        self.assertNotIn(key, self.engine.flapping_keys())
        self.assertIn(key, self.engine._active)
        self.assertEqual(len(self.active("lag_degraded")), 1)
        self.assertEqual(self.active("lag_degraded")[0]["message"], "0 of 2 LAG members are up")
        self.assertEqual(len(self.sent("lag_degraded")), 1)
        self.assertEqual(self.updates, 5)

    async def test_restated_after_a_restart(self):
        self.db.raise_alarm("lag_degraded", LAG, "warning",
                            "1 of 2 LAG members are up", time.time() - 3600)
        await self.engine.load()
        await self.engine._lag_rule(LAG, 1, 2)   # the same: nothing
        self.assertEqual(self.updates, 0)
        for _ in range(3):
            await self.engine._lag_rule(LAG, 0, 2)
        self.assertEqual(self.active("lag_degraded")[0]["message"], "0 of 2 LAG members are up")
        self.assertEqual(self.updates, 1)
        self.assertEqual(self.sent("lag_degraded"), [])


if __name__ == "__main__":
    unittest.main()
