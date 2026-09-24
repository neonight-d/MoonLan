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
import json
import logging
import time
from contextvars import ContextVar
from dataclasses import dataclass

from fastapi import FastAPI
from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Match, Mount

from . import access, auth
from .access import Principal
from .config import AuthConfig
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

# Between the password and the code: a ticket, not half a session. It
# opens nothing but the second step, lives five minutes and survives
# five wrong codes.
TICKET_SECONDS = 300
TICKET_TRIES = 5

# Expired sessions are dropped when they come back; this sweeps up the
# ones that never do
PURGE_SECONDS = 3600


@dataclass
class Ticket:
    user_id: int
    expires: float
    address: str
    tries: int = 0
    # the password hashed with today's parameters, stored once the
    # sign-in is complete
    rehashed: str | None = None


class LoginBody(BaseModel):
    # JSON, not a form: FastAPI reads forms only with python-multipart,
    # and MoonLan adds no dependency for this
    name: str = Field(max_length=64)
    password: str = Field(max_length=auth.PASSWORD_MAX)


class CodeBody(BaseModel):
    ticket: str = Field(max_length=128)
    code: str = Field(max_length=64)


class PasswordBody(BaseModel):
    current: str = Field(max_length=auth.PASSWORD_MAX)
    new: str = Field(max_length=auth.PASSWORD_MAX * 2)


def refuse(error: str, status: int, **extra) -> JSONResponse:
    return JSONResponse({"error": error, **extra}, status_code=status)


def principal() -> Principal | None:
    return _current.get()


def actor() -> str:
    """The name to journal an action under; "" while sign-in is off."""
    who = _current.get()
    return who.name if who else ""


