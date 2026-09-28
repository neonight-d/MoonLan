"""Signing in with a key (v0.7.6): the package, the storage, adding a
key, signing in with it, and an administrator's second factor."""

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
