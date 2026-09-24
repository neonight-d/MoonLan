"""Accounts: password hashes, the rules for names and passwords, the
users table and the journal that names who did what (v0.7.4)."""

import contextlib
import io
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from moonlan import auth, users
from moonlan.db import AccountError, Database


class PasswordHashTest(unittest.TestCase):
    def test_the_parameters_travel_with_the_hash(self):
        stored = auth.hash_password("correct horse battery")
        scheme, n, r, p, salt, digest = stored.split("$")
        self.assertEqual(
            (scheme, n, r, p), ("scrypt", "16384", "8", "1")
        )
        self.assertTrue(salt and digest)

    def test_right_and_wrong_password(self):
        stored = auth.hash_password("correct horse battery")
        self.assertTrue(auth.verify_password("correct horse battery", stored))
        self.assertFalse(auth.verify_password("correct horse batterY", stored))
        self.assertFalse(auth.verify_password("", stored))

    def test_the_same_password_twice_is_two_hashes(self):
        self.assertNotEqual(
            auth.hash_password("correct horse battery"),
            auth.hash_password("correct horse battery"),
        )

    def test_a_broken_record_is_a_wrong_password_not_a_crash(self):
        for stored in ("", "plain", "scrypt$x$8$1$AA$AA", "md5$1$2$3$4$5"):
            self.assertFalse(auth.verify_password("anything", stored))

    def test_a_hash_made_with_other_parameters_still_opens(self):
        # parameters raised in a later version must not lock anybody out:
        # an old hash is read with the parameters written in it
        with mock.patch.object(auth, "SCRYPT_N", 2 ** 10):
            old = auth.hash_password("correct horse battery")
        self.assertIn("$1024$", old)
        self.assertTrue(auth.verify_password("correct horse battery", old))
        self.assertTrue(auth.needs_rehash(old))
        self.assertFalse(auth.needs_rehash(auth.hash_password("x" * 10)))


class RulesTest(unittest.TestCase):
    def test_password_length(self):
        self.assertEqual(auth.password_problem("anton", "123456789"), "too_short")
        self.assertIsNone(auth.password_problem("anton", "1234567890"))
        self.assertEqual(
            auth.password_problem("anton", "x" * 2000), "too_long"
        )

    def test_password_is_not_the_name(self):
        self.assertEqual(
            auth.password_problem("Headteacher", "headTEACHER"), "same_as_name"
        )

    def test_no_composition_rules(self):
        # no capital, no digit, no symbol — and long: accepted
        self.assertIsNone(
            auth.password_problem("anton", "the quiet library at dusk")
        )

    def test_names(self):
        for good in ("anton", "a.petrov", "wall-monitor_2", "Антон"):
            self.assertIsNone(auth.name_problem(good), good)
        for bad in ("", "two words", "@console", "x" * 33, "a/b"):
            self.assertEqual(auth.name_problem(bad), "bad_name", bad)


class UsersTableTest(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.addCleanup(self.db.close)

    def test_add_and_find_in_any_case(self):
        self.db.add_user("Anton", "admin", "hash")
        self.assertEqual(self.db.user("anton")["name"], "Anton")
        self.assertEqual(self.db.user("ANTON")["role"], "admin")
        self.assertIsNone(self.db.user("boris"))

    def test_one_name_in_any_letter_case(self):
        self.db.add_user("Anton", "admin", "hash")
        with self.assertRaises(AccountError) as caught:
            self.db.add_user("anton", "viewer", "hash")
        self.assertEqual(caught.exception.code, "exists")

    def test_letter_case_beyond_ascii(self):
        # SQLite's NOCASE folds ASCII only: "Антон" and "антон" would
        # have been two accounts
        self.db.add_user("Антон", "user", "hash")
        with self.assertRaises(AccountError):
            self.db.add_user("антон", "user", "hash")

    def test_new_account_defaults(self):
        row = self.db.add_user("anton", "viewer", "hash", must_change=True)
        self.assertEqual(row["must_change"], 1)
        self.assertEqual(row["disabled"], 0)
        self.assertEqual(row["totp_enabled"], 0)
        self.assertEqual(row["last_login"], 0)
        self.assertGreater(row["created_at"], 0)


class JournalUserTest(unittest.TestCase):
    def test_an_event_names_who_did_it(self):
        db = Database(":memory:")
        self.addCleanup(db.close)
        db.add_event(100.0, "layout_cleared", "", "3 node(s)", user="anton")
        db.add_event(200.0, "host_down", "aa:bb:cc:dd:ee:ff")
        events = db.journal(10)
        self.assertEqual(events[0]["user"], "")
        self.assertEqual(events[1]["user"], "anton")

    def test_a_v073_journal_gains_the_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            conn = sqlite3.connect(path)
            conn.executescript(
                "CREATE TABLE journal (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " ts REAL NOT NULL, event TEXT NOT NULL, mac TEXT NOT NULL,"
                " details TEXT DEFAULT '');"
                "INSERT INTO journal (ts, event, mac, details) "
                "VALUES (1.0, 'layout_cleared', '', '5 node(s)');"
            )
            conn.commit()
            conn.close()
            db = Database(path)
            events = db.journal(10)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["user"], "")
            self.assertEqual(db.users(), [])
            db.close()


