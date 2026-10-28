"""One's own notifications (v0.8.4): an email confirmed by a code, a
Telegram chat by a link to the bot, and who gets an alarm. The Bot API
is a stand-in here; nothing leaves the machine."""

import asyncio
import io
import json
import re
import time
import unittest
import urllib.error
from unittest import mock

import service_fixture  # noqa: F401  (sets MOONLAN_CONFIG first)
from asgi_client import call
from moonlan import auth, server, settings, signin, subscriptions
from moonlan.config import Config
from moonlan.db import Database
from moonlan.notify import Notifier, Recipient


class FakeBot:
    """getMe, getUpdates with its offset, sendMessage — and a 409"""

    def __init__(self):
        self.updates = []
        self.offsets = []
        self.sent = []
        self.conflict = False
        self.next_id = 1

    def press_start(self, key, chat=555, kind="private", date=None):
        self.updates.append({"update_id": self.next_id, "message": {
            "message_id": self.next_id, "date": int(date or time.time()),
            "chat": {"id": chat, "type": kind}, "text": "/start " + key}})
        self.next_id += 1

    def __call__(self, method, payload=None, timeout=10):
        payload = payload or {}
        if method == "getMe":
            return {"ok": True, "result": {"username": "moonlan_test_bot"}}
        if method == "sendMessage":
            self.sent.append(payload)
            return {"ok": True}
        if method == "getUpdates":
            self.offsets.append(payload["offset"])
            if self.conflict:
                raise urllib.error.HTTPError(
                    "https://api.telegram.org/bot…/getUpdates", 409, "Conflict", {},
                    io.BytesIO(b'{"ok":false,"description":"Conflict: terminated by '
                               b'other getUpdates request"}'))
            self.updates = [u for u in self.updates if u["update_id"] >= payload["offset"]]
            return {"ok": True, "result": list(self.updates)}
        raise AssertionError(method)


class Setup:
    """Accounts, a journal, a config with both channels set up, and a
    notifier whose email and Bot API are stand-ins"""

    def __init__(self, test):
        self.cfg = Config()
        self.cfg.notifications.email.enabled = True
        self.cfg.notifications.email.smtp_host = "mail.example.local"
        self.cfg.notifications.telegram.enabled = True
        self.cfg.notifications.telegram.bot_token = "1:TEST"
        self.accounts = Database(":memory:")
        self.journal = Database(":memory:")
        test.addCleanup(self.accounts.close)
        test.addCleanup(self.journal.close)
        self.notifier = Notifier(self.cfg)
        self.mail = []
        self.notifier.send_each = self.send_each
        self.bot = FakeBot()
        self.notifier.telegram_call = self.bot
        self.subs = subscriptions.Subscriptions(
            self.accounts, self.journal, self.cfg, self.notifier)
        self.notifier.personal = self.subs.recipients
        self.anna = self.accounts.add_user("anna", "user", "hash")["id"]
        self.olga = self.accounts.add_user("olga", "viewer", "hash")["id"]
        self.refuse = set()

    def send_each(self, channel, recipients, text, severity="info", subject=None):
        self.mail.append((channel, [r.address for r in recipients], text, subject))
        return [(r, OSError("refused")) for r in recipients if r.address in self.refuse]

    def code(self):
        """The code in the last email"""
        return re.search(r"\b(\d{6})\b", self.mail[-1][2]).group(1)

    def confirm_email(self, user_id, name, address):
        self.subs.send_code(user_id, name, address, "en", now=time.time() - 120)
        self.subs.confirm_code(user_id, name, self.code())


