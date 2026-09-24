"""Signing in, on the service's side: who is asking, and the guard in
front of every request.

The rights themselves are a table in access.py. This module finds out
who a request comes from — the session cookie, looked up in the
database on every request, so an account changed with
`python -m moonlan.users` while the service runs takes effect at once
— and refuses what the table does not allow.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextvars import ContextVar

from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Match, Mount

from . import access, auth
from .access import Principal
from .db import Database

log = logging.getLogger("moonlan")

SESSION_COOKIE = "moonlan_session"

# How to switch sign-in on, for the log and for the page
CREATE_ADMIN = "python -m moonlan.users add <name> --role admin"

# Who the request being handled comes from. Set by the guard for the
# length of the request, so a handler that journals an action can name
# the person without every signature in server.py growing a parameter;
# None while sign-in is off, and for a handler called directly (tests).
_current: ContextVar[Principal | None] = ContextVar(
    "moonlan_principal", default=None
)

# Writing last_seen on every request of every open map would be a
# write per request for nothing: a minute is precise enough for a
# twelve-hour idle limit
TOUCH_SECONDS = 60

# Methods that change nothing and need no Origin check
SAFE_METHODS = ("GET", "HEAD")


def principal() -> Principal | None:
    return _current.get()


def actor() -> str:
    """The name to journal an action under; "" while sign-in is off."""
    who = _current.get()
    return who.name if who else ""


class SignIn:
    """Sessions, looked up in `accounts` — the service's own database,
    or the demo's accounts file in demo mode."""

    def __init__(self, accounts: Database):
        self.accounts = accounts

    def sign_in_on(self) -> bool:
        return self.accounts.active_admins() > 0

    def log_state(self) -> None:
        """One line at startup: is the map open, or behind sign-in."""
        users = self.accounts.users()
        if not self.sign_in_on():
            log.warning(
                "Sign-in is not set up: the map is open to everyone who can "
                "reach it, and anyone can pin, reset the layout and start a "
                "scan. Create the first administrator on this machine with: "
                "%s — sign-in switches on at once, no restart needed.",
                CREATE_ADMIN,
            )
            return
        roles = {role: 0 for role in auth.ROLES}
        for row in users:
            if not row["disabled"]:
                roles[row["role"]] += 1
        log.info(
            "Sign-in: on — %d administrator(s), %d user(s), %d viewer(s)",
            roles["admin"], roles["user"], roles["viewer"],
        )

    def _lookup(self, token: str, now: float) -> tuple[bool, Principal | None]:
        on = self.sign_in_on()
        if not on or not token:
            return on, None
        digest = auth.token_hash(token)
        row = self.accounts.session_row(digest)
        if row is None:
            return on, None
        who = self._principal(row, now)
        if who is None:
            self.accounts.drop_session(digest)
            return on, None
        if now - row["last_seen"] > TOUCH_SECONDS:
            self.accounts.touch_session(digest, now)
        return on, who

    def _principal(self, row: dict, now: float) -> Principal | None:
        if row["user_id"] is None:
            return None
        if row["name"] is None or row["disabled"]:
            return None
        step = None
        if row["must_change"]:
            step = "password"
        elif row["role"] == "admin" and not row["second_factor"]:
            if row["totp_enabled"]:
                # a second factor exists and this session never gave it
                return None
            step = "totp"
        return Principal(
            name=row["name"], role=row["role"], user_id=row["user_id"],
            session=row["token_hash"], step=step,
        )

    async def who(self, request: Request) -> tuple[bool, Principal | None]:
        """(is sign-in on, who is asking — None for nobody)."""
        return await asyncio.to_thread(
            self._lookup, request.cookies.get(SESSION_COOKIE, ""),
            time.time(),
        )


def route_key(routes, scope) -> str | None:
    """"METHOD /path/template" of the route that will serve this
    request — found the way the router finds it, first full match."""
    for route in routes:
        match, _ = route.matches(scope)
        if match == Match.FULL:
            if isinstance(route, Mount):
                return access.STATIC
            # HEAD is GET without the body, and has GET's rights
            method = "GET" if scope["method"] == "HEAD" else scope["method"]
            # a kind of route with no path of its own is named by
            # nothing in the table — administrators only, not open
            path = getattr(route, "path", None) or f"<{type(route).__name__}>"
            return f"{method} {path}"
    return None


def same_origin(request: Request) -> bool:
    """The page that sent this is the map itself.

    With SameSite=Strict on the cookie this closes cross-site request
    forgery: a page elsewhere can make the browser send a request, but
    not with our cookie and not with our Origin. A request without an
    Origin is refused — every browser sends one with a POST, PUT, PATCH
    or DELETE, and a script can send one too.
    """
    origin = request.headers.get("origin", "")
    host = request.headers.get("host", "")
    return bool(origin) and origin.lower() == (
        f"{request.url.scheme}://{host}".lower()
    )


class Guard:
    """ASGI middleware: every request is checked against access.RULES
    before it reaches a handler — or its body is even read."""

    def __init__(self, app, signin: SignIn, router):
        self.app = app
        self.signin = signin
        self.router = router

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        key = route_key(self.router.routes, scope)
        method = scope["method"]
        rule = access.rule_for(key) if key else access.PUBLIC
        if rule == access.PUBLIC and method in SAFE_METHODS:
            # the page and its files: no database lookup per stylesheet
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        on, who = await self.signin.who(request)
        refusal = access.decide(key or access.STATIC, who, on)
        if refusal is None and on and method not in SAFE_METHODS \
                and not same_origin(request):
            refusal = access.Refusal(403, {"error": "bad_origin"})
        if refusal is not None:
            await JSONResponse(refusal.body, status_code=refusal.status)(
                scope, receive, send
            )
            return
        token = _current.set(who)
        try:
            await self.app(scope, receive, send)
        finally:
            _current.reset(token)


def add_routes(app: FastAPI, sign_in: SignIn) -> None:
    """/api/auth/*: who am I, and (later) signing in and out.

    Added to the app itself, not through an APIRouter: an included
    router is one opaque entry in app.routes, and the guard — like the
    test that every route is in the rights table — has to see each
    route by its own path.
    """

    @app.get("/api/auth/me")
    async def auth_me(request: Request):
        """Who this browser is signed in as — or that sign-in is off,
        which the page shows in its header for as long as it lasts."""
        on, who = await sign_in.who(request)
        if not on:
            return {"sign_in": False, "create_admin": CREATE_ADMIN}
        if who is None:
            return JSONResponse(
                {"error": "sign_in_required", "sign_in": True},
                status_code=401,
            )
        return {
            "sign_in": True, "name": who.name, "role": who.role,
            "step": who.step,
        }