def add(db, name, role="viewer", disabled=False):
    db.add_user(name, role, auth.hash_password("correct horse battery"))
    if disabled:
        db.set_disabled(name, True)


def open_sessions(db, name, n=2):
    user_id = db.user(name)["id"]
    for i in range(n):
        db.add_session(f"{name}-{i}", user_id, time.time())


class LastAdminTest(unittest.TestCase):
    """Sign-in switches off without an enabled administrator, so the
    last one cannot be removed, disabled or demoted — from the console
    or from the browser, which call the same methods."""

    def setUp(self):
        self.db = Database(":memory:")
        self.addCleanup(self.db.close)
        add(self.db, "anton", "admin")
        add(self.db, "vera", "user")

    def refused(self, call, *args):
        with self.assertRaises(AccountError) as caught:
            call(*args)
        self.assertEqual(caught.exception.code, "last_admin")

    def test_the_last_admin_stays(self):
        self.refused(self.db.set_role, "anton", "user")
        self.refused(self.db.set_disabled, "anton", True)
        self.refused(self.db.delete_user, "anton")
        row = self.db.user("anton")
        self.assertEqual((row["role"], row["disabled"]), ("admin", 0))
        self.assertEqual(self.db.active_admins(), 1)

    def test_with_a_second_admin_it_can_go(self):
        self.db.set_role("vera", "admin")
        self.db.set_role("anton", "user")
        self.assertEqual(self.db.active_admins(), 1)
        self.refused(self.db.delete_user, "vera")

    def test_a_disabled_admin_does_not_count(self):
        add(self.db, "boris", "admin", disabled=True)
        self.refused(self.db.set_role, "anton", "viewer")
        # enabling boris makes anton free to go
        self.db.set_disabled("boris", False)
        self.db.delete_user("anton")
        self.assertIsNone(self.db.user("anton"))

    def test_other_accounts_are_not_held(self):
        self.db.set_disabled("vera", True)
        self.db.delete_user("vera")
        self.assertIsNone(self.db.user("vera"))

    def test_unknown_name(self):
        with self.assertRaises(AccountError) as caught:
            self.db.set_role("nobody", "user")
        self.assertEqual(caught.exception.code, "unknown")


class ClosingSessionsTest(unittest.TestCase):
    """A new password, a new role, disabling, deleting and resetting
    the second factor sign the account out everywhere."""

    def setUp(self):
        self.db = Database(":memory:")
        self.addCleanup(self.db.close)
        add(self.db, "anton", "admin")
        add(self.db, "vera", "user")
        open_sessions(self.db, "anton")
        open_sessions(self.db, "vera")

    def assert_closed(self, name, closed):
        self.assertEqual(closed, 2)
        self.assertEqual(self.db.sessions_of(name), [])
        # nobody else's
        other = "anton" if name == "vera" else "vera"
        self.assertEqual(len(self.db.sessions_of(other)), 2)

    def test_password(self):
        self.assert_closed(
            "vera", self.db.set_password("vera", auth.hash_password("x" * 12))
        )

    def test_password_changed_in_the_browser_keeps_that_browser(self):
        closed = self.db.set_password(
            "vera", auth.hash_password("x" * 12), keep_session="vera-1"
        )
        self.assertEqual(closed, 1)
        self.assertEqual(
            [s["token_hash"] for s in self.db.sessions_of("vera")], ["vera-1"]
        )

    def test_role(self):
        self.assert_closed("vera", self.db.set_role("vera", "viewer")[1])

    def test_disable(self):
        self.assert_closed("vera", self.db.set_disabled("vera", True))

    def test_delete(self):
        self.assert_closed("vera", self.db.delete_user("vera"))

    def test_reset_totp(self):
        self.assert_closed("anton", self.db.reset_totp("anton"))

    def test_close_sessions(self):
        self.assert_closed("anton", self.db.close_sessions("anton"))

    def test_binding_totp_elsewhere_closes_password_only_sessions(self):
        # an admin's session opened on a password alone must not become
        # a full one because a secret was bound on the console meanwhile
        self.assert_closed(
            "anton", self.db.enable_totp("anton", "SECRET", "")
        )


