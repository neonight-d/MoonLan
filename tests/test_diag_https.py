"""diag asks the service over https when MoonLan serves TLS (v0.8.8).

`_ask_service` always built http://host:port, and with listen.tls_cert
set that port speaks only TLS: --config lost its table of poll times,
--layout and --skipped failed outright. It now goes over https — and
trusts exactly the configured certificate: no CA, no "do not verify";
the server's certificate is compared with the first one in
listen.tls_cert before anything is sent, so a stranger on the port
gets neither the request nor the console token.

Run with:  python -m unittest discover -s tests
"""

import contextlib
import io
import json
import os
import ssl
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_https import make_certificate, x509  # noqa: E402

from moonlan import diag  # noqa: E402

TOKEN = "console-token-for-tests"


class Service:
    """A local server that answers JSON and remembers what reached it."""

    def __init__(self, cert=None, key=None):
        self.seen: list[dict] = []
        service = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                service.seen.append(dict(self.headers))
                body = json.dumps({"path": self.path}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        # a client that hangs up after the handshake is the point of one
        # test, not an error to print
        self.httpd.handle_error = lambda *_args: None
        if cert:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert, key)
            self.httpd.socket = context.wrap_socket(
                self.httpd.socket, server_side=True
            )
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_exc):
        self.httpd.shutdown()
        self.httpd.server_close()


def config(port, cert=""):
    return SimpleNamespace(
        listen_host="0.0.0.0", listen_port=port, listen_tls_cert=cert,
        users_db_path=lambda: "/nonexistent/users.db",
    )


def ask(cfg, path="/api/polling"):
    out = io.StringIO()
    with mock.patch.object(diag, "_console_token", lambda _cfg: TOKEN), \
            contextlib.redirect_stdout(out):
        answer = diag._ask_service(cfg, path)
    return answer, out.getvalue()


@unittest.skipIf(x509 is None, "cryptography is not installed")
class HttpsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        (self.dir / "ours").mkdir()
        (self.dir / "theirs").mkdir()
        self.cert, self.key, _ = make_certificate(self.dir / "ours", ("moonlan.local",))
        self.other_cert, self.other_key, _ = make_certificate(
            self.dir / "theirs", ("moonlan.local",)
        )

    def test_the_configured_certificate_gets_the_request(self):
        with Service(self.cert, self.key) as service:
            answer, out = ask(config(service.port, self.cert), "/api/skipped-oids")
        self.assertEqual(answer, {"path": "/api/skipped-oids"})
        self.assertEqual(out, "")
        self.assertEqual(len(service.seen), 1)
        self.assertEqual(service.seen[0].get("X-MoonLan-Console"), TOKEN)

    def test_a_chain_is_matched_by_its_first_certificate(self):
        chain = self.dir / "chain.pem"
        chain.write_text(
            Path(self.cert).read_text() + Path(self.other_cert).read_text()
        )
        with Service(self.cert, self.key) as service:
            answer, _out = ask(config(service.port, str(chain)))
        self.assertEqual(answer, {"path": "/api/polling"})

    def test_another_certificate_gets_nothing(self):
        with Service(self.other_cert, self.other_key) as service:
            answer, out = ask(config(service.port, self.cert))
        self.assertIsNone(answer)
        # not one request, so no token either
        self.assertEqual(service.seen, [])
        self.assertIn(f"the certificate on 127.0.0.1:{service.port} is not "
                      "the one in listen.tls_cert", out)
        self.assertIn("has not been restarted since the certificate was renewed", out)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0,
                     "root reads any file")
    def test_an_unreadable_certificate_is_said_so(self):
        locked = self.dir / "locked.pem"
        locked.write_text(Path(self.cert).read_text())
        os.chmod(locked, 0)
        self.addCleanup(os.chmod, locked, 0o600)
        with Service(self.cert, self.key) as service:
            answer, out = ask(config(service.port, str(locked)))
        self.assertIsNone(answer)
        self.assertEqual(service.seen, [])
        self.assertIn("listen.tls_cert", out)
        self.assertIn("cannot be read here", out)
        self.assertIn("run it as the user MoonLan runs as", out)


class HttpTest(unittest.TestCase):
    def test_without_tls_cert_it_is_http_as_before(self):
        with Service() as service:
            answer, out = ask(config(service.port))
        self.assertEqual(answer, {"path": "/api/polling"})
        self.assertEqual(out, "")
        self.assertEqual(service.seen[0].get("X-MoonLan-Console"), TOKEN)


if __name__ == "__main__":
    unittest.main()
