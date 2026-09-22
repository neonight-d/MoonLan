"""The browser re-checks the web UI on every load.

Without a Cache-Control header a browser decides for itself how long
app.js stays fresh, and for a file last touched weeks ago that is days.
After an upgrade the page went on running the previous version: a fix
to the map was on the server and in nobody's browser.

Run with:  python -m unittest discover -s tests
"""

import asyncio
import sys
import unittest
from pathlib import Path

# One shared configuration for every test that imports the service —
# see tests/service_fixture.py for why it cannot be per-module.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from service_fixture import SWITCHES  # noqa: E402,F401

from moonlan import server  # noqa: E402


def _get(path: str, headers: list[tuple[bytes, bytes]] = ()) -> tuple[int, dict]:
    """One GET straight through the ASGI app: status and headers."""
    scope = {
        "type": "http", "method": "GET", "path": path, "raw_path": path.encode(),
        "query_string": b"", "headers": list(headers), "root_path": "",
        "scheme": "http", "server": ("test", 80), "client": ("127.0.0.1", 1),
        "http_version": "1.1",
    }
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(server.app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    return start["status"], {
        k.decode().lower(): v.decode() for k, v in start["headers"]
    }


class StaticCacheTest(unittest.TestCase):
    def test_the_script_is_revalidated(self):
        status, headers = _get("/app.js")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("cache-control"), "no-cache")
        self.assertIn("etag", headers)

    def test_the_page_itself_is_revalidated(self):
        status, headers = _get("/")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("cache-control"), "no-cache")

    def test_an_unchanged_file_costs_a_304(self):
        """no-cache is "ask first", not "download again"."""
        _, headers = _get("/app.js")
        status, again = _get(
            "/app.js", [(b"if-none-match", headers["etag"].encode())]
        )
        self.assertEqual(status, 304)
        self.assertEqual(again.get("cache-control"), "no-cache")


if __name__ == "__main__":
    unittest.main()