class TotpVectorsTest(unittest.TestCase):
    """RFC 6238, appendix B, SHA1 rows: eight digits there."""

    KEY = b"12345678901234567890"
    VECTORS = [
        (59, "94287082"),
        (1111111109, "07081804"),
        (1111111111, "14050471"),
        (1234567890, "89005924"),
        (2000000000, "69279037"),
        (20000000000, "65353130"),
    ]

    def test_rfc6238_sha1(self):
        for at, code in self.VECTORS:
            self.assertEqual(auth.totp_code(self.KEY, at, digits=8), code, at)

    def test_six_digits_are_the_last_six(self):
        self.assertEqual(auth.totp_code(self.KEY, 59), "287082")

    def test_one_step_either_side(self):
        secret = auth.new_totp_secret()
        key = auth.secret_bytes(secret)
        now = 1_700_000_015.0
        step = auth.totp_step(now)
        for offset in (-1, 0, 1):
            code = auth.totp_code(key, now + offset * 30)
            self.assertEqual(auth.match_totp(secret, code, now), step + offset)
        for offset in (-2, 2):
            code = auth.totp_code(key, now + offset * 30)
            self.assertIsNone(auth.match_totp(secret, code, now))

    def test_secret_as_people_type_it(self):
        secret = auth.new_totp_secret()
        self.assertEqual(len(auth.secret_bytes(secret)), 20)
        spaced = " ".join(secret[i:i + 4] for i in range(0, 32, 4)).lower()
        self.assertEqual(auth.secret_bytes(spaced), auth.secret_bytes(secret))

    def test_the_otpauth_line(self):
        uri = auth.otpauth_uri("anton", "ABCDEFGH")
        self.assertTrue(uri.startswith("otpauth://totp/MoonLan:anton?"))
        for part in ("secret=ABCDEFGH", "issuer=MoonLan", "algorithm=SHA1",
                     "digits=6", "period=30"):
            self.assertIn(part, uri)


class RecoveryCodesTest(unittest.TestCase):
    def test_a_code_works_once(self):
        codes = auth.new_recovery_codes()
        self.assertEqual(len(codes), 8)
        self.assertEqual(len(set(codes)), 8)
        stored = auth.hash_recovery_codes(codes)
        self.assertNotIn(codes[0], stored)
        left = auth.spend_recovery_code(codes[3].upper().replace("-", " "), stored)
        self.assertIsNotNone(left)
        self.assertEqual(auth.recovery_left(left), 7)
        self.assertIsNone(auth.spend_recovery_code(codes[3], left))
        self.assertIsNone(auth.spend_recovery_code("aaaaa-aaaaa", left))