class EmailTest(unittest.TestCase):
    def setUp(self):
        self.s = Setup(self)

    def test_the_code_goes_to_the_address_in_its_language(self):
        answer = self.s.subs.send_code(self.s.anna, "anna", "Anna@Example.local", "ru")
        self.assertEqual(answer["status"], "sent")
        channel, to, body, subject = self.s.mail[-1]
        self.assertEqual((channel, to, subject), ("email", ["anna@example.local"],
                                                  "MoonLan: код подтверждения"))
        self.assertIn("Если вы этого не запрашивали — просто удалите письмо.", body)
        self.assertRegex(body, r"\b\d{6}\b")

    def test_only_a_hash_is_kept(self):
        self.s.subs.send_code(self.s.anna, "anna", "anna@example.local", "en")
        code = self.s.code()
        row = self.s.accounts.code_of(self.s.anna, "email")
        self.assertNotIn(code, row["secret"])
        self.assertTrue(subscriptions.code_matches(code, row["secret"]))
        self.assertFalse(subscriptions.code_matches("000000" if code != "000000" else "111111",
                                                    row["secret"]))

    def test_the_right_code_confirms(self):
        self.s.subs.send_code(self.s.anna, "anna", "anna@example.local", "en")
        state = self.s.subs.confirm_code(self.s.anna, "anna", self.s.code())
        self.assertEqual(state["subscriptions"]["email"]["address"], "anna@example.local")
        self.assertIsNone(state["pending"]["email"])
        self.assertEqual(self.s.journal.journal(5)[0]["event"], "subscription_added")

    def test_fifteen_minutes(self):
        sent = time.time()
        self.s.subs.send_code(self.s.anna, "anna", "anna@example.local", "en", now=sent)
        with self.assertRaises(subscriptions.Refused) as late:
            self.s.subs.confirm_code(self.s.anna, "anna", self.s.code(), now=sent + 15 * 60 + 1)
        self.assertEqual((late.exception.error, late.exception.status), ("code_expired", 410))
        self.assertIsNone(self.s.accounts.code_of(self.s.anna, "email"))

    def test_five_wrong_codes_burn_it(self):
        self.s.subs.send_code(self.s.anna, "anna", "anna@example.local", "en")
        right = self.s.code()
        wrong = "000000" if right != "000000" else "111111"
        for left in (4, 3, 2, 1):
            with self.assertRaises(subscriptions.Refused) as refused:
                self.s.subs.confirm_code(self.s.anna, "anna", wrong)
            self.assertEqual((refused.exception.error, refused.exception.extra),
                             ("wrong_code", {"left": left}))
        with self.assertRaises(subscriptions.Refused) as burned:
            self.s.subs.confirm_code(self.s.anna, "anna", wrong)
        self.assertEqual(burned.exception.error, "code_burned")
        with self.assertRaises(subscriptions.Refused) as gone:
            self.s.subs.confirm_code(self.s.anna, "anna", right)
        self.assertEqual(gone.exception.error, "no_code")

    def test_a_code_a_minute_five_an_hour(self):
        start = 1_800_000_000.0
        send = lambda at: self.s.subs.send_code(  # noqa: E731
            self.s.anna, "anna", "anna@example.local", "en", now=at)
        send(start)
        with self.assertRaises(subscriptions.Refused) as soon:
            send(start + 30)
        self.assertEqual((soon.exception.error, soon.exception.status,
                          soon.exception.extra), ("too_soon", 429, {"retry_at": start + 60}))
        for n in range(1, 5):
            send(start + 61 * n)
        with self.assertRaises(subscriptions.Refused) as many:
            send(start + 61 * 5 + 1)
        self.assertEqual((many.exception.error, many.exception.extra),
                         ("too_many_codes", {"retry_at": start + 3600}))
        # another account is not held up
        self.s.subs.send_code(self.s.olga, "olga", "olga@example.local", "en",
                              now=start + 61 * 5 + 1)

    def test_nothing_but_the_code_before_it_is_confirmed(self):
        self.s.subs.send_code(self.s.anna, "anna", "anna@example.local", "en")
        self.assertEqual(self.s.subs.recipients("email", "host_down"), [])
        self.assertNotIn("email", self.s.notifier.plan("host_down"))
        self.s.subs.confirm_code(self.s.anna, "anna", self.s.code())
        self.assertEqual([r.address for r in self.s.notifier.plan("host_down")["email"]],
                         ["anna@example.local"])

    def test_a_channel_off_takes_no_subscription(self):
        self.s.cfg.notifications.email.enabled = False
        with self.assertRaises(subscriptions.Refused) as off:
            self.s.subs.send_code(self.s.anna, "anna", "anna@example.local", "en")
        self.assertEqual((off.exception.error, off.exception.extra), ("channel_off", {"why": "off"}))
        self.assertEqual(self.s.mail, [])


