"""Health, About and the page itself (v0.8.13).

/api/health for monitoring from outside, /api/about and the license,
and index.html with the addresses of the page's own files versioned by
what is in them — beside the folder server.py mounts last, which the
cache revalidates. Moved out of server.py word for word
(docs/ARCHITECTURE.md); the state is read as rt.X when a route runs.
Nothing here imports moonlan.server.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from .. import __version__, about, oui, signin
from ..runtime import rt
from .common import _refuse

router = APIRouter()


WEB_DIR = Path(__file__).resolve().parent.parent.parent / "web"


# For About (v0.8.1): when this process started, and the commit it was
# started from — read once, as a pull after the start changes .git and
# not the code that is running
STARTED_AT = time.time()
BUILD = about.build_hash()
COPYRIGHT = about.copyright_line(about.license_text())


@router.get("/api/health")
async def api_health() -> dict:
    """For monitoring from outside, without signing in: is it up, and
    which version. Nothing about the network."""
    return {"status": "ok", "version": __version__}


@router.get("/api/about")
async def api_about() -> dict:
    """What MoonLan is, which build, what it runs on and with which
    versions, and the IEEE register. The server itself — Python, the
    system, uptime, sign-in, HTTPS — only to an administrator, or to
    everybody while sign-in is off, when everybody is one."""
    on = await asyncio.to_thread(rt.sign_in.sign_in_on)
    who = signin.principal()
    body = {
        "version": __version__,
        "build": BUILD,
        "copyright": COPYRIGHT,
        "components": about.components(),
        "ieee": {
            "loaded": rt.vendors.loaded,
            "date": rt.vendors.oldest(),
            "command": oui.UPDATE_COMMAND,
        },
    }
    if not on or (who is not None and who.role == "admin"):
        body["server"] = about.server(STARTED_AT, on, rt.transport)
    return body


@router.get("/api/about/license")
async def api_about_license() -> Response:
    """The LICENSE file, as text: MIT asks for it to go with every copy"""
    text = await asyncio.to_thread(about.license_text)
    if text is None:
        return _refuse("no_license", 404)
    return Response(text, media_type="text/plain; charset=utf-8")


# The page's own files, addressed in index.html by what is in them
VERSIONED_ASSETS = ("app.js", "i18n.js", "style.css")


def _index_html() -> str:
    """index.html with each of the page's files named by its content.

    `no-cache` below only helps a browser that has been told it: a
    copy cached before this header existed carries no such instruction
    and is still "fresh" by the browser's own reckoning. After the
    upgrade that introduced the header, a page went on running with a
    new app.js and an old i18n.js — and showed translation keys where
    the journal should have said "Placed by hand". A file whose content
    changes gets a new address, and no cache has a copy of that.
    """
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    for name in VERSIONED_ASSETS:
        digest = hashlib.sha1((WEB_DIR / name).read_bytes()).hexdigest()[:12]
        html = html.replace(f'"{name}"', f'"{name}?v={digest}"')
    return html


@router.get("/", include_in_schema=False)
@router.get("/index.html", include_in_schema=False)
async def index_page() -> HTMLResponse:
    return HTMLResponse(
        await asyncio.to_thread(_index_html),
        headers={"Cache-Control": "no-cache"},
    )


class RevalidatedStaticFiles(StaticFiles):
    """The web UI, which the browser must re-check on every load.

    Served without a Cache-Control header, a file last modified weeks
    ago is "fresh" by the browser's own reckoning for days: after an
    upgrade the page went on running the previous app.js, and a fix to
    the map reached nobody until their cache happened to expire.
    `no-cache` is not "do not cache" — the browser keeps its copy and
    asks each time, and an unchanged file costs a 304.
    """

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response
