"""diag and sign-in (v0.7.4): what `diag --config` says about the
accounts, and how diag asks a service that wants a session."""

import contextlib
import io
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import service_fixture  # noqa: F401  (sets MOONLAN_CONFIG first)
from asgi_client import call
from moonlan import auth, diag, server
from moonlan.config import load_config
from moonlan.db import Database


class SignInSectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "moonlan.db"
        config = Path(self.tmp.name) / "config.yaml"
        config.write_text(f"db_path: {self.path}\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"MOONLAN_CONFIG": str(config)}):
            os.environ.pop("MOONLAN_DEMO", None)
            self.cfg = load_config()

    def section(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            diag._print_sign_in(self.cfg)
        return out.getvalue()

    def test_before_the_service_has_run_v074(self):
        self.path.touch()
        self.assertIn("sign-in is off", self.section())

    def test_open(self):
        Database(self.path).close()
        text = self.section()
        self.assertIn("OFF — the map is open to everyone", text)
        self.assertIn("python -m moonlan.users add <name> --role admin", text)
        self.assertIn("HTTP", text)

    def test_what_is_wrong_is_named(self):
        db = Database(self.path)
        for name, role in (("anton", "admin"), ("boris", "admin"),
                           ("vera", "user"), ("wall", "viewer")):
            db.add_user(name, role, "hash")
        db.enable_totp("anton", auth.new_totp_secret(), "")
        db.lock(db.user("vera")["id"], time.time() + 600)
        db.add_user("old", "viewer", "hash")
        db.set_disabled("old", True)
        db.close()
        text = self.section()
        self.assertIn("ON — 2 enabled administrator(s)", text)
        self.assertIn("2 administrator(s), 1 user(s), 1 viewer(s); 1 disabled",
                      text)
        self.assertIn("administrators without TOTP: boris", text)
        self.assertIn("locked now:  vera until", text)
        self.assertIn("unlock", text)
        self.assertIn("12 h without a request, 30 days at most", text)


class ConsoleTokenTest(unittest.TestCase):
    """diag writes nothing to the database, so it cannot open a session;
    the service writes it a token at startup instead."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = server.accounts
        self.clean()
        self.addCleanup(self.clean)
        self.db.add_user("anton", "admin", "hash")   # sign-in on
        self.file = Path(self.tmp.name) / "moonlan.db.console"
        server.sign_in.write_console_token(str(self.file))
        self.addCleanup(setattr, server.sign_in, "_console", "")
        self.token = self.file.read_text().strip()

    def clean(self):
        with self.db._lock, self.db._conn:
            self.db._conn.execute("DELETE FROM sessions")
            self.db._conn.execute("DELETE FROM users")

    def test_only_its_own_user_reads_it(self):
        self.assertEqual(stat.S_IMODE(self.file.stat().st_mode), 0o600)

    def test_it_reads_and_does_nothing_else(self):
        header = {"x-moonlan-console": self.token}
        self.assertEqual(
            call(server.app, "GET", "/api/layout", headers=header).status, 200
        )
        self.assertEqual(
            call(server.app, "GET", "/api/polling", headers=header).status, 200
        )
        for method, path in (("POST", "/api/scan"),
                             ("DELETE", "/api/layout"),
                             ("GET", "/api/users")):
            self.assertEqual(
                call(server.app, method, path, headers=header).status, 403,
                path,
            )

    def test_a_wrong_or_old_token_is_nobody(self):
        header = {"x-moonlan-console": "not-the-token"}
        self.assertEqual(
            call(server.app, "GET", "/api/layout", headers=header).status, 401
        )
        # a restart writes a new one; the old one stops working
        server.sign_in.write_console_token(str(self.file))
        header = {"x-moonlan-console": self.token}
        self.assertEqual(
            call(server.app, "GET", "/api/layout", headers=header).status, 401
        )

    def test_diag_sends_it(self):
        cfg = mock.Mock()
        cfg.users_db_path.return_value = str(self.file)[:-len(".console")]
        self.assertEqual(diag._console_token(cfg), self.token)
        cfg.users_db_path.return_value = str(Path(self.tmp.name) / "none.db")
        self.assertEqual(diag._console_token(cfg), "")


if __name__ == "__main__":
    unittest.main()