class TelegramTest(unittest.TestCase):
    def setUp(self):
        self.s = Setup(self)

    def link(self, user_id=None, name="anna", lang="ru"):
        answer = self.s.subs.start_link(user_id or self.s.anna, name, lang)
        self.assertTrue(answer["link"].startswith("https://t.me/moonlan_test_bot?start="))
        key = answer["link"].split("start=")[1]
        self.assertRegex(key, r"^[A-Za-z0-9_-]{24}$")
        return key

    def update(self, key, kind="private", chat=555):
        self.s.bot.press_start(key, chat=chat, kind=kind)
        return self.s.subs.handle_update(self.s.bot.updates[-1], time.time() - 5)

    def test_start_from_a_private_chat_connects_it(self):
        key = self.link()
        self.assertEqual(self.s.subs.link_state(self.s.anna)["state"], "waiting")
        self.assertEqual(self.update(key, chat=777), "linked")
        sub = self.s.accounts.subscriptions_of(self.s.anna)["telegram"]
        self.assertEqual(sub["address"], "777")
        self.assertEqual(self.s.bot.sent[-1],
                         {"chat_id": 777, "text": "MoonLan: уведомления для anna подключены."})
        self.assertEqual(self.s.subs.link_state(self.s.anna), {"state": "connected"})
        # once
        self.assertIsNone(self.update(key, chat=888))

    def test_a_group_is_refused(self):
        key = self.link()
        self.assertEqual(self.update(key, kind="group", chat=-100), "group")
        self.assertNotIn("telegram", self.s.accounts.subscriptions_of(self.s.anna))
        self.assertEqual(self.s.subs.link_state(self.s.anna)["state"], "group")
        self.assertEqual(self.s.bot.sent[-1]["chat_id"], -100)
        # the private chat after it still works
        self.assertEqual(self.update(key), "linked")

    def test_a_key_not_given_and_one_out_of_date(self):
        self.link()
        self.assertIsNone(self.update("x" * 24))
        self.assertEqual(self.s.bot.sent, [])
        key = self.link(lang="en")
        with mock.patch("moonlan.subscriptions.time.time", return_value=time.time() + 601):
            self.assertEqual(self.update(key), "expired")
            self.assertEqual(self.s.subs.link_state(self.s.anna)["state"], "expired")
        self.assertIn("out of date", self.s.bot.sent[-1]["text"])
        self.assertNotIn("telegram", self.s.accounts.subscriptions_of(self.s.anna))

    def test_one_link_at_a_time(self):
        first = self.link()
        second = self.link()
        self.assertIsNone(self.update(first))
        self.assertEqual(self.update(second), "linked")

    def test_a_message_from_before_the_link_is_not_read(self):
        key = self.link()
        self.s.bot.press_start(key, date=time.time() - 3600)
        self.assertIsNone(self.s.subs.handle_update(self.s.bot.updates[-1], time.time()))

    def test_the_loop_reads_while_a_link_waits_and_then_stops(self):
        async def run():
            key = self.link()
            self.s.bot.next_id = 41
            self.s.bot.press_start(key, chat=999)
            self.s.subs.ensure_polling()
            self.assertTrue(self.s.subs.polling())
            await asyncio.wait_for(self.s.subs._task, 5)

        asyncio.run(run())
        self.assertFalse(self.s.subs.polling())
        self.assertEqual(self.s.accounts.subscriptions_of(self.s.anna)["telegram"]["address"], "999")
        # the update read is confirmed by the next offset
        self.assertEqual(self.s.bot.offsets, [0])
        self.assertEqual(self.s.subs.offset, 42)

    def test_the_offset_moves_on(self):
        async def run():
            self.link()
            self.s.bot.next_id = 7
            self.s.bot.press_start("y" * 24)       # not a key of ours
            original = self.s.bot.__call__

            def answer(method, payload=None, timeout=10):
                result = original(method, payload, timeout)
                if method == "getUpdates" and len(self.s.bot.offsets) == 2:
                    self.s.accounts.drop_code(self.s.anna, "telegram")
                return result

            self.s.notifier.telegram_call = answer
            await asyncio.wait_for(self.s.subs.poll(), 5)

        asyncio.run(run())
        self.assertEqual(self.s.bot.offsets[:2], [0, 8])

    def test_409_stops_it_and_is_said(self):
        async def run():
            self.link()
            self.s.bot.conflict = True
            with self.assertLogs("moonlan.subscriptions", "WARNING") as logs:
                await asyncio.wait_for(self.s.subs.poll(), 5)
            return logs

        logs = asyncio.run(run())
        state = self.s.subs.link_state(self.s.anna)
        self.assertEqual(state["state"], "conflict")
        self.assertIn("terminated by other getUpdates", state["conflict"])
        self.assertIn("409 Conflict", "\n".join(logs.output))