class ConsoleTest(unittest.TestCase):
    """python -m moonlan.users, against a database of its own."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "moonlan.db"
        config = Path(self.tmp.name) / "config.yaml"
        config.write_text(f"db_path: {self.path}\n", encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {"MOONLAN_CONFIG": str(config)})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("MOONLAN_DEMO", None)

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *argv, passwords=(), answers=()):
        out = io.StringIO()
        with mock.patch("getpass.getpass", side_effect=list(passwords)), \
                mock.patch("builtins.input", side_effect=list(answers)), \
                contextlib.redirect_stdout(out):
            code = users.main(list(argv))
        return code, out.getvalue()

    def db(self):
        db = Database(self.path)
        self.addCleanup(db.close)
        return db

    def test_the_first_admin_switches_sign_in_on(self):
        code, out = self.run_cli(
            "add", "anton", "--role", "admin",
            passwords=["correct horse battery"] * 2,
        )
        self.assertEqual(code, 0, out)
        self.assertIn("Sign-in is ON", out)
        db = self.db()
        row = db.user("anton")
        self.assertEqual(row["role"], "admin")
        self.assertTrue(
            auth.verify_password("correct horse battery", row["password"])
        )
        self.assertEqual(db.active_admins(), 1)
        event = db.journal(1)[0]
        self.assertEqual(
            (event["event"], event["mac"], event["user"]),
            ("user_added", "anton", "@console"),
        )
        self.assertEqual(json.loads(event["details"])["role"], "admin")

    def test_a_refused_password_creates_nothing(self):
        code, out = self.run_cli(
            "add", "anton", passwords=["short", "anton12345", "ANTON12345",
                                       "different1", "different2"],
        )
        self.assertEqual(code, 1)
        self.assertIn("Too short", out)
        self.assertIsNone(self.db().user("anton"))

    def test_no_password_on_the_command_line(self):
        with contextlib.redirect_stderr(io.StringIO()):
            for argv in (["add", "anton", "--password", "x" * 12],
                         ["passwd", "anton", "correct horse battery"]):
                with self.assertRaises(SystemExit):
                    users.build_parser().parse_args(argv)

    def test_the_last_admin_is_refused_with_a_reason(self):
        self.run_cli("add", "anton", "--role", "admin",
                     passwords=["correct horse battery"] * 2)
        for argv in (["role", "anton", "user"], ["disable", "anton"],
                     ["delete", "anton", "--yes"]):
            code, out = self.run_cli(*argv)
            self.assertEqual(code, 1, argv)
            self.assertIn("last enabled administrator", out)
        self.assertEqual(self.db().active_admins(), 1)

    def test_passwd_closes_sessions(self):
        self.run_cli("add", "vera", "--role", "user",
                     passwords=["correct horse battery"] * 2)
        db = self.db()
        open_sessions(db, "vera", 3)
        code, out = self.run_cli(
            "passwd", "vera", "--temporary",
            passwords=["a new long password"] * 2,
        )
        self.assertEqual(code, 0, out)
        self.assertIn("3 session(s) closed", out)
        self.assertEqual(db.sessions_of("vera"), [])
        self.assertEqual(db.user("vera")["must_change"], 1)

    def test_totp_binds_and_prints_the_secret(self):
        self.run_cli("add", "anton", "--role", "admin",
                     passwords=["correct horse battery"] * 2)
        code, out = self.run_cli("totp", "anton")
        self.assertEqual(code, 0, out)
        row = self.db().user("anton")
        self.assertEqual(row["totp_enabled"], 1)
        self.assertIn(auth.otpauth_uri("anton", row["totp_secret"]), out)
        self.assertIn("ykman oath accounts uri", out)
        self.assertEqual(auth.recovery_left(row["recovery"]), 8)
        code, out = self.run_cli("reset-totp", "anton")
        row = self.db().user("anton")
        self.assertEqual((row["totp_enabled"], row["totp_secret"],
                          row["recovery"]), (0, "", ""))

    def test_unlock(self):
        self.run_cli("add", "vera", passwords=["correct horse battery"] * 2)
        db = self.db()
        with db._lock, db._conn:
            db._conn.execute(
                "UPDATE users SET locked_until = ?", (time.time() + 600,)
            )
        code, out = self.run_cli("unlock", "vera")
        self.assertIn("unlocked", out)
        self.assertEqual(db.user("vera")["locked_until"], 0)
        self.assertEqual(db.journal(1)[0]["event"], "user_unlocked")

    def test_list(self):
        self.run_cli("add", "anton", "--role", "admin",
                     passwords=["correct horse battery"] * 2)
        code, out = self.run_cli("list")
        self.assertEqual(code, 0)
        self.assertIn("anton", out)
        self.assertIn("off!", out)

    def test_delete_asks(self):
        self.run_cli("add", "vera", passwords=["correct horse battery"] * 2)
        code, out = self.run_cli("delete", "vera", answers=["n"])
        self.assertEqual(code, 1)
        self.assertIsNotNone(self.db().user("vera"))
        with mock.patch("sys.stdin.isatty", return_value=True):
            code, out = self.run_cli("delete", "vera", answers=["y"])
        self.assertEqual(code, 0, out)
        self.assertIsNone(self.db().user("vera"))


if __name__ == "__main__":
    unittest.main()
