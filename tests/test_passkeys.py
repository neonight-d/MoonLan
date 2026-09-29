"""Signing in with a key (v0.7.6): the package, the storage, adding a
key, signing in with it, and an administrator's second factor."""

import contextlib
import io
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

import service_fixture  # noqa: F401  (sets MOONLAN_CONFIG first)
import soft_authenticator as soft
from asgi_client import call
from fido2.utils import websafe_decode
from moonlan import auth, https, passkeys, server, signin, users
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



# ---------- adding a key ----------

PASSWORD = "correct horse battery"
HOST = "example.local:8443"
ORIGIN = "https://" + HOST


class KeysCase(unittest.TestCase):
    """The service at https://example.local:8443, with keys on."""

    def setUp(self):
        logger = logging.getLogger("moonlan")
        self.addCleanup(logger.setLevel, logger.level)
        # a copied key is an ERROR line by design; not here
        logger.setLevel(logging.CRITICAL)
        self.db = server.accounts
        self.clean()
        self.addCleanup(self.clean)
        server.sign_in._tickets.clear()
        server.sign_in.throttle = signin.Throttle()
        public, _ = https.parse_public_url(ORIGIN)
        for name, value in (("public_url", public),
                            ("webauthn", passkeys.Passkeys(public))):
            patch = mock.patch.object(server.sign_in, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.db.add_user("anton", "admin", auth.hash_password(PASSWORD))
        self.db.enable_totp("anton", auth.new_totp_secret(), "")
        self.db.add_user("vera", "user", auth.hash_password(PASSWORD))

    def clean(self):
        with self.db._lock, self.db._conn:
            for table in ("sessions", "passkeys", "users"):
                self.db._conn.execute(f"DELETE FROM {table}")

    def cookie(self, name, second_factor=True):
        """A session of `name`, as the browser at the public address
        holds it."""
        token = auth.new_token()
        self.db.add_session(auth.token_hash(token), self.db.user(name)["id"],
                            time.time(), second_factor)
        return f"{signin.SECURE_SESSION_COOKIE}={token}"

    def post(self, path, body, cookie=None, **kwargs):
        kwargs.setdefault("scheme", "https")
        kwargs.setdefault("host", HOST)
        return call(server.app, "POST", path, json_body=body, cookie=cookie,
                    **kwargs)

    def me(self, cookie):
        return call(server.app, "GET", "/api/auth/me", cookie=cookie,
                    scheme="https", host=HOST).json()

    def begin_add(self, cookie, passwordless=False, password=PASSWORD):
        return self.post("/api/auth/passkeys/begin",
                         {"password": password, "passwordless": passwordless},
                         cookie)

    def add_key(self, name="vera", alg=-7, passwordless=False, uv=True,
                rk=True, label="", cookie=None, origin=ORIGIN, rp_id=None):
        """Adds a key through the API. (the key, the last answer)."""
        cookie = cookie or self.cookie(name)
        begun = self.begin_add(cookie, passwordless)
        self.assertEqual(begun.status, 200, begun.body)
        key, credential = soft.create(begun.json()["options"], origin, alg,
                                      uv=uv, rk=rk, rp_id=rp_id)
        answer = self.post("/api/auth/passkeys/finish", {
            "request": begun.json()["request"], "credential": credential,
            "label": label,
        }, cookie)
        return key, answer


class AddKeyTest(KeysCase):
    def test_the_options(self):
        # the tick decides what is asked of the key: to keep the sign-in
        # and ask for a PIN, or whatever it can
        for passwordless, asked in ((True, "required"), (False, "preferred")):
            with self.subTest(passwordless=passwordless):
                options = self.begin_add(
                    self.cookie("vera"), passwordless
                ).json()["options"]
                selection = options["authenticatorSelection"]
                self.assertEqual(selection["residentKey"], asked)
                self.assertEqual(selection["requireResidentKey"],
                                 passwordless)
                self.assertEqual(selection["userVerification"], asked)
                self.assertEqual(options["extensions"], {"credProps": True})
        begun = self.begin_add(self.cookie("vera"))
        options = begun.json()["options"]
        self.assertEqual(options["rp"]["id"], "example.local")
        self.assertEqual([p["alg"] for p in options["pubKeyCredParams"]],
                         [-7, -8, -257])
        self.assertEqual(options["attestation"], "none")
        self.assertEqual(len(websafe_decode(options["challenge"])), 32)
        handle = websafe_decode(options["user"]["id"])
        self.assertEqual(handle, self.db.user("vera")["webauthn_user_id"])
        self.assertEqual(options["user"]["name"], "vera")
        self.assertEqual(options["excludeCredentials"], [])

    def test_es256_and_eddsa(self):
        for alg in (-7, -8):
            with self.subTest(alg=alg):
                _, answer = self.add_key(alg=alg, label=f"alg {alg}")
                self.assertEqual(answer.status, 200, answer.body)
        keys = self.db.passkeys(self.db.user("vera")["id"])
        self.assertEqual([k["label"] for k in keys], ["alg -7", "alg -8"])
        self.assertEqual([k["transports"] for k in keys], ["usb", "usb"])
        self.assertTrue(all(k["discoverable"] == 1 and k["user_verified"]
                            for k in keys))

    def test_the_same_key_is_not_offered_twice(self):
        key, _ = self.add_key()
        begun = self.begin_add(self.cookie("vera")).json()["options"]
        self.assertEqual(
            [websafe_decode(c["id"]) for c in begun["excludeCredentials"]],
            [key.credential_id],
        )

    def test_the_password_first(self):
        answer = self.begin_add(self.cookie("vera"), password="wrong one!")
        self.assertEqual(answer.status, 403)
        self.assertEqual(answer.json()["error"], "wrong_password")

    def test_not_over_plain_http(self):
        plain = self.cookie("vera").replace(signin.SECURE_SESSION_COOKIE,
                                            signin.SESSION_COOKIE)
        answer = self.post("/api/auth/passkeys/begin",
                           {"password": PASSWORD}, plain,
                           scheme="http", host="10.0.0.5:8080")
        self.assertEqual(answer.json()["error"], "https_required")

    def refused(self, answer, code):
        self.assertEqual(answer.status, 400, answer.body)
        self.assertEqual(answer.json()["error"], code)
        self.assertEqual(self.db.passkeys(self.db.user("vera")["id"]), [])

    def test_another_origin(self):
        _, answer = self.add_key(origin="https://example.local.evil.test")
        self.refused(answer, "key_refused")
        _, answer = self.add_key(origin="https://other.example.local:8443")
        self.refused(answer, "key_refused")

    def test_another_rp_id(self):
        _, answer = self.add_key(rp_id="local")
        self.refused(answer, "key_refused")

    def test_a_challenge_works_once(self):
        cookie = self.cookie("vera")
        begun = self.begin_add(cookie).json()
        _, credential = soft.create(begun["options"], ORIGIN)
        body = {"request": begun["request"], "credential": credential}
        self.assertEqual(
            self.post("/api/auth/passkeys/finish", body, cookie).status, 200
        )
        again = self.post("/api/auth/passkeys/finish", body, cookie)
        self.assertEqual(again.json()["error"], "challenge_expired")

    def test_a_challenge_lasts_five_minutes(self):
        cookie = self.cookie("vera")
        begun = self.begin_add(cookie).json()
        _, credential = soft.create(begun["options"], ORIGIN)
        later = time.time() + passkeys.CHALLENGE_SECONDS + 1
        with mock.patch.object(passkeys.time, "time", return_value=later):
            answer = self.post("/api/auth/passkeys/finish", {
                "request": begun["request"], "credential": credential,
            }, cookie)
        self.refused(answer, "challenge_expired")

    def test_a_challenge_is_for_the_person_who_asked(self):
        begun = self.begin_add(self.cookie("vera")).json()
        _, credential = soft.create(begun["options"], ORIGIN)
        answer = self.post("/api/auth/passkeys/finish", {
            "request": begun["request"], "credential": credential,
        }, self.cookie("anton"))
        self.assertEqual(answer.json()["error"], "challenge_expired")

    def stored(self):
        """(passwordless, discoverable) of vera's keys, oldest first."""
        return [(k["passwordless"], k["discoverable"])
                for k in self.db.passkeys(self.db.user("vera")["id"])]

    def test_without_a_password(self):
        for rk in (True, None):
            with self.subTest(credProps=rk):
                _, answer = self.add_key(passwordless=True, rk=rk)
                self.assertEqual(answer.status, 200, answer.body)
                self.assertEqual(answer.json()["passwordless"], True)
        # asked with "required", a key that registered keeps the sign-in
        # itself, whether or not the browser says so in credProps
        self.assertEqual(self.stored(), [(1, 1), (1, 1)])

    def test_only_after_the_password(self):
        for rk in (True, False, None):
            with self.subTest(credProps=rk):
                _, answer = self.add_key(passwordless=False, rk=rk)
                self.assertEqual(answer.json()["passwordless"], False)
        # asked with "preferred", silence is not a yes (v0.7.6 read it as
        # one): unknown stays unknown
        self.assertEqual(self.stored(), [(0, 1), (0, 0), (0, None)])

    def test_a_key_that_cannot_is_refused_not_downgraded(self):
        # asked to keep the sign-in and ask for a PIN, a key that says it
        # did neither is not quietly turned into a second factor: the
        # person is told to untick
        for kwargs in ({"uv": False}, {"rk": False}):
            with self.subTest(**kwargs):
                _, answer = self.add_key(passwordless=True, **kwargs)
                self.refused(answer, "cannot_passwordless")

    def test_the_first_key_brings_recovery_codes(self):
        _, answer = self.add_key()
        codes = answer.json()["recovery"]
        self.assertGreater(len(codes), 0)
        self.assertEqual(auth.recovery_left(self.db.user("vera")["recovery"]),
                         len(codes))
        _, answer = self.add_key()
        self.assertIsNone(answer.json()["recovery"])

    def test_journaled(self):
        self.add_key(label="blue key")
        event = next(e for e in server.db.journal(5)
                     if e["event"] == "user_passkey_added")
        self.assertEqual(event["user"], "vera")
        self.assertEqual(json.loads(event["details"])["label"], "blue key")

    def test_shown_in_the_account(self):
        cookie = self.cookie("vera")
        self.add_key(label="blue key", cookie=cookie)
        (key,) = self.me(cookie)["keys"]
        self.assertEqual((key["number"], key["label"], key["passwordless"]),
                         (1, "blue key", False))
        self.assertNotIn("credential_id", key)
        self.assertNotIn("public_key", key)

    def test_at_most_ten(self):
        for n in range(PASSKEYS_MAX):
            self.add_key(label=str(n))
        answer = self.begin_add(self.cookie("vera"))
        self.assertEqual(answer.json()["error"], "too_many_keys")

    def test_keys_off(self):
        server.sign_in.webauthn.why = "no_fido2"
        answer = self.begin_add(self.cookie("vera"))
        self.assertEqual(answer.status, 409)
        self.assertEqual(answer.json(), {"error": "passkeys_off",
                                         "why": "no_fido2"})

    def test_an_administrator_without_a_factor_may_add_one(self):
        self.db.add_user("olga", "admin", auth.hash_password(PASSWORD))
        cookie = self.cookie("olga", second_factor=False)
        self.assertEqual(self.me(cookie)["step"], "totp")
        _, answer = self.add_key("olga", cookie=cookie)
        self.assertEqual(answer.status, 200, answer.body)
        self.assertIsNone(self.me(cookie)["step"])




class AddedBeforeV077Test(KeysCase):
    """"Without a password" given to a key that never said it keeps the
    sign-in: shown for what it is, and left alone in the database."""

    def setUp(self):
        super().setUp()
        vera = self.db.user("vera")["id"]
        self.db.add_passkey(vera, a_key(1, label="old", passwordless=1,
                                        discoverable=None))
        self.db.add_passkey(vera, a_key(2, label="sure", passwordless=1,
                                        discoverable=1))
        self.db.add_passkey(vera, a_key(3, label="second", passwordless=0,
                                        discoverable=None))

    def test_the_account_says_so(self):
        keys = self.me(self.cookie("vera"))["keys"]
        self.assertEqual([(k["label"], k["passwordless"], k["unconfirmed"])
                          for k in keys],
                         [("old", True, True), ("sure", True, False),
                          ("second", False, False)])

    def test_the_database_is_not_rewritten(self):
        self.me(self.cookie("vera"))
        rows = self.db.passkeys(self.db.user("vera")["id"])
        self.assertEqual([(k["passwordless"], k["discoverable"]) for k in rows],
                         [(1, None), (1, 1), (0, None)])

    def test_the_console_says_so(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "moonlan.db"
            config = Path(folder) / "config.yaml"
            config.write_text(f"db_path: {path}\n", encoding="utf-8")
            db = Database(path)
            vera = db.add_user("vera", "user", "scrypt$x")
            db.add_passkey(vera["id"], a_key(1, label="old", passwordless=1,
                                             discoverable=None))
            db.close()
            out = io.StringIO()
            with mock.patch.dict(os.environ, {"MOONLAN_CONFIG": str(config)}), \
                    contextlib.redirect_stdout(out):
                os.environ.pop("MOONLAN_DEMO", None)
                self.assertEqual(users.main(["passkeys", "vera"]), 0)
        self.assertIn("NOT confirmed by the key", out.getvalue())
        self.assertIn("remove it and add it again", out.getvalue())


# ---------- signing in with a key ----------

class SignInCase(KeysCase):
    def begin(self, ticket="", **kwargs):
        return self.post("/api/auth/passkey/begin", {"ticket": ticket},
                         **kwargs)

    def finish(self, request, credential, **kwargs):
        return self.post("/api/auth/passkey/finish",
                         {"request": request, "credential": credential},
                         **kwargs)

    def alone(self, key, client="10.0.0.99", origin=ORIGIN, **answer):
        """Signs in with the key alone. The answer."""
        begun = self.begin(client=client).json()
        credential = soft.get(begun["options"], origin, key, **answer)
        return self.finish(begun["request"], credential, client=client)

    def password(self, name="vera", client="10.0.0.99"):
        answer = self.post("/api/auth/login",
                           {"name": name, "password": PASSWORD},
                           client=client)
        self.assertEqual(answer.status, 200, answer.body)
        return answer.json()

    def after_password(self, key, name="vera", client="10.0.0.99",
                       **answer):
        ticket = self.password(name, client)["ticket"]
        begun = self.begin(ticket, client=client).json()
        credential = soft.get(begun["options"], ORIGIN, key, **answer)
        return self.finish(begun["request"], credential, client=client)

    def logins(self):
        return [json.loads(e["details"]) for e in server.db.journal(20)
                if e["event"] == "login"]


class SignInWithKeyTest(SignInCase):
    def test_alone_es256_and_eddsa(self):
        for alg in (-7, -8):
            with self.subTest(alg=alg):
                key, _ = self.add_key(alg=alg, passwordless=True)
                answer = self.alone(key)
                self.assertEqual(answer.status, 200, answer.body)
                header = answer.header("set-cookie")[0]
                self.assertTrue(header.startswith("__Host-moonlan_session="))
                self.assertEqual(self.logins()[0]["method"], "passkey")

    def test_the_options_alone(self):
        options = self.begin().json()["options"]
        self.assertEqual(options["rpId"], "example.local")
        self.assertEqual(options["allowCredentials"], [])
        self.assertEqual(options["userVerification"], "required")
        self.assertEqual(len(websafe_decode(options["challenge"])), 32)

    def test_after_the_password(self):
        key, _ = self.add_key(passwordless=False)
        step = self.password()
        self.assertEqual(step["status"], "code_required")
        self.assertEqual(step["methods"], ["passkey", "recovery"])
        begun = self.begin(step["ticket"]).json()
        options = begun["options"]
        self.assertEqual(
            [websafe_decode(c["id"]) for c in options["allowCredentials"]],
            [key.credential_id],
        )
        self.assertEqual(options["userVerification"], "discouraged")
        # the password was the thing known: no PIN needed now
        answer = self.finish(begun["request"],
                             soft.get(options, ORIGIN, key, uv=False))
        self.assertEqual(answer.status, 200, answer.body)
        self.assertEqual(self.logins()[0]["method"], "password+passkey")

    def test_a_password_alone_is_not_enough_once_there_is_a_key(self):
        self.add_key(passwordless=True)
        self.assertEqual(self.password()["status"], "code_required")

    def test_alone_without_user_verification(self):
        key, _ = self.add_key(passwordless=True)
        answer = self.alone(key, uv=False)
        self.assertEqual(answer.status, 401)
        self.assertEqual(answer.json()["error"], "no_user_verification")

    def test_a_second_factor_key_does_not_sign_in_alone(self):
        key, _ = self.add_key(passwordless=False)
        answer = self.alone(key)
        self.assertEqual(answer.json()["error"], "key_second_only")

    def test_another_origin(self):
        key, _ = self.add_key(passwordless=True)
        for origin in ("https://example.local.evil.test",
                       "http://example.local:8443",
                       "https://example.local"):
            with self.subTest(origin=origin):
                answer = self.alone(key, origin=origin)
                self.assertEqual(answer.json()["error"], "key_refused")

    def test_another_rp_id_hash(self):
        key, _ = self.add_key(passwordless=True)
        answer = self.alone(key, rp_id="evil.test")
        self.assertEqual(answer.json()["error"], "key_refused")

    def test_a_challenge_works_once(self):
        key, _ = self.add_key(passwordless=True)
        begun = self.begin().json()
        credential = soft.get(begun["options"], ORIGIN, key)
        self.assertEqual(self.finish(begun["request"], credential).status, 200)
        again = self.finish(begun["request"], credential)
        self.assertEqual(again.json()["error"], "challenge_expired")
        # the same answer under a fresh challenge: signed over another
        fresh = self.begin().json()
        replayed = self.finish(fresh["request"], credential)
        self.assertEqual(replayed.json()["error"], "key_refused")

    def test_a_challenge_lasts_five_minutes(self):
        key, _ = self.add_key(passwordless=True)
        begun = self.begin().json()
        credential = soft.get(begun["options"], ORIGIN, key)
        later = time.time() + passkeys.CHALLENGE_SECONDS + 1
        with mock.patch.object(passkeys.time, "time", return_value=later):
            answer = self.finish(begun["request"], credential)
        self.assertEqual(answer.json()["error"], "challenge_expired")

    def test_a_key_the_service_does_not_know(self):
        stranger = soft.SoftKey(-7, "example.local", b"x" * 32)
        answer = self.alone(stranger)
        self.assertEqual(answer.json()["error"], "key_unknown")

    def test_whose_key_it_is(self):
        vera, _ = self.add_key("vera", passwordless=True)
        # a key that claims another account's handle
        vera.user_handle = self.db.webauthn_user_id(
            self.db.user("anton")["id"]
        )
        self.assertEqual(self.alone(vera).json()["error"], "key_refused")
        # after vera's password, anton's key
        self.db.reset_totp("anton")
        anton, _ = self.add_key("anton", passwordless=False)
        answer = self.after_password(anton, "vera", any_key=True)
        self.assertEqual(answer.json()["error"], "key_refused")

    def test_the_ticket_runs_out_for_a_key_too(self):
        key, _ = self.add_key(passwordless=False)
        ticket = self.password()["ticket"]
        begun = self.begin(ticket).json()
        for entry in server.sign_in._tickets.values():
            entry.expires = time.time() - 1
        answer = self.finish(begun["request"],
                             soft.get(begun["options"], ORIGIN, key))
        self.assertEqual(answer.json()["error"], "ticket_expired")

    def test_a_recovery_code_instead_of_the_key(self):
        _, added = self.add_key(passwordless=False)
        code = added.json()["recovery"][0]
        ticket = self.password()["ticket"]
        answer = self.post("/api/auth/totp", {"ticket": ticket, "code": code})
        self.assertEqual(answer.status, 200, answer.body)
        self.assertEqual(self.logins()[0]["method"], "password+recovery")

    def test_keys_off(self):
        key, _ = self.add_key(passwordless=False)
        server.sign_in.webauthn.why = "no_fido2"
        self.assertEqual(self.begin().json()["error"], "passkeys_off")
        # the second step then offers what is left
        self.assertEqual(self.password()["methods"], ["recovery"])

    def test_an_administrator_with_a_key_only(self):
        self.db.reset_totp("anton")
        key, _ = self.add_key("anton", passwordless=True)
        # the password alone does not do
        self.assertEqual(self.password("anton")["status"], "code_required")
        answer = self.alone(key)
        cookie = answer.header("set-cookie")[0].split(";", 1)[0]
        self.assertIsNone(self.me(cookie)["step"])


class CounterTest(SignInCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(self.clear_alarms)
        self.key, _ = self.add_key(passwordless=True)

    def clear_alarms(self):
        with server.db._lock, server.db._conn:
            server.db._conn.execute(
                "DELETE FROM alarms WHERE type = 'passkey_clone_suspected'"
            )
        server.alarm_engine._active = {
            a for a in server.alarm_engine._active
            if a[0] != "passkey_clone_suspected"
        }

    def clone_alarms(self):
        return [a for a in server.db.alarms(True, 100)
                if a["type"] == "passkey_clone_suspected"]

    def test_counting_up(self):
        for counter in (1, 2, 7):
            self.assertEqual(self.alone(self.key, counter=counter).status, 200)
        stored = self.db.passkeys(self.db.user("vera")["id"])[0]
        self.assertEqual(stored["sign_count"], 7)
        self.assertGreater(stored["last_used"], 0)

    def test_going_back_is_refused_and_raises_the_alarm(self):
        self.assertEqual(self.alone(self.key, counter=5).status, 200)
        for counter in (5, 3):
            with self.subTest(counter=counter):
                answer = self.alone(self.key, counter=counter)
                self.assertEqual(answer.status, 401)
                self.assertEqual(answer.json()["error"], "key_refused")
        (alarm,) = self.clone_alarms()
        self.assertEqual(alarm["severity"], "critical")
        self.assertIn("vera", alarm["subject"])
        self.assertIn("remove-passkey vera 1", alarm["message"])
        # the panel shows it as it is, not as "switch: port"
        shown = call(server.app, "GET", "/api/alarms",
                     cookie=self.cookie("anton"), scheme="https",
                     host=HOST).json()["alarms"]
        shown = next(a for a in shown
                     if a["type"] == "passkey_clone_suspected")
        self.assertEqual((shown["display"], shown["port"]),
                         (alarm["subject"], ""))
        events = [e for e in server.db.journal(20)
                  if e["event"] == "passkey_clone_suspected"]
        self.assertEqual(len(events), 2)
        self.assertEqual(json.loads(events[-1]["details"])["counter"], 5)

    def test_a_key_that_does_not_count(self):
        for _ in range(3):
            self.assertEqual(self.alone(self.key, counter=0).status, 200)
        self.assertEqual(self.clone_alarms(), [])

    def test_zero_after_counting_is_taken_and_the_count_kept(self):
        self.assertEqual(self.alone(self.key, counter=4).status, 200)
        self.assertEqual(self.alone(self.key, counter=0).status, 200)
        stored = self.db.passkeys(self.db.user("vera")["id"])[0]
        self.assertEqual(stored["sign_count"], 4)
        self.assertEqual(self.alone(self.key, counter=3).status, 401)


class KeyFailuresTest(SignInCase):
    def test_they_slow_the_address_down(self):
        key, _ = self.add_key(passwordless=True)
        for _ in range(signin.ADDRESS_FREE_FAILURES):
            self.alone(key, origin="https://evil.test")
        answer = self.begin()
        self.assertEqual(answer.status, 429)
        self.assertEqual(answer.json()["error"], "too_many_attempts")

    def test_they_lock_the_account(self):
        key, _ = self.add_key(passwordless=True)
        for n in range(signin.ACCOUNT_LOCK_FAILURES):
            answer = self.alone(key, origin="https://evil.test",
                                client=f"10.0.1.{n}")
        self.assertEqual(answer.status, 423)
        # and the lock holds for the key as for the password
        self.assertEqual(self.alone(key, client="10.0.2.1").status, 423)

    def test_an_unknown_key_counts_for_the_address(self):
        stranger = soft.SoftKey(-7, "example.local", b"x" * 32)
        for _ in range(signin.ADDRESS_FREE_FAILURES):
            self.alone(stranger)
        self.assertEqual(self.begin().status, 429)



# ---------- an administrator's second factor ----------

class SecondFactorTest(SignInCase):
    """TOTP or a key; never neither, unless reset on purpose."""

    def setUp(self):
        super().setUp()
        self.db.add_user("olga", "admin", auth.hash_password(PASSWORD))
        self.olga, _ = self.add_key("olga", passwordless=True,
                                    cookie=self.cookie("olga", False))

    def olga_keys(self):
        return self.db.passkeys(self.db.user("olga")["id"])

    def test_her_last_key_stays_in_the_browser(self):
        cookie = self.cookie("olga")
        (key,) = self.olga_keys()
        answer = call(server.app, "DELETE", f"/api/auth/passkeys/{key['id']}",
                      cookie=cookie, scheme="https", host=HOST)
        self.assertEqual(answer.status, 409)
        self.assertEqual(answer.json()["error"], "last_factor")
        # another administrator cannot take it either
        answer = call(server.app, "DELETE",
                      f"/api/users/olga/passkeys/{key['id']}",
                      cookie=self.cookie("anton"), scheme="https", host=HOST)
        self.assertEqual(answer.json()["error"], "last_factor")
        self.assertEqual(len(self.olga_keys()), 1)

    def test_with_a_second_key_one_may_go(self):
        cookie = self.cookie("olga")
        self.add_key("olga", cookie=cookie)
        first = self.olga_keys()[0]
        answer = call(server.app, "DELETE",
                      f"/api/auth/passkeys/{first['id']}",
                      cookie=cookie, scheme="https", host=HOST)
        self.assertEqual(answer.status, 200, answer.body)
        self.assertEqual(len(self.olga_keys()), 1)
        event = server.db.journal(1)[0]
        self.assertEqual(event["event"], "user_passkey_removed")

    def test_nobody_elses_key(self):
        (key,) = self.olga_keys()
        answer = call(server.app, "DELETE", f"/api/auth/passkeys/{key['id']}",
                      cookie=self.cookie("vera"), scheme="https", host=HOST)
        self.assertEqual(answer.status, 404)

    def test_a_reset_leaves_the_binding_as_the_first_thing(self):
        answer = self.post("/api/users/olga/reset-passkeys", {},
                           self.cookie("anton"))
        self.assertEqual(answer.status, 200, answer.body)
        self.assertEqual(answer.json()["removed"], 1)
        self.assertEqual(self.olga_keys(), [])
        # she signs in with the password alone — to bind a new factor
        answer = self.post("/api/auth/login",
                           {"name": "olga", "password": PASSWORD})
        cookie = answer.header("set-cookie")[0].split(";", 1)[0]
        self.assertEqual(self.me(cookie)["step"], "totp")
        refused = call(server.app, "GET", "/api/topology", cookie=cookie,
                       scheme="https", host=HOST)
        self.assertEqual(refused.json()["error"], "step_required")

    def test_the_users_panel_shows_the_keys(self):
        answer = call(server.app, "GET", "/api/users",
                      cookie=self.cookie("anton"), scheme="https", host=HOST)
        olga = next(u for u in answer.json()["users"] if u["name"] == "olga")
        self.assertEqual(len(olga["passkeys"]), 1)
        self.assertNotIn("credential_id", olga["passkeys"][0])
        vera = next(u for u in answer.json()["users"] if u["name"] == "vera")
        self.assertEqual(vera["passkeys"], [])

    def test_the_key_is_her_second_factor(self):
        # a session from the password alone is not hers any more
        answer = self.post("/api/auth/login",
                           {"name": "olga", "password": PASSWORD})
        self.assertEqual(answer.json()["status"], "code_required")
        answer = self.after_password(self.olga, "olga")
        cookie = answer.header("set-cookie")[0].split(";", 1)[0]
        self.assertIsNone(self.me(cookie)["step"])


class ConsoleKeysTest(unittest.TestCase):
    """python -m moonlan.users passkeys / remove-passkey /
    reset-passkeys, against a database of its own."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "moonlan.db"
        config = Path(folder.name) / "config.yaml"
        config.write_text(f"db_path: {self.path}\n", encoding="utf-8")
        patch = mock.patch.dict(os.environ, {"MOONLAN_CONFIG": str(config)})
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop("MOONLAN_DEMO", None)
        self.db = Database(self.path)
        self.addCleanup(self.db.close)
        self.olga = self.db.add_user("olga", "admin", "scrypt$x")
        self.db.add_passkey(self.olga["id"], a_key(1, label="blue"))
        self.db.add_passkey(self.olga["id"], a_key(2, label="red",
                                                   passwordless=1))
        self.db.add_session("s", self.olga["id"], time.time(), True)

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = users.main(list(argv))
        return code, out.getvalue()

    def test_the_list(self):
        code, out = self.run_cli("passkeys", "olga")
        self.assertEqual(code, 0, out)
        self.assertIn(" 1. blue — a second factor after the password", out)
        self.assertIn(" 2. red — signs in without a password", out)
        code, out = self.run_cli("list")
        self.assertRegex(out, r"olga\s+admin\s+active\s+off\s+2 ")
        self.assertNotIn("off!", out)

    def test_remove_one_then_not_the_last(self):
        code, out = self.run_cli("remove-passkey", "olga", "1")
        self.assertEqual(code, 0, out)
        self.assertIn("key 1 (blue) removed. 1 session(s) closed", out)
        code, out = self.run_cli("remove-passkey", "olga", "1")
        self.assertEqual(code, 1)
        self.assertIn("last second factor", out)
        self.assertIn("reset-passkeys olga", out)
        self.assertEqual(len(self.db.passkeys(self.olga["id"])), 1)

    def test_no_such_number(self):
        code, out = self.run_cli("remove-passkey", "olga", "7")
        self.assertEqual(code, 1)
        self.assertIn("no key with that number", out)

    def test_reset_all(self):
        code, out = self.run_cli("reset-passkeys", "olga")
        self.assertEqual(code, 0, out)
        self.assertIn("2 key(s) removed. 1 session(s) closed", out)
        self.assertIn("binds a new second factor at the next sign-in", out)
        self.assertEqual(self.db.passkeys(self.olga["id"]), [])
        events = [e["event"] for e in self.db.journal(5)]
        self.assertIn("user_passkeys_reset", events)
        code, out = self.run_cli("list")
        self.assertIn("off!", out)



# ---------- what the page says ----------

WEB = ROOT / "web"


def page_strings() -> dict[str, set[str]]:
    """The keys of each language in web/i18n.js."""
    text = (WEB / "i18n.js").read_text(encoding="utf-8")
    found = {}
    for lang in ("en", "ru"):
        body = re.search(rf"^  {lang}: {{$(.*?)^  }},$", text,
                         re.S | re.M).group(1)
        found[lang] = set(re.findall(r'^    "?([\w+.-]+)"?:', body, re.M))
    return found


class PageTest(unittest.TestCase):
    """Every reason and every refusal has words, in both languages."""

    def setUp(self):
        self.strings = page_strings()

    def assertWords(self, key):
        for lang in ("en", "ru"):
            self.assertIn(key, self.strings[lang], f"{lang}: {key}")

    def test_why_a_key_cannot_be_used(self):
        # the page's own reasons, and every one the server can give
        for code in ("insecure", "elsewhere", "no_webauthn", "no_public_url",
                     "public_url_ip", "public_url_http", "no_fido2"):
            self.assertWords(f"passkeyWhy_{code}")
        for public in ("", "https://10.0.0.5", "http://example.local",
                       "https://example.local"):
            parsed, _ = https.parse_public_url(public)
            code = passkeys.unavailable(parsed)
            if code:
                self.assertWords(f"passkeyWhy_{code}")

    def test_the_reasons_come_in_this_order(self):
        # a secure page, the address, the server's reason, the browser
        body = (WEB / "app.js").read_text(encoding="utf-8")
        body = body[body.index("function keyUnavailable()"):]
        body = body[:body.index("\n}\n")]
        order = [body.index(mark) for mark in (
            '"passkeyWhy_insecure"', '"passkeyWhy_elsewhere"',
            '"passkeyWhy_" + info.why', '"passkeyWhy_no_webauthn"',
        )]
        self.assertEqual(order, sorted(order))

    def test_what_the_browser_throws(self):
        for name in ("NotAllowedError", "InvalidStateError", "SecurityError",
                     "NotSupportedError", "AbortError", "ConstraintError",
                     "other"):
            self.assertWords(f"keyErr_{name}")

    def test_every_refusal_of_the_key_routes(self):
        source = "\n".join(
            (ROOT / "moonlan" / name).read_text(encoding="utf-8")
            for name in ("signin.py", "passkeys.py", "db.py")
        )
        codes = set(re.findall(r'PasskeyError\(\s*"(\w+)"', source))
        codes |= set(re.findall(r'AccountError\("(\w+)"', source))
        codes |= {"passkeys_off", "key_unknown", "key_second_only",
                  "no_keys", "wrong_password", "https_required"}
        self.assertIn("no_user_verification", codes)
        for code in sorted(codes):
            self.assertWords(f"err_{code}")

    def test_the_alarm_and_the_journal(self):
        self.assertWords("al_passkey_clone_suspected")
        for event in ("user_passkey_added", "user_passkey_removed",
                      "user_passkeys_reset", "passkey_clone_suspected"):
            self.assertWords(f"ev_{event}")


if __name__ == "__main__":
    unittest.main()