class DeliveryTest(unittest.TestCase):
    def setUp(self):
        self.s = Setup(self)

    def addresses(self, alarm_type, channel="email"):
        return [r.address for r in self.s.notifier.plan(alarm_type).get(channel, [])]

    def test_one_message_to_an_address_in_both(self):
        self.s.cfg.notifications.email.mail_to = ["anna@example.local", "noc@example.local"]
        self.s.confirm_email(self.s.anna, "anna", "ANNA@example.local")
        self.assertEqual(self.addresses("host_down"),
                         ["anna@example.local", "noc@example.local"])

    def test_null_follows_the_table_a_list_does_not(self):
        self.s.confirm_email(self.s.anna, "anna", "anna@example.local")
        self.s.confirm_email(self.s.olga, "olga", "olga@example.local")
        self.s.subs.change(self.s.olga, "email", {"alarm_types": ["port_util"]})
        self.assertEqual(self.addresses("host_down"), ["anna@example.local"])
        self.assertEqual(self.addresses("port_util"), ["olga@example.local"])
        # the administrator sends port_util to email and host_down no more
        settings.save(self.s.cfg, self.s.journal,
                      {"alarm_notify": {"port_util": ["email"], "host_down": ["syslog"]}}, "x")
        self.assertEqual(self.addresses("host_down"), [])
        self.assertEqual(self.addresses("port_util"),
                         ["anna@example.local", "olga@example.local"])
        # back to the table
        self.s.subs.change(self.s.olga, "email", {"alarm_types": None})
        self.assertEqual(self.s.subs.state(self.s.olga)["subscriptions"]["email"]["alarm_types"], None)

    def test_switched_off_and_disabled_get_nothing(self):
        self.s.confirm_email(self.s.anna, "anna", "anna@example.local")
        self.s.subs.change(self.s.anna, "email", {"enabled": False})
        self.assertEqual(self.addresses("host_down"), [])
        self.s.subs.change(self.s.anna, "email", {"enabled": True})
        self.s.accounts.set_disabled("anna", True)
        self.assertEqual(self.addresses("host_down"), [])

    def test_one_failure_does_not_stop_the_rest_and_no_address_is_logged(self):
        self.s.confirm_email(self.s.anna, "anna", "anna@example.local")
        self.s.confirm_email(self.s.olga, "olga", "olga@example.local")
        self.s.refuse = {"anna@example.local"}
        self.s.mail.clear()
        with self.assertLogs("moonlan.notify", "ERROR") as logs:
            asyncio.run(self.s.notifier.notify("host_down", "aa:bb", "critical", "down"))
        self.assertEqual(self.s.mail[0][1], ["anna@example.local", "olga@example.local"])
        text = "\n".join(logs.output)
        self.assertIn(f"user #{self.s.anna}", text)
        self.assertNotIn("anna@example.local", text)

    def test_telegram_sends_to_each_chat(self):
        notifier = Notifier(self.s.cfg)
        calls = []

        def bot(method, payload=None, timeout=10):
            calls.append(payload["chat_id"])
            if payload["chat_id"] == "1":
                raise OSError("blocked")
            return {"ok": True}

        notifier.telegram_call = bot
        failures = notifier.send_each("telegram", [Recipient("1", 5), Recipient("2", 6)], "x")
        self.assertEqual(calls, ["1", "2"])
        self.assertEqual([r.address for r, _ in failures], ["1"])

    def test_deleting_an_account_deletes_its_subscriptions(self):
        self.s.confirm_email(self.s.olga, "olga", "olga@example.local")
        self.s.accounts.delete_user("olga")
        self.assertEqual(self.s.accounts.active_subscriptions("email"), [])


