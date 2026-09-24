"""Signing in, through the real app (v0.7.4): the password, the code,
the session and its limits, signing out, one's own password."""

import logging
import time
import unittest
from unittest import mock

import service_fixture  # noqa: F401  (sets MOONLAN_CONFIG first)
from asgi_client import call
from moonlan import auth, server, signin

PASSWORD = "correct horse battery"


def cookie_of(answer):
    """The session cookie a response sets, as a Cookie header."""
    for header in answer.header("set-cookie"):
        if header.startswith(signin.SESSION_COOKIE + "="):
            return header.split(";", 1)[0]
    return None


class SignInCase(unittest.TestCase):
    def setUp(self):
        # every refused attempt is a WARNING line by design; not here
        logger = logging.getLogger("moonlan")
        self.addCleanup(logger.setLevel, logger.level)
        logger.setLevel(logging.ERROR)
        self.db = server.accounts
        self.clean()
        self.addCleanup(self.clean)
        server.sign_in._tickets.clear()
        # the admin that switches sign-in on
        self.add("anton", "admin", totp=True)

    def clean(self):
        with self.db._lock, self.db._conn:
            self.db._conn.execute("DELETE FROM sessions")
            self.db._conn.execute("DELETE FROM users")

    def add(self, name, role, totp=False, must_change=False):
        self.db.add_user(name, role, auth.hash_password(PASSWORD),
                         must_change=must_change)
        if totp:
            secret = auth.new_totp_secret()
            self.db.enable_totp(name, secret, "")
            return secret
        return None

    def secret(self, name):
        return self.db.user(name)["totp_secret"]

    def login(self, name, password=PASSWORD, **kwargs):
        return call(server.app, "POST", "/api/auth/login",
                    json_body={"name": name, "password": password}, **kwargs)

    def code(self, ticket, code):
        return call(server.app, "POST", "/api/auth/totp",
                    json_body={"ticket": ticket, "code": code})

    def code_now(self, name, at=None):
        return auth.totp_code(
            auth.secret_bytes(self.secret(name)), at or time.time()
        )

    def me(self, cookie):
        return call(server.app, "GET", "/api/auth/me", cookie=cookie)


