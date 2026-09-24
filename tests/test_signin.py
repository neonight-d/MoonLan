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
        server.sign_in.throttle = signin.Throttle()
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

    def code(self, ticket, code, **kwargs):
        return call(server.app, "POST", "/api/auth/totp",
                    json_body={"ticket": ticket, "code": code}, **kwargs)

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
        for n in range(signin.TICKET_TRIES):
            # from different addresses: this is about the ticket, not
            # about the delay per address
            self.assertEqual(self.code(ticket, wrong, client=f"10.0.1.{n}").json(),
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


class ThrottleTest(unittest.TestCase):
    def test_the_delay_doubles_after_five(self):
        throttle = signin.Throttle()
        delays = []
        for n in range(12):
            throttle.address_failed("10.0.0.5", 1000.0)
            delays.append(throttle.wait("10.0.0.5", 1000.0))
        self.assertEqual(delays, [0, 0, 0, 0, 1, 2, 4, 8, 16, 32, 60, 60])
        self.assertEqual(throttle.wait("10.0.0.6", 1000.0), 0)
        throttle.succeeded("10.0.0.5", "vera")
        self.assertEqual(throttle.wait("10.0.0.5", 1000.0), 0)

    def test_an_address_quiet_for_an_hour_starts_over(self):
        throttle = signin.Throttle()
        for _ in range(7):
            throttle.address_failed("10.0.0.5", 1000.0)
        throttle.address_failed("10.0.0.6", 1000.0 + 3700)
        self.assertEqual(throttle.failures("10.0.0.5"), 0)

    def test_ten_in_a_row_lock(self):
        throttle = signin.Throttle()
        self.assertEqual(
            [throttle.account_failed("vera") for _ in range(10)],
            [False] * 9 + [True],
        )
        # the count starts over after a lock
        self.assertFalse(throttle.account_failed("vera"))


class ProtectionTest(SignInCase):
    def test_the_sixth_attempt_waits(self):
        self.add("vera", "user")
        for _ in range(5):
            self.assertEqual(self.login("vera", "wrong password").status, 401)
        answer = self.login("vera")
        self.assertEqual(answer.status, 429)
        self.assertEqual(answer.json(),
                         {"error": "too_many_attempts", "retry_after": 1})
        self.assertEqual(answer.header("retry-after"), ["1"])
        # another address is not held up
        self.assertEqual(self.login("vera", client="10.0.0.98").status, 200)

    def test_a_wait_is_not_a_guess(self):
        # attempts refused for being too soon are not checked at all, so
        # the right password in the middle of a flood does not get in
        # either — and costs no scrypt
        self.add("vera", "user")
        for _ in range(5):
            self.login("vera", "wrong password")
        with mock.patch.object(auth, "verify_password") as verify:
            self.assertEqual(self.login("vera").status, 429)
        verify.assert_not_called()

    def lock_vera(self):
        # from ten addresses: the lock is about the account, whoever types
        for n in range(signin.ACCOUNT_LOCK_FAILURES):
            answer = self.login("vera", "wrong password", client=f"10.0.2.{n}")
        return answer

    def test_ten_wrong_passwords_lock_the_account(self):
        self.add("vera", "user")
        last = self.lock_vera()
        self.assertEqual(last.status, 423)
        self.assertEqual(last.json()["error"], "locked")
        self.assertAlmostEqual(last.json()["retry_after"], 900, delta=2)
        # the right password does not open it either
        self.assertEqual(self.login("vera", client="10.0.3.1").status, 423)
        self.assertGreater(self.db.user("vera")["locked_until"], time.time())
        event = server.db.journal(1)[0]
        self.assertEqual((event["event"], event["mac"]),
                         ("account_locked", "vera"))

    def test_unlock_from_the_console(self):
        self.add("vera", "user")
        self.lock_vera()
        import contextlib
        import io
        from moonlan import users
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(users.main(["unlock", "vera"]), 0)
        self.assertIn("unlocked", out.getvalue())
        self.assertEqual(self.login("vera", client="10.0.3.1").status, 200)

    def test_a_lock_ends_by_itself(self):
        self.add("vera", "user")
        self.lock_vera()
        self.db.lock(self.db.user("vera")["id"], time.time() - 1)
        self.assertEqual(self.login("vera", client="10.0.3.1").status, 200)

    def test_a_name_nobody_has_locks_the_same_way(self):
        # or "locked" would tell which names are real
        self.add("vera", "user")
        real = self.lock_vera()
        for n in range(signin.ACCOUNT_LOCK_FAILURES):
            phantom = self.login("nobody-here", "wrong password",
                                 client=f"10.0.4.{n}")
        self.assertEqual(real.status, phantom.status)
        self.assertEqual(real.json()["error"], phantom.json()["error"])
        self.assertEqual(
            self.login("nobody-here", "x" * 12, client="10.0.5.1").json()["error"],
            self.login("vera", "x" * 12, client="10.0.5.2").json()["error"],
        )

    def test_a_name_nobody_has_costs_a_hash(self):
        with mock.patch.object(auth, "burn_a_check",
                               wraps=auth.burn_a_check) as burn:
            self.login("nobody-here", "some password")
        burn.assert_called_once_with("some password")

    def test_wrong_codes_count_towards_the_lock(self):
        # the password is known here; the code is what is being guessed
        right = self.code_now("anton")
        wrong = "000000" if right != "000000" else "111111"
        last = None
        for n in range(signin.ACCOUNT_LOCK_FAILURES):
            ticket = self.login("anton", client=f"10.0.6.{n}").json()["ticket"]
            last = self.code(ticket, wrong, client=f"10.0.6.{n}")
        self.assertEqual(last.status, 423)
        self.assertEqual(self.login("anton", client="10.0.7.1").status, 423)

    def test_failures_are_logged_with_the_address(self):
        self.add("vera", "user")
        logger = logging.getLogger("moonlan")
        logger.setLevel(logging.WARNING)
        with self.assertLogs("moonlan", "WARNING") as logged:
            self.login("vera", "wrong password", client="10.9.8.7")
        self.assertIn("10.9.8.7", logged.output[0])


class BindingTest(SignInCase):
    """TOTP bound from the browser, and the recovery codes."""

    def setup(self, cookie):
        return call(server.app, "POST", "/api/auth/totp/setup", cookie=cookie)

    def confirm(self, cookie, code, password=PASSWORD):
        return call(server.app, "POST", "/api/auth/totp/confirm",
                    cookie=cookie,
                    json_body={"code": code, "password": password})

    def test_an_administrator_binds_it_right_after_the_password(self):
        self.add("boris", "admin")
        cookie = cookie_of(self.login("boris"))
        self.assertEqual(self.me(cookie).json()["step"], "totp")
        shown = self.setup(cookie)
        self.assertEqual(shown.status, 200, shown.body)
        secret = shown.json()["secret"]
        self.assertEqual(shown.json()["uri"], auth.otpauth_uri("boris", secret))
        # nothing is bound until a code made from it comes back
        self.assertEqual(self.db.user("boris")["totp_enabled"], 0)
        code = auth.totp_code(auth.secret_bytes(secret), time.time())
        bound = self.confirm(cookie, code)
        self.assertEqual(bound.status, 200, bound.body)
        codes = bound.json()["recovery"]
        self.assertEqual(len(codes), 8)
        me = self.me(cookie).json()
        self.assertEqual((me["step"], me["totp"], me["recovery_left"]),
                         (None, True, 8))
        # this very session goes on as a full one
        self.assertEqual(
            call(server.app, "DELETE", "/api/layout", cookie=cookie).status,
            200,
        )
        # and the code used to bind it is spent
        ticket = self.login("boris").json()["ticket"]
        self.assertEqual(self.code(ticket, code).json(), {"error": "code_used"})

    def test_a_scanning_mistake_locks_nobody_out(self):
        self.add("vera", "user")
        cookie = cookie_of(self.login("vera"))
        secret = self.setup(cookie).json()["secret"]
        right = auth.totp_code(auth.secret_bytes(secret), time.time())
        wrong = "000000" if right != "000000" else "111111"
        self.assertEqual(self.confirm(cookie, wrong).json(),
                         {"error": "invalid_code"})
        self.assertEqual(self.db.user("vera")["totp_enabled"], 0)
        # still signs in with the password alone
        self.assertEqual(self.login("vera").json(), {"status": "signed_in"})

    def test_binding_asks_for_the_password(self):
        self.add("vera", "user")
        cookie = cookie_of(self.login("vera"))
        secret = self.setup(cookie).json()["secret"]
        code = auth.totp_code(auth.secret_bytes(secret), time.time())
        self.assertEqual(self.confirm(cookie, code, "not the password").json(),
                         {"error": "wrong_password"})
        self.assertEqual(self.db.user("vera")["totp_enabled"], 0)

    def test_binding_closes_the_other_sessions(self):
        self.add("vera", "user")
        here = cookie_of(self.login("vera"))
        there = cookie_of(self.login("vera"))
        secret = self.setup(here).json()["secret"]
        self.confirm(here, auth.totp_code(auth.secret_bytes(secret),
                                          time.time()))
        self.assertEqual(self.me(here).status, 200)
        self.assertEqual(self.me(there).status, 401)

    def test_rebinding_retires_the_old_secret(self):
        old = self.secret("anton")
        ticket = self.login("anton").json()["ticket"]
        cookie = cookie_of(self.code(ticket, self.code_now("anton")))
        new = self.setup(cookie).json()["secret"]
        # the old secret works until the new one is confirmed
        self.assertEqual(self.secret("anton"), old)
        at = time.time() + 30   # the next step: the current one is spent
        self.confirm(cookie, auth.totp_code(auth.secret_bytes(new), at))
        self.assertEqual(self.secret("anton"), new)

    def test_a_recovery_code_instead_of_the_app(self):
        codes = auth.new_recovery_codes()
        self.db.enable_totp("anton", self.secret("anton"),
                            auth.hash_recovery_codes(codes))
        ticket = self.login("anton").json()["ticket"]
        answer = self.code(ticket, codes[2].upper())
        self.assertEqual(answer.status, 200, answer.body)
        cookie = cookie_of(answer)
        self.assertEqual(self.me(cookie).json()["recovery_left"], 7)
        # it works once
        ticket = self.login("anton").json()["ticket"]
        self.assertEqual(self.code(ticket, codes[2]).json(),
                         {"error": "invalid_code"})
        # the others still work
        ticket = self.login("anton").json()["ticket"]
        self.assertEqual(self.code(ticket, codes[5]).status, 200)
        login = server.db.journal(1)[0]
        self.assertEqual(login["event"], "login")
        self.assertIn('"recovery_left": 6', login["details"])

    def test_not_while_sign_in_is_off(self):
        self.clean()
        self.assertEqual(self.setup(None).json(), {"error": "sign_in_off"})


class AccountsApiTest(SignInCase):
    """The Users section's API: an administrator's, the console's rules."""

    def setUp(self):
        super().setUp()
        ticket = self.login("anton").json()["ticket"]
        self.admin = cookie_of(self.code(ticket, self.code_now("anton")))

    def api(self, method, path, body=None, cookie=None):
        return call(server.app, method, path, json_body=body,
                    cookie=cookie or self.admin)

    def test_create_with_a_temporary_password(self):
        answer = self.api("POST", "/api/users", {"name": "vera", "role": "user"})
        self.assertEqual(answer.status, 200, answer.body)
        password = answer.json()["password"]
        self.assertIsNone(auth.password_problem("vera", password))
        cookie = cookie_of(self.login("vera", password))
        self.assertEqual(self.me(cookie).json()["step"], "password")
        event = server.db.journal(5)
        added = next(e for e in event if e["event"] == "user_added")
        self.assertEqual((added["mac"], added["user"]), ("vera", "anton"))

    def test_names_and_roles_are_checked(self):
        self.assertEqual(
            self.api("POST", "/api/users", {"name": "two words",
                                            "role": "user"}).json(),
            {"error": "bad_name"},
        )
        self.assertEqual(
            self.api("POST", "/api/users", {"name": "vera",
                                            "role": "root"}).json(),
            {"error": "bad_role"},
        )
        self.api("POST", "/api/users", {"name": "vera", "role": "user"})
        taken = self.api("POST", "/api/users", {"name": "VERA", "role": "user"})
        self.assertEqual((taken.status, taken.json()["error"]),
                         (409, "exists"))

    def test_the_last_administrator_here_too(self):
        for body in ({"role": "user"}, {"disabled": True}):
            answer = self.api("PATCH", "/api/users/anton", body)
            self.assertEqual((answer.status, answer.json()["error"]),
                             (409, "last_admin"), body)
        answer = self.api("DELETE", "/api/users/anton")
        self.assertEqual((answer.status, answer.json()["error"]),
                         (409, "last_admin"))

    def test_role_disable_delete(self):
        self.add("vera", "user")
        vera = cookie_of(self.login("vera"))
        self.assertEqual(self.api("PATCH", "/api/users/vera",
                                  {"role": "viewer"}).status, 200)
        self.assertEqual(self.db.user("vera")["role"], "viewer")
        # a new role signs the account out
        self.assertEqual(self.me(vera).status, 401)
        vera = cookie_of(self.login("vera"))
        self.api("PATCH", "/api/users/vera", {"disabled": True})
        self.assertEqual(self.me(vera).status, 401)
        self.assertEqual(self.login("vera").json(), {"error": "disabled"})
        self.api("PATCH", "/api/users/vera", {"disabled": False})
        self.assertEqual(self.login("vera").status, 200)
        self.assertEqual(self.api("DELETE", "/api/users/vera").status, 200)
        self.assertIsNone(self.db.user("vera"))
        self.assertEqual(self.api("DELETE", "/api/users/vera").status, 404)

    def test_new_password_reset_totp_unlock_sign_out(self):
        self.add("vera", "user", totp=True)
        answer = self.api("POST", "/api/users/vera/password")
        password = answer.json()["password"]
        self.assertTrue(auth.verify_password(
            password, self.db.user("vera")["password"]))
        self.assertEqual(self.db.user("vera")["must_change"], 1)
        self.assertEqual(self.api("POST", "/api/users/vera/reset-totp").status,
                         200)
        self.assertEqual(self.db.user("vera")["totp_enabled"], 0)
        self.db.lock(self.db.user("vera")["id"], time.time() + 600)
        self.assertEqual(self.api("POST", "/api/users/vera/unlock").json(),
                         {"status": "unlocked"})
        vera = cookie_of(self.login("vera", password))
        self.assertEqual(self.api("POST", "/api/users/vera/logout").json(),
                         {"sessions_closed": 1})
        self.assertEqual(self.me(vera).status, 401)

    def test_the_list(self):
        self.add("vera", "user")
        users = {u["name"]: u for u in self.api("GET", "/api/users").json()["users"]}
        self.assertEqual(set(users), {"anton", "vera"})
        self.assertEqual((users["anton"]["role"], users["anton"]["totp"],
                          users["anton"]["sessions"]), ("admin", True, 1))
        self.assertNotIn("password", users["vera"])
        self.assertNotIn("totp_secret", users["vera"])

    def test_only_for_administrators(self):
        self.add("vera", "user")
        vera = cookie_of(self.login("vera"))
        for method, path in (("GET", "/api/users"),
                             ("POST", "/api/users/anton/password"),
                             ("DELETE", "/api/users/anton")):
            answer = self.api(method, path, cookie=vera)
            self.assertEqual((answer.status, answer.json()["need"]),
                             (403, "admin"), path)

    def test_never_while_sign_in_is_off(self):
        # the first administrator comes from the console, not from here
        self.clean()
        answer = call(server.app, "POST", "/api/users",
                      json_body={"name": "mallory", "role": "admin"})
        self.assertEqual((answer.status, answer.json()),
                         (403, {"error": "sign_in_off"}))
        self.assertIsNone(self.db.user("mallory"))


if __name__ == "__main__":
    unittest.main()
