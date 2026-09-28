"""HTTPS (v0.7.6): the public address, the certificate, the proxy,
the cookie and the headers."""

import asyncio
import contextlib
import datetime
import io
import logging
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

import service_fixture  # noqa: F401  (sets MOONLAN_CONFIG first)
from asgi_client import call
from moonlan import auth, config as config_module, diag, https, server, signin
from moonlan import passkeys as passkeys_module

try:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
except ImportError:  # the service runs without it; these tests do not
    x509 = None


class PublicUrlTest(unittest.TestCase):
    def parse(self, text):
        return https.parse_public_url(text)

    def test_a_name_with_a_port(self):
        public, problems = self.parse("https://Example.LOCAL:8443/")
        self.assertEqual(problems, [])
        self.assertEqual(public.origin, "https://example.local:8443")
        self.assertEqual(public.rp_id, "example.local")
        self.assertTrue(public.secure)
        self.assertIsNone(https.passkey_problem(public))

    def test_the_default_port_is_not_part_of_the_origin(self):
        # a browser sends https://example.local, never ...:443
        public, _ = self.parse("https://example.local:443")
        self.assertEqual(public.origin, "https://example.local")
        public, _ = self.parse("http://example.local:80")
        self.assertEqual(public.origin, "http://example.local")

    def test_not_set(self):
        self.assertEqual(self.parse(""), (None, []))
        self.assertEqual(https.passkey_problem(None), "no_public_url")

    def test_an_address_is_not_a_relying_party(self):
        public, problems = self.parse("https://10.0.0.5:8443")
        self.assertEqual(problems, [])
        self.assertTrue(public.is_ip)
        self.assertEqual(public.rp_id, "")
        self.assertEqual(https.passkey_problem(public), "public_url_ip")
        public, _ = self.parse("https://[fd00::5]:8443")
        self.assertEqual(public.origin, "https://[fd00::5]:8443")
        self.assertEqual(https.passkey_problem(public), "public_url_ip")

    def test_plain_http_carries_no_passkey_except_on_localhost(self):
        public, _ = self.parse("http://example.local:8080")
        self.assertFalse(public.secure)
        self.assertEqual(https.passkey_problem(public), "public_url_http")
        public, _ = self.parse("http://localhost:18511")
        self.assertIsNone(https.passkey_problem(public))

    def test_what_cannot_be_used_is_refused_with_a_reason(self):
        for text in ("ftp://example.local", "https://example.local/map",
                     "https://example.local/?a=1", "https://u:p@example.local",
                     "https://example.local:port", "https://", "example.local"):
            public, problems = self.parse(text)
            self.assertIsNone(public, text)
            self.assertTrue(problems, text)
            self.assertIn("listen.public_url", problems[0])


class StartupLineTest(unittest.TestCase):
    """One line at startup says what the address makes possible."""

    def line(self, text):
        public, problems = https.parse_public_url(text)
        with mock.patch.object(server, "public_url", public), \
                mock.patch.object(server, "public_url_problems", problems), \
                self.assertLogs("moonlan", "INFO") as logged:
            server._log_public_url()
        return "\n".join(logged.output)

    def test_each_case_says_what_follows(self):
        self.assertIn("not set", self.line(""))
        self.assertIn("passkeys are bound to example.local",
                      self.line("https://example.local:8443"))
        self.assertIn("is an IP address", self.line("https://10.0.0.5"))
        self.assertIn("plain http", self.line("http://example.local"))
        self.assertIn("ignored", self.line("https://example.local/map"))



# ---------- the certificate ----------



def make_certificate(folder, names=("example.local",), days=365):
    """A self-signed certificate and its key, PEM, in `folder`."""
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])]))
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test CA")]))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=days))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(n) for n in names]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = Path(folder) / "cert.pem"
    key_path = Path(folder) / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    os.chmod(key_path, 0o600)
    return str(cert_path), str(key_path), cert