class PasswordStepTest(SignInCase):
    def test_sign_in_and_out(self):
        self.add("vera", "user")
        answer = self.login("vera")
        self.assertEqual(answer.status, 200, answer.body)
        cookie = cookie_of(answer)
        self.assertIsNotNone(cookie)
        me = self.me(cookie).json()
        self.assertEqual((me["name"], me["role"], me["step"]),
                         ("vera", "user", None))
        self.assertEqual(
            call(server.app, "GET", "/api/topology", cookie=cookie).status, 200
        )
        out = call(server.app, "POST", "/api/auth/logout", cookie=cookie)
        self.assertEqual(out.status, 200)
        self.assertTrue(any("Max-Age=0" in h or "max-age=0" in h.lower()
                            for h in out.header("set-cookie")))
        self.assertEqual(self.me(cookie).status, 401)
        events = [e["event"] for e in server.db.journal(5)]
        self.assertIn("login", events)
        self.assertIn("logout", events)

    def test_the_cookie(self):
        self.add("vera", "user")
        header = next(h for h in self.login("vera").header("set-cookie")
                      if h.startswith(signin.SESSION_COOKIE))
        lower = header.lower()
        self.assertIn("httponly", lower)
        self.assertIn("samesite=strict", lower)
        self.assertIn("path=/", lower)
        self.assertIn(f"max-age={30 * 86400}", lower)
        # Secure arrives with HTTPS in v0.7.5; over http the browser
        # would drop a Secure cookie
        self.assertNotIn("secure", lower.replace("samesite", ""))

    def test_the_database_keeps_only_a_hash(self):
        self.add("vera", "user")
        token = cookie_of(self.login("vera")).split("=", 1)[1]
        with self.db._lock:
            rows = self.db._conn.execute(
                "SELECT token_hash FROM sessions"
            ).fetchall()
        self.assertEqual([r[0] for r in rows], [auth.token_hash(token)])
        self.assertNotIn(token, [r[0] for r in rows])

    def test_json_not_a_form(self):
        answer = call(
            server.app, "POST", "/api/auth/login",
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        self.assertNotEqual(answer.status, 200)

    def test_wrong_password_and_unknown_name_answer_alike(self):
        self.add("vera", "user")
        wrong = self.login("vera", "not the password")
        unknown = self.login("nobody-here", "not the password")
        self.assertEqual(wrong.status, 401)
        self.assertEqual((wrong.status, wrong.body), (unknown.status, unknown.body))
        self.assertEqual(wrong.json(), {"error": "invalid_credentials"})
        self.assertIsNone(cookie_of(wrong))

    def test_a_disabled_account(self):
        self.add("vera", "user")
        self.db.set_disabled("vera", True)
        # a wrong password learns nothing about the account
        self.assertEqual(self.login("vera", "wrong password").json(),
                         {"error": "invalid_credentials"})
        self.assertEqual(self.login("vera").json(), {"error": "disabled"})

    def test_not_while_sign_in_is_off(self):
        self.clean()
        self.add("vera", "user")
        self.assertEqual(self.login("vera").status, 409)

    def test_the_name_in_any_letter_case(self):
        self.add("Vera", "user")
        self.assertEqual(self.login("VERA").status, 200)

    def test_an_old_hash_is_redone_at_sign_in(self):
        with mock.patch.object(auth, "SCRYPT_N", 2 ** 10):
            self.db.add_user("vera", "user", auth.hash_password(PASSWORD))
        self.assertTrue(auth.needs_rehash(self.db.user("vera")["password"]))
        self.assertEqual(self.login("vera").status, 200)
        stored = self.db.user("vera")["password"]
        self.assertFalse(auth.needs_rehash(stored))
        self.assertTrue(auth.verify_password(PASSWORD, stored))


class CodeStepTest(SignInCase):
    def test_password_then_code(self):
        first = self.login("anton")
        self.assertEqual(first.status, 200)
        self.assertEqual(first.json()["status"], "code_required")
        self.assertIsNone(cookie_of(first), "no session before the code")
        answer = self.code(first.json()["ticket"], self.code_now("anton"))
        self.assertEqual(answer.status, 200, answer.body)
        cookie = cookie_of(answer)
        me = self.me(cookie).json()
        self.assertEqual((me["name"], me["role"], me["step"]),
                         ("anton", "admin", None))
        self.assertEqual(
            call(server.app, "DELETE", "/api/layout", cookie=cookie).status,
            200,
        )

    def test_a_code_works_once(self):
        code = self.code_now("anton")
        ticket = self.login("anton").json()["ticket"]
        self.assertEqual(self.code(ticket, code).status, 200)
        # the same code again, with a fresh ticket from the right password
        ticket = self.login("anton").json()["ticket"]
        again = self.code(ticket, code)
        self.assertEqual(again.status, 401)
        self.assertEqual(again.json(), {"error": "code_used"})
        # …and an earlier one within the window neither
        earlier = auth.totp_code(
            auth.secret_bytes(self.secret("anton")), time.time() - 30
        )
        self.assertEqual(self.code(ticket, earlier).json()["error"],
                         "code_used")

    def test_a_clock_half_a_minute_off(self):
        ticket = self.login("anton").json()["ticket"]
        late = self.code_now("anton", time.time() - 30)
        self.assertEqual(self.code(ticket, late).status, 200)

    def test_wrong_codes_use_up_the_ticket(self):
        ticket = self.login("anton").json()["ticket"]
        right = self.code_now("anton")
        wrong = "000000" if right != "000000" else "111111"
        for _ in range(signin.TICKET_TRIES):
            self.assertEqual(self.code(ticket, wrong).json(),
                             {"error": "invalid_code"})
        self.assertEqual(self.code(ticket, right).json(),
                         {"error": "ticket_expired"})

    def test_a_ticket_lives_five_minutes(self):
        ticket = self.login("anton").json()["ticket"]
        for entry in server.sign_in._tickets.values():
            entry.expires = time.time() - 1
        self.assertEqual(self.code(ticket, self.code_now("anton")).json(),
                         {"error": "ticket_expired"})

    def test_a_ticket_is_not_a_session(self):
        ticket = self.login("anton").json()["ticket"]
        cookie = f"{signin.SESSION_COOKIE}={ticket}"
        self.assertEqual(
            call(server.app, "GET", "/api/topology", cookie=cookie).status, 401
        )


class SessionLimitsTest(SignInCase):
    def session(self, name, created_ago, seen_ago):
        token = auth.new_token()
        now = time.time()
        self.db.add_session(auth.token_hash(token), self.db.user(name)["id"],
                            now - created_ago, second_factor=True)
        self.db.touch_session(auth.token_hash(token), now - seen_ago)
        return f"{signin.SESSION_COOKIE}={token}"

    def status(self, cookie):
        return call(server.app, "GET", "/api/topology", cookie=cookie).status

    def test_idle_hours(self):
        self.add("vera", "user")
        self.assertEqual(self.status(self.session("vera", 3600 * 13,
                                                  3600 * 11.5)), 200)
        self.assertEqual(self.status(self.session("vera", 3600 * 13,
                                                  3600 * 12.5)), 401)

    def test_a_wall_monitor_stays_for_weeks(self):
        # a viewer's page refreshing itself every thirty seconds
        self.add("wall", "viewer")
        cookie = self.session("wall", 86400 * 29, 90)
        self.assertEqual(self.status(cookie), 200)
        # and it counts as activity: last_seen moves (at most once a
        # minute — see signin.TOUCH_SECONDS)
        digest = auth.token_hash(cookie.split("=", 1)[1])
        self.assertGreater(self.db.session_row(digest)["last_seen"],
                           time.time() - 5)

    def test_thirty_days_whatever_happens(self):
        self.add("wall", "viewer")
        self.assertEqual(self.status(self.session("wall", 86400 * 30.5, 30)),
                         401)


class StepsTest(SignInCase):
    def test_a_new_password_before_anything_else(self):
        self.add("vera", "user", must_change=True)
        cookie = cookie_of(self.login("vera"))
        self.assertEqual(self.me(cookie).json()["step"], "password")
        refused = call(server.app, "GET", "/api/topology", cookie=cookie)
        self.assertEqual(refused.status, 403)
        self.assertEqual(refused.json(),
                         {"error": "step_required", "step": "password"})
        change = lambda current, new: call(  # noqa: E731
            server.app, "POST", "/api/auth/password", cookie=cookie,
            json_body={"current": current, "new": new},
        )
        self.assertEqual(change("wrong one", "a brand new password").json(),
                         {"error": "wrong_password"})
        self.assertEqual(change(PASSWORD, "short").json(),
                         {"error": "too_short"})
        self.assertEqual(change(PASSWORD, PASSWORD).json(),
                         {"error": "same_as_current"})
        self.assertEqual(change(PASSWORD, "a brand new password").status, 200)
        self.assertEqual(self.me(cookie).json()["step"], None)
        self.assertEqual(
            call(server.app, "GET", "/api/topology", cookie=cookie).status, 200
        )

    def test_changing_ones_password_closes_the_other_sessions(self):
        self.add("vera", "user")
        here = cookie_of(self.login("vera"))
        there = cookie_of(self.login("vera"))
        answer = call(server.app, "POST", "/api/auth/password", cookie=here,
                      json_body={"current": PASSWORD,
                                 "new": "a brand new password"})
        self.assertEqual(answer.json()["sessions_closed"], 1)
        self.assertEqual(self.me(here).status, 200)
        self.assertEqual(self.me(there).status, 401)

    def test_an_administrator_binds_a_second_factor_first(self):
        self.add("boris", "admin")
        cookie = cookie_of(self.login("boris"))
        self.assertEqual(self.me(cookie).json()["step"], "totp")
        for method, path in (("GET", "/api/topology"),
                             ("DELETE", "/api/layout"),
                             ("POST", "/api/auth/password")):
            answer = call(server.app, method, path, cookie=cookie)
            self.assertEqual(answer.json(),
                             {"error": "step_required", "step": "totp"}, path)
        self.assertEqual(
            call(server.app, "POST", "/api/auth/logout", cookie=cookie).status,
            200,
        )


if __name__ == "__main__":
    unittest.main()
