"""Requests through the whole ASGI app, middleware included, without
httpx: Starlette's TestClient needs it, and MoonLan adds no
dependency for its tests. The app is called the way uvicorn calls it.
"""

import asyncio
import json

HOST = "moonlan.test:8080"
ORIGIN = "http://" + HOST


class Answer:
    def __init__(self, status: int, headers: list, body: bytes):
        self.status = status
        self.headers = headers
        self.body = body

    def json(self):
        return json.loads(self.body or b"null")

    def header(self, name: str) -> list[str]:
        return [v for k, v in self.headers if k == name.lower()]


async def _call(app, method, path, headers, body, client, host, scheme):
    target, _, query = path.partition("?")
    raw = [(b"host", host.encode())]
    for name, value in headers.items():
        raw.append((name.lower().encode(), value.encode()))
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method, "scheme": scheme, "path": target,
        "raw_path": target.encode(), "query_string": query.encode(),
        "root_path": "", "headers": raw, "client": (client, 50000),
        "server": ("moonlan.test", 8080),
    }
    pending = [{"type": "http.request", "body": body, "more_body": False}]
    done = asyncio.Event()

    # the client goes away once the response is out, not before: a
    # disconnect at once cuts a streamed or file response short
    async def receive():
        if pending:
            return pending.pop(0)
        await done.wait()
        return {"type": "http.disconnect"}

    sent = []

    async def send(message):
        sent.append(message)
        if message["type"] == "http.response.body" and not message.get("more_body"):
            done.set()

    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    return Answer(
        start["status"],
        [(k.decode().lower(), v.decode()) for k, v in start["headers"]],
        b"".join(
            m.get("body", b"") for m in sent
            if m["type"] == "http.response.body"
        ),
    )


def call(app, method, path, *, json_body=None, cookie=None, origin=None,
         headers=None, client="10.0.0.99", host=HOST, scheme="http",
         data=None):
    """One request; `origin` defaults to the map's own for anything
    but GET, as a browser on the map would send it — False sends none.
    `data` is a raw body (a file being uploaded)."""
    headers = dict(headers or {})
    body = data or b""
    if json_body is not None:
        body = json.dumps(json_body).encode()
        headers["content-type"] = "application/json"
    if cookie:
        headers["cookie"] = cookie
    if origin is None and method not in ("GET", "HEAD"):
        origin = f"{scheme}://{host}"
    if origin is not None and origin is not False:
        headers["origin"] = origin
    return asyncio.run(
        _call(app, method, path, headers, body, client, host, scheme)
    )