@unittest.skipIf(x509 is None, "cryptography is not installed")
class CertificateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cert, self.key, self.x509 = make_certificate(
            self.tmp.name, ("example.local", "*.example.local")
        )

    def test_good_files_pass(self):
        https.check_tls_files(self.cert, self.key)

    def test_each_problem_is_one_sentence(self):
        cases = [
            ((self.cert, ""), "go together"),
            ((self.cert, self.key + ".nope"), "does not exist"),
        ]
        other = tempfile.mkdtemp(dir=self.tmp.name)
        _, other_key, _ = make_certificate(other)
        cases.append(((self.cert, other_key), "does not belong"))
        garbage = Path(self.tmp.name) / "garbage.pem"
        garbage.write_text("not a certificate")
        cases.append(((str(garbage), self.key), "not PEM"))
        locked = Path(self.tmp.name) / "locked.pem"
        locked.write_bytes(Path(self.key).read_bytes())
        os.chmod(locked, 0)
        if not os.access(locked, os.R_OK):   # root reads everything
            cases.append(((self.cert, str(locked)), "cannot be read"))
        for (cert, key), words in cases:
            with self.assertRaises(https.TlsProblem) as caught:
                https.check_tls_files(cert, key)
            self.assertIn(words, str(caught.exception), (cert, key))
            self.assertNotIn("\n", str(caught.exception))

    def test_run_py_exits_with_that_sentence(self):
        import run
        cfg = mock.Mock(listen_tls_cert=self.cert, listen_tls_key="/nope.pem")
        with self.assertRaises(SystemExit) as caught:
            run._tls_options(cfg)
        self.assertIn("does not exist", str(caught.exception.code))
        cfg = mock.Mock(listen_tls_cert=self.cert, listen_tls_key=self.key)
        self.assertEqual(run._tls_options(cfg), {
            "ssl_certfile": self.cert, "ssl_keyfile": self.key,
        })
        cfg = mock.Mock(listen_tls_cert="", listen_tls_key="")
        self.assertEqual(run._tls_options(cfg), {})

    def test_names_and_end_with_and_without_cryptography(self):
        info = https.read_certificate(self.cert)
        self.assertEqual(info.names, ("example.local", "*.example.local"))
        self.assertAlmostEqual(
            info.not_after, self.x509.not_valid_after_utc.timestamp(), delta=1
        )
        plain = https._read_certificate_stdlib(self.cert)
        self.assertEqual(plain.names, info.names)
        self.assertAlmostEqual(plain.not_after, info.not_after, delta=1)
        self.assertIn("example.local", plain.subject)

    def test_which_names_a_browser_accepts(self):
        names = ("example.local", "*.example.local")
        self.assertTrue(https.covers(names, "EXAMPLE.local"))
        self.assertTrue(https.covers(names, "map.example.local"))
        self.assertFalse(https.covers(names, "a.map.example.local"))
        self.assertFalse(https.covers(names, "local"))
        self.assertFalse(https.covers(names, "10.0.0.5"))

    def test_a_key_others_can_read(self):
        self.assertIsNone(https.key_mode_too_open(self.key))
        os.chmod(self.key, 0o644)
        self.assertEqual(https.key_mode_too_open(self.key), 0o644)


