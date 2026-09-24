"""Accounts: password hashes, the rules for names and passwords, the
users table and the journal that names who did what (v0.7.4)."""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from moonlan import auth
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


if __name__ == "__main__":
    unittest.main()
