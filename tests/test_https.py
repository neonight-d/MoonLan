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