@unittest.skipIf(x509 is None, "cryptography is not installed")
class CertificateAtStartupTest(unittest.TestCase):
    """What the service says and raises about the certificate it serves."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        logger = logging.getLogger("moonlan")
        self.addCleanup(logger.setLevel, logger.level)
        logger.setLevel(logging.ERROR)   # a raised alarm is a log line
        with server.db._lock, server.db._conn:
            server.db._conn.execute("DELETE FROM alarms")
        server.alarm_engine._active.clear()

    def startup(self, days, names=("example.local",), public="https://example.local"):
        cert, key, _ = make_certificate(self.tmp.name, names, days)
        parsed, _ = https.parse_public_url(public)
        patches = [
            mock.patch.object(server.config, "listen_tls_cert", cert),
            mock.patch.object(server.config, "listen_tls_key", key),
            mock.patch.object(server, "public_url", parsed),
            mock.patch.object(server, "certificate", None),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        with self.assertLogs("moonlan", "INFO") as logged:
            server._log_certificate()
        return "\n".join(logged.output)

    def active(self):
        return [row["type"] for row in server.db.alarms(True, 100)]

    def test_a_good_certificate(self):
        text = self.startup(365)
        self.assertIn("valid until", text)
        self.assertNotIn("WARNING", text)
        asyncio.run(server.check_certificate_end())
        self.assertEqual(self.active(), [])

    def test_ending_soon(self):
        text = self.startup(20)
        self.assertRegex(text, r"WARNING.*ends on .* in (19|20) day")
        asyncio.run(server.check_certificate_end())
        self.assertEqual(self.active(), [])      # 20 days: a warning, no alarm
        text = self.startup(10)
        asyncio.run(server.check_certificate_end())
        self.assertEqual(self.active(), ["tls_cert_expiring"])
        message = server.db.alarms(True, 1)[0]["message"]
        self.assertIn("restart MoonLan", message)
        # replaced and restarted: a process serving a good one clears it
        self.startup(365)
        asyncio.run(server.check_certificate_end())
        self.assertEqual(self.active(), [])

    def test_a_certificate_for_another_name(self):
        text = self.startup(365, names=("other.example.local",))
        self.assertIn("does not name example.local", text)

    def test_a_key_others_can_read(self):
        self.startup(365)
        os.chmod(server.config.listen_tls_key, 0o640)
        with self.assertLogs("moonlan", "INFO") as logged:
            server._log_certificate()
        self.assertTrue(any("chmod 600" in line for line in logged.output))


class RedirectTest(unittest.TestCase):
    def ask(self, request: bytes) -> bytes:
        async def go():
            listener = await https.serve_redirect(
                "127.0.0.1", 0, "https://example.local:8443"
            )
            port = listener.sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(request)
            await writer.drain()
            answer = await reader.read()
            writer.close()
            listener.close()
            await listener.wait_closed()
            return answer
        return asyncio.run(go())

    def test_same_path_at_the_public_address(self):
        answer = self.ask(b"GET /index.html?x=1 HTTP/1.1\r\nHost: a\r\n\r\n")
        self.assertTrue(answer.startswith(b"HTTP/1.1 307"))
        self.assertIn(b"Location: https://example.local:8443/index.html?x=1\r\n",
                      answer)

    def test_nothing_else_gets_into_location(self):
        for target in (b"//evil.example/", b"http://evil.example/", b"/\x7fx"):
            answer = self.ask(b"GET " + target + b" HTTP/1.1\r\n\r\n")
            self.assertIn(b"Location: https://example.local:8443/\r\n", answer)


# ---------- behind a proxy ----------


PASSWORD = "correct horse battery"
PROXY = "127.0.0.1"


class ProxyTest(unittest.TestCase):
    """nginx holds the TLS at https://example.local and forwards to
    MoonLan over http from 127.0.0.1."""

    def setUp(self):
        logger = logging.getLogger("moonlan")
        self.addCleanup(logger.setLevel, logger.level)
        logger.setLevel(logging.ERROR)
        self.db = server.accounts
        self.clean()
        self.addCleanup(self.clean)
        server.sign_in.throttle = signin.Throttle()
        self.db.add_user("anton", "admin", auth.hash_password(PASSWORD))
        self.db.enable_totp("anton", auth.new_totp_secret(), "")
        self.db.add_user("vera", "user", auth.hash_password(PASSWORD))
        token = auth.new_token()
        self.db.add_session(auth.token_hash(token),
                            self.db.user("vera")["id"], time.time())
        # the session under both names: which one counts follows the
        # scheme MoonLan sees, and that is what these tests are about
        self.cookie = (f"{signin.SESSION_COOKIE}={token}; "
                       f"{signin.SECURE_SESSION_COOKIE}={token}")
        # as run.py sets it up with listen.trusted_proxies: [127.0.0.1]
        self.app = ProxyHeadersMiddleware(server.app, trusted_hosts=[PROXY])

    def clean(self):
        with self.db._lock, self.db._conn:
            self.db._conn.execute("DELETE FROM sessions")
            self.db._conn.execute("DELETE FROM users")

    def pin(self, app, client, headers, origin="https://example.local",
            host="example.local"):
        return call(app, "PATCH", "/api/layout", cookie=self.cookie,
                    json_body={"nodes": {}}, origin=origin, client=client,
                    host=host, headers=headers).status

    def test_the_forwarded_scheme_from_the_proxy(self):
        forwarded = {"x-forwarded-proto": "https", "x-forwarded-for": "192.0.2.10"}
        # v0.7.5's failure: without the proxy headers MoonLan sees http
        self.assertEqual(self.pin(server.app, PROXY, forwarded), 403)
        self.assertEqual(self.pin(self.app, PROXY, forwarded), 200)

    def test_the_same_header_from_anybody_else_changes_nothing(self):
        forwarded = {"x-forwarded-proto": "https"}
        self.assertEqual(self.pin(self.app, "10.0.0.50", forwarded), 403)

    def test_the_public_address_is_the_map(self):
        public, _ = https.parse_public_url("https://example.local")
        with mock.patch.object(server.sign_in, "public_url", public):
            # a proxy that does not say https: the public address decides
            self.assertEqual(self.pin(server.app, PROXY, {}), 200)
            # the map opened by address keeps working with a password
            self.assertEqual(self.pin(
                server.app, "10.0.0.50", {}, origin="http://10.0.0.5:8080",
                host="10.0.0.5:8080"), 200)
            # a page elsewhere does not
            self.assertEqual(self.pin(
                server.app, "10.0.0.50", {}, origin="https://evil.example"), 403)

    def login(self, client, forwarded_for=None, password="wrong password",
              origin="https://example.local"):
        headers = {"x-forwarded-proto": "https"}
        if forwarded_for:
            headers["x-forwarded-for"] = forwarded_for
        return call(self.app, "POST", "/api/auth/login",
                    json_body={"name": "vera", "password": password},
                    origin=origin, host="example.local",
                    client=client, headers=headers).status

    def test_the_delay_is_kept_per_real_client(self):
        for _ in range(5):
            self.assertEqual(self.login(PROXY, "192.0.2.10"), 401)
        # the sixth attempt from that person waits…
        self.assertEqual(self.login(PROXY, "192.0.2.10"), 429)
        # …and somebody else behind the same proxy does not
        self.assertEqual(self.login(PROXY, "192.0.2.11", PASSWORD), 200)

    def test_nobody_else_can_claim_another_address(self):
        # a client that is not the proxy: its X-Forwarded-Proto is
        # ignored (so its own origin is http) and so is X-Forwarded-For —
        # rotating it does not escape the delay
        own = "http://example.local"
        for n in range(5):
            self.assertEqual(
                self.login("10.0.0.50", f"192.0.2.{20 + n}", origin=own), 401
            )
        self.assertEqual(self.login("10.0.0.50", "192.0.2.99", origin=own), 429)


class TrustedProxiesTest(unittest.TestCase):
    def test_what_is_trusted(self):
        trusted, problems = https.parse_trusted_proxies(
            ["127.0.0.1", "10.0.0.0/24", "::1", "*", "nginx"]
        )
        self.assertEqual(trusted, ["127.0.0.1", "10.0.0.0/24", "::1"])
        self.assertEqual(len(problems), 2)
        self.assertIn("any client", problems[0])

    def test_nobody_trusted_switches_the_headers_off(self):
        # uvicorn trusts 127.0.0.1 by default; MoonLan does not
        self.assertEqual(https.uvicorn_proxy_options([]),
                         {"proxy_headers": False})
        self.assertEqual(https.uvicorn_proxy_options(["127.0.0.1"]),
                         {"proxy_headers": True,
                          "forwarded_allow_ips": ["127.0.0.1"]})


# ---------- the cookie, the password over http, HSTS ----------

PUBLIC_HOST = "example.local:8443"
PUBLIC = "https://" + PUBLIC_HOST


def session_header(answer):
    """The Set-Cookie line that carries a session, whatever its name."""
    for header in answer.header("set-cookie"):
        name = header.split("=", 1)[0]
        if name in (signin.SESSION_COOKIE, signin.SECURE_SESSION_COOKIE) \
                and "max-age=0" not in header.lower():
            return header
    return None


class SecureCookieCase(unittest.TestCase):
    def setUp(self):
        logger = logging.getLogger("moonlan")
        self.addCleanup(logger.setLevel, logger.level)
        logger.setLevel(logging.ERROR)
        self.db = server.accounts
        self.clean()
        self.addCleanup(self.clean)
        server.sign_in._tickets.clear()
        server.sign_in.throttle = signin.Throttle()
        self.db.add_user("anton", "admin", auth.hash_password(PASSWORD))
        self.db.enable_totp("anton", auth.new_totp_secret(), "")
        self.db.add_user("vera", "user", auth.hash_password(PASSWORD))

    def clean(self):
        with self.db._lock, self.db._conn:
            self.db._conn.execute("DELETE FROM sessions")
            self.db._conn.execute("DELETE FROM users")

    def public(self, text):
        public, _ = https.parse_public_url(text)
        patch = mock.patch.object(server.sign_in, "public_url", public)
        patch.start()
        self.addCleanup(patch.stop)

    def login(self, name="vera", **kwargs):
        return call(server.app, "POST", "/api/auth/login",
                    json_body={"name": name, "password": PASSWORD}, **kwargs)

    def over_https(self):
        return {"scheme": "https", "host": PUBLIC_HOST}


class SecureCookieTest(SecureCookieCase):
    def test_over_https_the_cookie_is_secure_and_host_only(self):
        answer = self.login(**self.over_https())
        self.assertEqual(answer.status, 200, answer.body)
        header = session_header(answer)
        self.assertTrue(header.startswith("__Host-moonlan_session="), header)
        parts = [p.strip().lower() for p in header.split(";")]
        self.assertIn("secure", parts)
        self.assertIn("path=/", parts)
        self.assertIn("httponly", parts)
        self.assertIn("samesite=strict", parts)
        # __Host- is refused by the browser with a Domain
        self.assertFalse(any(p.startswith("domain=") for p in parts))

    def test_the_second_step_sets_it_too(self):
        ticket = self.login("anton", **self.over_https()).json()["ticket"]
        code = auth.totp_code(
            auth.secret_bytes(self.db.user("anton")["totp_secret"]),
            time.time(),
        )
        answer = call(server.app, "POST", "/api/auth/totp",
                      json_body={"ticket": ticket, "code": code},
                      **self.over_https())
        self.assertEqual(answer.status, 200, answer.body)
        self.assertTrue(session_header(answer).startswith("__Host-"))

    def test_over_http_nothing_changes(self):
        header = session_header(self.login())
        self.assertTrue(header.startswith("moonlan_session="), header)
        self.assertNotIn("secure", header.lower().replace("samesite", ""))

    def test_each_cookie_counts_only_where_it_belongs(self):
        token = session_header(self.login(**self.over_https())) \
            .split(";", 1)[0].split("=", 1)[1]
        me = lambda cookie, **kw: call(  # noqa: E731
            server.app, "GET", "/api/auth/me", cookie=cookie, **kw
        ).status
        self.assertEqual(me(f"__Host-moonlan_session={token}",
                            **self.over_https()), 200)
        # a cookie that crossed plain http is not carried over to https
        self.assertEqual(me(f"moonlan_session={token}",
                            **self.over_https()), 401)
        self.assertEqual(me(f"__Host-moonlan_session={token}"), 401)

    def test_signing_out_clears_both_names(self):
        answer = self.login(**self.over_https())
        cookie = session_header(answer).split(";", 1)[0]
        out = call(server.app, "POST", "/api/auth/logout", cookie=cookie,
                   **self.over_https())
        cleared = {h.split("=", 1)[0] for h in out.header("set-cookie")
                   if "max-age=0" in h.lower()}
        self.assertEqual(cleared, {"moonlan_session", "__Host-moonlan_session"})


class HttpsRequiredTest(SecureCookieCase):
    def test_no_password_over_http_when_the_address_is_https(self):
        self.public(PUBLIC)
        answer = self.login()
        self.assertEqual(answer.status, 403)
        self.assertEqual(answer.json(),
                         {"error": "https_required", "url": PUBLIC + "/"})
        self.assertIsNone(session_header(answer))
        self.assertEqual(self.login(**self.over_https()).status, 200)

    def test_nor_a_password_change_or_a_code(self):
        self.public(PUBLIC)
        session = session_header(self.login(**self.over_https())) \
            .split(";", 1)[0].split("=", 1)[1]
        # a session still works over http (it is the password that is
        # kept off the wire), but a new password is not taken there
        self.assertEqual(call(server.app, "GET", "/api/auth/me",
                              cookie=f"moonlan_session={session}").status, 200)
        answer = call(server.app, "POST", "/api/auth/password",
                      json_body={"current": PASSWORD, "new": "x" * 12},
                      cookie=f"moonlan_session={session}")
        self.assertEqual(answer.json()["error"], "https_required")
        answer = call(server.app, "POST", "/api/auth/totp",
                      json_body={"ticket": "t", "code": "123456"})
        self.assertEqual(answer.json()["error"], "https_required")

    def test_the_page_learns_where_to_go_before_typing(self):
        self.public(PUBLIC)
        answer = call(server.app, "GET", "/api/auth/me")
        self.assertEqual(answer.status, 401)
        self.assertEqual(answer.json()["https_url"], PUBLIC + "/")
        answer = call(server.app, "GET", "/api/auth/me", **self.over_https())
        self.assertNotIn("https_url", answer.json())

    def test_without_an_https_address_http_works_as_before(self):
        for text in ("", "http://localhost:8080"):
            with self.subTest(public_url=text):
                self.public(text)
                self.assertEqual(self.login().status, 200)
                self.assertNotIn(
                    "https_url", call(server.app, "GET", "/api/auth/me").json()
                )


class HstsTest(unittest.TestCase):
    def sts(self, app, scheme):
        answer = call(app, "GET", "/api/health", scheme=scheme)
        return answer.header("strict-transport-security")

    def test_off_by_default(self):
        self.assertEqual(config_module.Config().listen_hsts_max_age, 0)
        self.assertEqual(self.sts(server.app, "https"), [])

    def test_only_over_https(self):
        app = https.Hsts(server.app, 31536000)
        self.assertEqual(self.sts(app, "https"), ["max-age=31536000"])
        self.assertEqual(self.sts(app, "http"), [])

    def test_a_negative_age_reads_as_off(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.yaml"
            path.write_text("listen:\n  hsts_max_age: -5\n", encoding="utf-8")
            self.assertEqual(
                config_module.load_config(path).listen_hsts_max_age, 0
            )


class TransportTest(unittest.TestCase):
    def test_the_mode(self):
        public, _ = https.parse_public_url(PUBLIC)
        plain, _ = https.parse_public_url("http://example.local")
        mode = https.transport_mode
        self.assertEqual(mode("/etc/moonlan/tls/cert.pem", [], None), "tls")
        self.assertEqual(mode("", ["127.0.0.1"], public), "proxy")
        self.assertEqual(mode("", ["127.0.0.1"], None), "proxy")
        self.assertEqual(mode("", ["127.0.0.1"], plain), "http")
        self.assertEqual(mode("", [], public), "http")
        self.assertEqual(mode("", [], None), "http")

    def line(self, transport, public=None, hsts=0):
        public, _ = https.parse_public_url(public or "")
        with mock.patch.object(server, "transport", transport), \
                mock.patch.object(server, "public_url", public), \
                mock.patch.object(server.config, "listen_hsts_max_age", hsts), \
                self.assertLogs("moonlan", "INFO") as logged:
            server._log_transport()
        return "\n".join(logged.output)

    def test_one_line_at_startup(self):
        self.assertIn("served by MoonLan itself", self.line("tls"))
        self.assertIn("behind a reverse proxy", self.line("proxy"))
        self.assertIn("Connection: plain HTTP", self.line("http"))
        self.assertIn("HSTS: max-age=600", self.line("tls", hsts=600))

    def test_an_https_address_with_nothing_behind_it_is_called_out(self):
        self.assertIn("password is refused", self.line("http", PUBLIC))

    def test_the_clear_text_warning_only_over_http(self):
        db = server.accounts
        with db._lock, db._conn:
            db._conn.execute("DELETE FROM users")
        db.add_user("anton", "admin", auth.hash_password(PASSWORD))
        self.addCleanup(lambda: db._conn.execute("DELETE FROM users"))
        for transport, warned in (("http", True), ("tls", False),
                                  ("proxy", False)):
            with self.subTest(transport=transport):
                sign_in = signin.SignIn(db, server.db, server.config.auth,
                                        None, transport)
                with self.assertLogs("moonlan", "INFO") as logged:
                    sign_in.log_state()
                self.assertEqual(
                    any("clear text" in line for line in logged.output), warned
                )



# ---------- diag --config ----------

class DiagTest(unittest.TestCase):
    """What `diag --config` says about HTTPS and keys."""

    def section(self, listen=""):
        with tempfile.TemporaryDirectory() as folder:
            self.folder = folder
            path = Path(folder) / "config.yaml"
            path.write_text(
                f"db_path: {Path(folder) / 'test.db'}\n"
                + (f"listen:\n{listen(folder) if callable(listen) else listen}"
                   if listen else ""),
                encoding="utf-8",
            )
            cfg = config_module.load_config(path)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                diag._print_https(cfg)
        return out.getvalue()

    def test_nothing_set(self):
        text = self.section()
        self.assertIn("mode:            plain HTTP", text)
        self.assertIn("public address:  not set", text)
        self.assertIn("trusted proxies: none", text)
        self.assertIn("HSTS:            off", text)
        self.assertRegex(text, r"fido2:           2\.\d+\.\d+ \(cryptography ")
        self.assertIn("keys:            off (no_public_url)", text)

    def test_behind_a_proxy(self):
        text = self.section(
            "  public_url: https://example.local\n"
            "  trusted_proxies: [127.0.0.1, '*']\n"
            "  hsts_max_age: 600\n"
        )
        self.assertIn("HTTPS at a reverse proxy in front", text)
        self.assertIn("passkey RP ID:   example.local", text)
        self.assertIn("trusted proxies: 127.0.0.1", text)
        self.assertIn('"*" would let any client claim any address', text)
        self.assertIn("HSTS:            max-age=600", text)
        self.assertIn("keys:            on — bound to example.local", text)

    def test_an_ip_address(self):
        text = self.section("  public_url: https://10.0.0.5:8443\n")
        self.assertIn("passkey RP ID:   none — an IP address", text)
        self.assertIn("keys:            off (public_url_ip)", text)
        self.assertIn("neither tls_cert nor trusted_proxies", text)

    @unittest.skipIf(x509 is None, "cryptography is not installed")
    def test_the_certificate(self):
        def listen(folder):
            cert, key, _ = make_certificate(folder, ("moonlan.lan",), days=20)
            os.chmod(key, 0o644)
            return (f"  public_url: https://example.local:8443\n"
                    f"  tls_cert: {cert}\n  tls_key: {key}\n")
        text = self.section(listen)
        self.assertIn("HTTPS served by MoonLan itself", text)
        self.assertIn("subject:       CN=moonlan.lan", text)
        self.assertIn("names (SAN):   moonlan.lan", text)
        self.assertRegex(text, r"valid until:   \S+ — in (19|20) day\(s\) — "
                               r"replace it soon")
        self.assertIn("names the host: NO — browsers will refuse it at "
                      "example.local", text)
        self.assertIn("mode 644: readable by others, chmod 600", text)

    def test_a_certificate_that_will_not_start(self):
        text = self.section("  tls_cert: /nonexistent/cert.pem\n"
                            "  tls_key: /nonexistent/key.pem\n")
        self.assertIn("does not exist — MoonLan will not start", text)

    def test_without_fido2(self):
        with mock.patch.object(passkeys_module, "Fido2Server", None), \
                mock.patch.object(passkeys_module, "FIDO2_MISSING",
                                  "fido2 is not installed"):
            text = self.section("  public_url: https://example.local\n"
                                "  trusted_proxies: [127.0.0.1]\n")
        self.assertIn("fido2:           NOT INSTALLED — fido2 is not "
                      "installed; see docs/OPERATIONS.md#installing", text)
        self.assertIn("keys:            off (no_fido2)", text)


if __name__ == "__main__":
    unittest.main()
