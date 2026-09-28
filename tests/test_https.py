"""HTTPS (v0.7.6): the public address, the certificate, the proxy."""

import unittest
from unittest import mock

import service_fixture  # noqa: F401  (sets MOONLAN_CONFIG first)
from moonlan import https, server


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


if __name__ == "__main__":
    unittest.main()


# ---------- the certificate (task 2) ----------

import asyncio  # noqa: E402
import contextlib  # noqa: E402
import datetime  # noqa: E402
import io  # noqa: E402
import logging  # noqa: E402
import os  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

try:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
except ImportError:  # the service runs without it; these tests do not
    x509 = None


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