class SignIn:
    """Sessions, looked up in `accounts` — the service's own database,
    or the demo's accounts file in demo mode. Signing in and out is
    written to `journal`, the service's own database either way: the
    journal is what the map shows."""

    def __init__(self, accounts: Database, journal: Database,
                 settings: AuthConfig):
        self.accounts = accounts
        self.journal = journal
        self.settings = settings
        self._tickets: dict[str, Ticket] = {}
        self._purged = 0.0

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
        if on and now - self._purged > PURGE_SECONDS:
            self._purged = now
            self.accounts.purge_sessions(
                now - self.settings.session_idle_hours * 3600,
                now - self.settings.session_max_days * 86400,
            )
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

    def _expired(self, row: dict, now: float) -> bool:
        """Idle too long, or open too long whatever it did. Every request
        of an open page counts as activity: the map refreshing itself
        every thirty seconds keeps a wall monitor signed in until
        session_max_days."""
        return (
            now - row["last_seen"] > self.settings.session_idle_hours * 3600
            or now - row["created_at"] > self.settings.session_max_days * 86400
        )

    def _principal(self, row: dict, now: float) -> Principal | None:
        if row["user_id"] is None or self._expired(row, now):
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

    # ---------- signing in ----------

    def _open_session(self, row: dict, second_factor: bool, address: str,
                      now: float, rehashed: str | None = None) -> str:
        token = auth.new_token()
        self.accounts.add_session(
            auth.token_hash(token), row["id"], now, second_factor, address
        )
        self.accounts.record_login(row["id"], now, rehashed)
        self.journal.add_event(
            now, "login", row["name"], json.dumps({"address": address}),
            row["name"],
        )
        log.info("Signed in: %s from %s", row["name"], address)
        return token

    def _signed_in(self, token: str) -> JSONResponse:
        """The cookie: HttpOnly — no script on the page reads it;
        SameSite=Strict — no other site's page sends it; Path=/. Not
        yet Secure: that needs HTTPS (v0.7.5). It lasts as long as a
        session may, so a browser restart does not sign a wall monitor
        out."""
        response = JSONResponse({"status": "signed_in"})
        response.set_cookie(
            SESSION_COOKIE, token,
            max_age=int(self.settings.session_max_days * 86400),
            httponly=True, samesite="strict", path="/",
        )
        return response

    def _check_password(self, name: str, password: str):
        """(the account, whether the password is right) — the account
        None when there is no such name."""
        row = None if auth.name_problem(name) else self.accounts.user(name)
        if row is None:
            return None, False
        return row, auth.verify_password(password, row["password"])

    async def login(self, name: str, password: str,
                    address: str) -> JSONResponse:
        """The first step: name and password."""
        now = time.time()
        row, right = await asyncio.to_thread(
            self._check_password, name, password
        )
        if not right:
            log.warning("Sign-in failed for %r from %s", name, address)
            # the same answer for a wrong name and a wrong password: which
            # names exist is nobody's business
            return refuse("invalid_credentials", 401)
        if row["disabled"]:
            # said only to somebody who knows the password
            log.warning("Sign-in refused for %s from %s: account disabled",
                        row["name"], address)
            return refuse("disabled", 403)
        rehashed = (
            await asyncio.to_thread(auth.hash_password, password)
            if auth.needs_rehash(row["password"]) else None
        )
        if row["totp_enabled"]:
            ticket = auth.new_token()
            self._drop_old_tickets(now)
            self._tickets[auth.token_hash(ticket)] = Ticket(
                row["id"], now + TICKET_SECONDS, address, rehashed=rehashed
            )
            return JSONResponse({"status": "code_required", "ticket": ticket})
        token = await asyncio.to_thread(
            self._open_session, row, False, address, now, rehashed
        )
        return self._signed_in(token)

    def _drop_old_tickets(self, now: float) -> None:
        for key in [k for k, t in self._tickets.items() if t.expires < now]:
            del self._tickets[key]

    async def second_step(self, ticket: str, code: str,
                          address: str) -> JSONResponse:
        """The second step: a code from the app or the key."""
        now = time.time()
        self._drop_old_tickets(now)
        key = auth.token_hash(ticket)
        entry = self._tickets.get(key)
        if entry is None:
            return refuse("ticket_expired", 401)
        row = await asyncio.to_thread(self.accounts.user_by_id, entry.user_id)
        if row is None or row["disabled"] or not row["totp_enabled"]:
            del self._tickets[key]
            return refuse("ticket_expired", 401)
        step = auth.match_totp(row["totp_secret"], code, now)
        if step is None:
            entry.tries += 1
            if entry.tries >= TICKET_TRIES:
                del self._tickets[key]
            log.warning("Sign-in: wrong code for %s from %s",
                        row["name"], address)
            return refuse("invalid_code", 401)
        if not await asyncio.to_thread(
            self.accounts.spend_totp_step, row["id"], step
        ):
            log.warning(
                "Sign-in: a code of %s used a second time, from %s",
                row["name"], address,
            )
            return refuse("code_used", 401)
        del self._tickets[key]
        token = await asyncio.to_thread(
            self._open_session, row, True, address, now, entry.rehashed
        )
        return self._signed_in(token)

    async def logout(self, who: Principal | None) -> JSONResponse:
        if who is not None and who.session:
            await asyncio.to_thread(self.accounts.drop_session, who.session)
            await asyncio.to_thread(
                self.journal.add_event, time.time(), "logout", who.name, "",
                who.name,
            )
            log.info("Signed out: %s", who.name)
        response = JSONResponse({"status": "signed_out"})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    async def change_password(self, who: Principal | None, current: str,
                              new: str) -> JSONResponse:
        """One's own password. The current one is asked for: a session
        taken over on the way (plain HTTP) must not be enough to lock
        its owner out. The other sessions of the account are closed,
        this one stays."""
        if who is None or who.user_id is None:
            return refuse("sign_in_required", 401)
        row, right = await asyncio.to_thread(
            self._check_password, who.name, current
        )
        if row is None or not right:
            log.warning("Password change for %s: the current password was "
                        "wrong", who.name)
            return refuse("wrong_password", 403)
        problem = auth.password_problem(row["name"], new)
        if problem is None and new == current:
            problem = "same_as_current"
        if problem:
            return refuse(problem, 400)
        closed = await asyncio.to_thread(
            self.accounts.set_password, row["name"],
            await asyncio.to_thread(auth.hash_password, new), False,
            who.session,
        )
        await asyncio.to_thread(
            self.journal.add_event, time.time(), "user_password", row["name"],
            json.dumps({"sessions": closed}), row["name"],
        )
        log.info("Password changed by %s themselves; %d other session(s) "
                 "closed", row["name"], closed)
        return JSONResponse({"status": "changed", "sessions_closed": closed})

    def me(self, who: Principal) -> dict:
        row = self.accounts.user(who.name) or {}
        return {
            "sign_in": True, "name": who.name, "role": who.role,
            "step": who.step,
            "totp": bool(row.get("totp_enabled")),
            "recovery_left": auth.recovery_left(row.get("recovery", "")),
        }


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


def _address(request: Request) -> str:
    return request.client.host if request.client else "?"


def add_routes(app: FastAPI, sign_in: SignIn) -> None:
    """/api/auth/*: who am I, signing in and out, one's own password.

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
        return await asyncio.to_thread(sign_in.me, who)

    @app.post("/api/auth/login")
    async def auth_login(body: LoginBody, request: Request):
        """Name and password. With TOTP on, the answer is "a code is
        needed" and a ticket for /api/auth/totp — not a session."""
        if not await asyncio.to_thread(sign_in.sign_in_on):
            return refuse("sign_in_off", 409)
        return await sign_in.login(body.name, body.password, _address(request))

    @app.post("/api/auth/totp")
    async def auth_totp(body: CodeBody, request: Request):
        return await sign_in.second_step(
            body.ticket, body.code, _address(request)
        )

    @app.post("/api/auth/logout")
    async def auth_logout():
        return await sign_in.logout(principal())

    @app.post("/api/auth/password")
    async def auth_password(body: PasswordBody):
        return await sign_in.change_password(
            principal(), body.current, body.new
        )
