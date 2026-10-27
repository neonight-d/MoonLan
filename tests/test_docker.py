"""The image (v0.8.3), as far as a unit test can see it: the build About
shows without .git, the health check against a server raised here over
http and https, what .dockerignore keeps out and what it must not. The
image itself is built and run by .github/workflows/docker.yml."""

import datetime
import fnmatch
import http.server
import json
import os
import ssl
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from moonlan import about, healthcheck

ROOT = Path(__file__).resolve().parent.parent


class BuildTest(unittest.TestCase):
    def test_the_build_of_an_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"MOONLAN_BUILD": "fd33823"}):
                self.assertEqual(about.build_hash(Path(tmp)), "fd33823")
            with mock.patch.dict(os.environ, {"MOONLAN_BUILD": "not a commit"}):
                self.assertIsNone(about.build_hash(Path(tmp)))
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertIsNone(about.build_hash(Path(tmp)))

    def test_a_clone_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            git = Path(tmp) / ".git"
            git.mkdir()
            (git / "HEAD").write_text("2529e8c2f5a1e9d0c4b7a8e6f3d2c1b0a9e8d7c6\n")
            with mock.patch.dict(os.environ, {"MOONLAN_BUILD": "fd33823"}):
                self.assertEqual(about.build_hash(Path(tmp)), "2529e8c")


class Health(http.server.BaseHTTPRequestHandler):
    status = {"status": "ok"}

    def do_GET(self):
        body = json.dumps(self.status if self.path == "/api/health" else {}).encode()
        self.send_response(200 if self.path == "/api/health" else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def serve(tls=None):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Health)
    if tls:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(*tls)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def self_signed(folder):
    """A certificate for another name than localhost, as a real one is"""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "moonlan.example.local")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1))
            .sign(key, hashes.SHA256()))
    cert_file, key_file = Path(folder) / "cert.pem", Path(folder) / "key.pem"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return str(cert_file), str(key_file)


class Listen:
    def __init__(self, host="0.0.0.0", port=8080, tls=""):
        self.listen_host, self.listen_port, self.listen_tls_cert = host, port, tls


class HealthcheckTest(unittest.TestCase):
    def test_where_it_asks(self):
        self.assertEqual(healthcheck.health_url(Listen()), "http://127.0.0.1:8080/api/health")
        self.assertEqual(healthcheck.health_url(Listen("::", 8443, "/config/tls/c.pem")),
                         "https://[::1]:8443/api/health")
        self.assertEqual(healthcheck.health_url(Listen("10.0.0.5", 9000)),
                         "http://10.0.0.5:9000/api/health")

    def test_over_http(self):
        server = serve()
        self.addCleanup(server.shutdown)
        port = server.server_address[1]
        self.assertTrue(healthcheck.healthy(f"http://127.0.0.1:{port}/api/health"))
        self.assertFalse(healthcheck.healthy(f"http://127.0.0.1:{port}/elsewhere"))

    def test_over_https_with_its_own_certificate(self):
        try:
            import cryptography  # noqa: F401
        except ImportError:
            self.skipTest("cryptography is not installed")
        with tempfile.TemporaryDirectory() as tmp:
            server = serve(self_signed(tmp))
            self.addCleanup(server.shutdown)
            port = server.server_address[1]
            self.assertTrue(healthcheck.healthy(f"https://127.0.0.1:{port}/api/health"))

    def test_nobody_there(self):
        server = serve()
        port = server.server_address[1]
        server.shutdown()
        server.server_close()
        self.assertFalse(healthcheck.healthy(f"http://127.0.0.1:{port}/api/health", timeout=2))

    def test_not_ok(self):
        server = serve()
        self.addCleanup(server.shutdown)
        with mock.patch.object(Health, "status", {"status": "starting"}):
            port = server.server_address[1]
            self.assertFalse(healthcheck.healthy(f"http://127.0.0.1:{port}/api/health"))


def ignored(path, patterns):
    """Whether a path would be left out of the build context — the
    subset of .dockerignore's rules the file uses: globs, ** and dirs"""
    parts = Path(path).parts
    for pattern in patterns:
        bare = pattern.rstrip("/")
        for i in range(1, len(parts) + 1):
            prefix = "/".join(parts[:i])
            if fnmatch.fnmatch(prefix, bare) or (bare.startswith("**/") and
                                                 fnmatch.fnmatch(parts[i - 1], bare[3:])):
                return True
    return False


class ImageContentsTest(unittest.TestCase):
    def setUp(self):
        self.patterns = [line.strip() for line in (ROOT / ".dockerignore").read_text().splitlines()
                         if line.strip() and not line.startswith("#")]

    def test_what_the_image_needs_is_in(self):
        for path in ("LICENSE", "requirements.txt", "web/vendor/vis-network-9.1.9/vis-network.min.js",
                     "web/app.js", "moonlan/server.py", "config.example.yaml", "run.py"):
            with self.subTest(path):
                self.assertFalse(ignored(path, self.patterns))

    def test_what_it_must_not_have_is_out(self):
        for path in (".git/HEAD", "config.yaml", "moonlan.db", "moonlan.db-wal", "moonlan.db.console",
                     "oui/oui.csv", "tests/test_docker.py", "docs/results/x.md", "docs/tz/x.md",
                     "moonlan/__pycache__/server.cpython-312.pyc", ".venv/bin/python"):
            with self.subTest(path):
                self.assertTrue(ignored(path, self.patterns))

    def test_the_dockerfile(self):
        docker = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertRegex(docker, r"FROM python:3\.12\.\d+-slim-bookworm\n")
        for line in ("USER moonlan", "WORKDIR /data", "EXPOSE 8080",
                     "MOONLAN_CONFIG=/config/config.yaml", "PYTHONPATH=/app",
                     'CMD ["python", "-m", "moonlan.healthcheck"]', "ARG MOONLAN_BUILD",
                     "iputils-ping traceroute tzdata ca-certificates"):
            self.assertIn(line, docker)
        # no curl installed: the health check is Python
        commands = "\n".join(line for line in docker.splitlines() if not line.lstrip().startswith("#"))
        self.assertNotIn("curl", commands)
        # the dependencies before the code, for the cache
        self.assertLess(docker.index("COPY requirements.txt ."), docker.index("COPY . ."))

    def test_the_compose_example(self):
        compose = (ROOT / "docs" / "deploy" / "docker-compose.yml").read_text(encoding="utf-8")
        for line in ("init: true", "./config:/config", "./data:/data", '"8080:8080"'):
            self.assertIn(line, compose)
        self.assertNotIn("network_mode", compose)


if __name__ == "__main__":
    unittest.main()