class RightsTest(unittest.TestCase):
    """Through the service: one's own and nobody else's"""

    def setUp(self):
        self.db = server.accounts
        self.clean()
        self.addCleanup(self.clean)

    def clean(self):
        with self.db._lock, self.db._conn:
            for table in ("sessions", "users", "subscriptions", "subscription_codes"):
                self.db._conn.execute(f"DELETE FROM {table}")

    def account(self, name, role):
        row = self.db.add_user(name, role, auth.hash_password("x" * 12))
        token = auth.new_token()
        self.db.add_session(auth.token_hash(token), row["id"], time.time(),
                            second_factor=True)
        return row["id"], f"{signin.SESSION_COOKIE}={token}"

    def test_off_without_sign_in(self):
        answer = call(server.app, "GET", "/api/me/notifications")
        self.assertEqual((answer.status, answer.json()), (403, {"error": "sign_in_off"}))

    def test_ones_own_only(self):
        self.account("anton", "admin")
        anna, anna_cookie = self.account("anna", "viewer")
        olga, olga_cookie = self.account("olga", "user")
        self.db.confirm_subscription(olga, "email", "olga@example.local", time.time())
        mine = call(server.app, "GET", "/api/me/notifications", cookie=anna_cookie)
        self.assertEqual(mine.status, 200)
        self.assertIsNone(mine.json()["subscriptions"]["email"])
        self.assertNotIn("olga@example.local", mine.body.decode())
        # a name in the request changes nothing: the session decides
        call(server.app, "DELETE", "/api/me/notifications/email?user=olga", cookie=anna_cookie)
        self.assertIn("email", self.db.subscriptions_of(olga))
        theirs = call(server.app, "GET", "/api/me/notifications", cookie=olga_cookie)
        self.assertEqual(theirs.json()["subscriptions"]["email"]["address"], "olga@example.local")

    def test_the_console_is_nobody(self):
        self.account("anton", "admin")
        with mock.patch.object(server.sign_in, "_console", auth.token_hash("console-token")):
            header = {"x-moonlan-console": "console-token"}
            self.assertEqual(call(server.app, "GET", "/api/topology", headers=header).status, 200)
            self.assertEqual(call(server.app, "GET", "/api/me/notifications",
                                  headers=header).status, 403)

    def test_an_administrator_turns_somebodys_off(self):
        _, admin = self.account("anton", "admin")
        olga, olga_cookie = self.account("olga", "user")
        self.db.confirm_subscription(olga, "email", "olga@example.local", time.time())
        listed = call(server.app, "GET", "/api/users", cookie=admin).json()["users"]
        self.assertEqual(next(u for u in listed if u["name"] == "olga")["notifications"], ["email"])
        self.assertEqual(call(server.app, "DELETE", "/api/users/olga/notifications",
                              cookie=olga_cookie).status, 403)
        answer = call(server.app, "DELETE", "/api/users/olga/notifications", cookie=admin)
        self.assertEqual(answer.status, 200)
        self.assertEqual(self.db.subscriptions_of(olga), {})
        self.assertEqual(server.db.journal(1)[0]["event"], "user_notifications_removed")
        self.assertEqual(call(server.app, "DELETE", "/api/users/nobody/notifications",
                              cookie=admin).status, 404)

    def test_a_refusal_is_a_code(self):
        self.account("anton", "admin")
        _, cookie = self.account("anna", "viewer")
        answer = call(server.app, "POST", "/api/me/notifications/email", cookie=cookie,
                      json_body={"address": "anna@example.local", "lang": "en"})
        self.assertEqual(answer.status, 409)
        self.assertEqual(answer.json(), {"error": "channel_off", "why": "off"})
        answer = call(server.app, "PATCH", "/api/me/notifications/email", cookie=cookie,
                      json_body={"enabled": False})
        self.assertEqual((answer.status, answer.json()["error"]), (404, "no_subscription"))


if __name__ == "__main__":
    unittest.main()
