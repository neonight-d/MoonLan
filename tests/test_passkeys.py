"""Signing in with a key (v0.7.6): the package, the storage, adding a
key, signing in with it, and an administrator's second factor."""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from moonlan import auth
from moonlan.db import PASSKEYS_MAX, AccountError, Database

ROOT = Path(__file__).resolve().parent.parent

# The service imported in a fresh interpreter in which fido2 and
# cryptography cannot be imported — as on a machine where
# `pip install fido2` failed to build cryptography
WITHOUT_FIDO2 = textwrap.dedent("""
    import importlib.abc, json, logging, sys

    class Missing(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name.split(".")[0] in ("fido2", "cryptography"):
                raise ModuleNotFoundError(f"No module named {name!r}",
                                          name=name)
            return None

    sys.meta_path.insert(0, Missing())
    lines = []

    class Keep(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())

    logging.getLogger("moonlan").addHandler(Keep())
    logging.getLogger("moonlan").setLevel(logging.INFO)
    sys.path.insert(0, "tests")
    from asgi_client import call
    from moonlan import passkeys, server
    server._log_config()
    # sign-in on: the form is where a key would be offered
    server.accounts.add_user("anton", "admin", "scrypt$unused")
    print(json.dumps({
        "health": call(server.app, "GET", "/api/health").status,
        "me": call(server.app, "GET", "/api/auth/me").json(),
        "version": passkeys.fido2_version(),
        "log": lines,
        "loaded": sorted(m for m in sys.modules
                         if m.split(".")[0] in ("fido2", "cryptography")),
    }))
""")


class WithoutFido2Test(unittest.TestCase):
    def run_without(self, public_url):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "config.yaml"
            config.write_text(
                f"db_path: {Path(folder) / 'test.db'}\n"
                "scan_interval_minutes: 0\n"
                f"listen:\n  public_url: {public_url}\n",
                encoding="utf-8",
            )
            done = subprocess.run(
                [sys.executable, "-c", WITHOUT_FIDO2], cwd=ROOT,
                env={**os.environ, "MOONLAN_CONFIG": str(config)},
                capture_output=True, text=True, timeout=120,
            )
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout.strip().splitlines()[-1])

    def test_the_service_starts_and_says_why(self):
        result = self.run_without("https://example.local:8443")
        self.assertEqual(result["loaded"], [])
        self.assertEqual(result["health"], 200)
        self.assertIsNone(result["version"])
        self.assertEqual(result["me"]["passkeys"],
                         {"why": "no_fido2",
                          "origin": "https://example.local:8443"})
        self.assertTrue(any(
            "Signing in with a key is off" in line and "fido2" in line
            for line in result["log"]
        ), result["log"])

    def test_an_address_problem_comes_first(self):
        # the order the page shows reasons in: the address, then the
        # package — fixing the package would not help an IP address
        result = self.run_without("https://10.0.0.5")
        self.assertEqual(result["me"]["passkeys"]["why"], "public_url_ip")
        self.assertFalse(any("Signing in with a key is off" in line
                             for line in result["log"]))


class WithFido2Test(unittest.TestCase):
    def test_the_version_is_known(self):
        from moonlan import passkeys
        version = passkeys.fido2_version()
        self.assertIsNotNone(version)
        self.assertEqual(int(version.split(".")[0]), 2)



# ---------- storage ----------

def a_key(n=0, **extra):
    return {"credential_id": bytes([n]) * 16, "public_key": b"\xa1\x01\x02",
            "sign_count": 0, "aaguid": "", "transports": "usb",
            "discoverable": 1, "user_verified": 1, "passwordless": 0,
            "label": f"key {n}", **extra}


class StorageTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "moonlan.db"
        self.db = Database(self.path)
        self.addCleanup(self.db.close)
        self.anton = self.db.add_user("anton", "admin", "scrypt$x")
        self.vera = self.db.add_user("vera", "user", "scrypt$x")

    def session(self, user, token="t"):
        self.db.add_session(token, user["id"], time.time())

    def test_an_old_database_gets_the_table_and_the_column(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "old.db"
            conn = sqlite3.connect(path)
            conn.execute(
                "CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "name TEXT NOT NULL, name_key TEXT NOT NULL UNIQUE, "
                "role TEXT NOT NULL, password TEXT NOT NULL, "
                "totp_secret TEXT DEFAULT '', totp_enabled INTEGER DEFAULT 0, "
                "recovery TEXT DEFAULT '', must_change INTEGER DEFAULT 0, "
                "disabled INTEGER DEFAULT 0, created_at REAL NOT NULL, "
                "last_login REAL DEFAULT 0)"
            )
            conn.execute("INSERT INTO users (name, name_key, role, password, "
                         "created_at) VALUES ('anton', 'anton', 'admin', "
                         "'x', 0)")
            conn.commit()
            conn.close()
            db = Database(path)
            try:
                user = db.user("anton")
                self.assertIsNone(user["webauthn_user_id"])
                self.assertEqual(db.passkeys(user["id"]), [])
            finally:
                db.close()

    def test_the_handle_is_random_and_stays(self):
        first = self.db.webauthn_user_id(self.anton["id"])
        self.assertEqual(len(first), 32)
        self.assertEqual(self.db.webauthn_user_id(self.anton["id"]), first)
        self.assertNotEqual(self.db.webauthn_user_id(self.vera["id"]), first)
        self.assertNotIn(b"anton", first)

    def test_a_key_and_what_is_kept_of_it(self):
        number, closed = self.db.add_passkey(self.vera["id"], a_key(1))
        self.assertEqual((number, closed), (1, 0))
        (key,) = self.db.passkeys(self.vera["id"])
        for column in ("credential_id", "public_key", "sign_count", "aaguid",
                       "transports", "discoverable", "user_verified",
                       "passwordless", "label", "created_at", "last_used"):
            self.assertIn(column, key)
        self.assertEqual(key["number"], 1)
        self.assertEqual(
            self.db.passkey_by_credential(bytes([1]) * 16)["id"], key["id"]
        )
        self.db.use_passkey(key["id"], 7, 1234.0)
        key = self.db.passkeys(self.vera["id"])[0]
        self.assertEqual((key["sign_count"], key["last_used"]), (7, 1234.0))

    def test_a_credential_belongs_to_one_account(self):
        self.db.add_passkey(self.vera["id"], a_key(1))
        with self.assertRaises(AccountError) as refused:
            self.db.add_passkey(self.anton["id"], a_key(1))
        self.assertEqual(refused.exception.code, "key_exists")

    def test_at_most_ten(self):
        for n in range(PASSKEYS_MAX):
            self.db.add_passkey(self.vera["id"], a_key(n))
        with self.assertRaises(AccountError) as refused:
            self.db.add_passkey(self.vera["id"], a_key(99))
        self.assertEqual(refused.exception.code, "too_many_keys")
        self.assertEqual(PASSKEYS_MAX, 10)

    def test_the_first_factor_closes_the_other_sessions(self):
        self.session(self.vera, "here")
        self.session(self.vera, "elsewhere")
        _, closed = self.db.add_passkey(self.vera["id"], a_key(1),
                                        keep_session="here")
        self.assertEqual(closed, 1)
        self.assertEqual(self.db.session_row("here")["second_factor"], 1)
        self.assertEqual(self.db.session_row("here")["passkeys"], 1)
        self.session(self.vera, "later")
        _, closed = self.db.add_passkey(self.vera["id"], a_key(2),
                                        keep_session="here")
        self.assertEqual(closed, 0)

    def test_recovery_codes_only_when_there_are_none(self):
        self.db.add_passkey(self.vera["id"], a_key(1), recovery="h1\nh2")
        self.assertEqual(self.db.user("vera")["recovery"], "h1\nh2")
        self.db.add_passkey(self.vera["id"], a_key(2), recovery="h3")
        self.assertEqual(self.db.user("vera")["recovery"], "h1\nh2")

    def test_an_administrator_keeps_a_second_factor(self):
        self.db.add_passkey(self.anton["id"], a_key(1))
        self.db.add_passkey(self.anton["id"], a_key(2))
        first, second = self.db.passkeys(self.anton["id"])
        self.db.remove_passkey("anton", first["id"])
        with self.assertRaises(AccountError) as refused:
            self.db.remove_passkey("anton", second["id"])
        self.assertEqual(refused.exception.code, "last_factor")
        # with TOTP the key is not the last factor
        self.db.enable_totp("anton", auth.new_totp_secret(), "")
        self.db.remove_passkey("anton", second["id"])
        # a user needs none
        self.db.add_passkey(self.vera["id"], a_key(3))
        self.db.remove_passkey("vera", self.db.passkeys(self.vera["id"])[0]["id"])

    def test_somebody_elses_key_is_not_found(self):
        self.db.add_passkey(self.vera["id"], a_key(1))
        key = self.db.passkeys(self.vera["id"])[0]
        with self.assertRaises(AccountError) as refused:
            self.db.remove_passkey("anton", key["id"])
        self.assertEqual(refused.exception.code, "unknown_key")

    def test_removing_closes_sessions(self):
        self.db.add_passkey(self.vera["id"], a_key(1))
        self.session(self.vera, "here")
        self.session(self.vera, "elsewhere")
        key = self.db.passkeys(self.vera["id"])[0]
        _, closed = self.db.remove_passkey("vera", key["id"], "here")
        self.assertEqual(closed, 1)

    def test_a_reset_is_allowed_even_for_the_last_factor(self):
        self.db.add_passkey(self.anton["id"], a_key(1))
        self.session(self.anton)
        self.assertEqual(self.db.reset_passkeys("anton"), (1, 1))
        self.assertEqual(self.db.passkeys(self.anton["id"]), [])

    def test_deleting_an_account_takes_its_keys(self):
        self.db.add_passkey(self.vera["id"], a_key(1))
        self.db.delete_user("vera")
        self.assertIsNone(self.db.passkey_by_credential(bytes([1]) * 16))
        self.assertEqual(self.db.passkey_counts(), {})


if __name__ == "__main__":
    unittest.main()
