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
import hmac
import json
import logging
import math
import os
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
from .db import AccountError, Database

log = logging.getLogger("moonlan")

SESSION_COOKIE = "moonlan_session"

# diag, on the server: python -m moonlan.diag asks the running service
# for what only the service knows (poll times, paused OIDs, which nodes
# are on the map). It sends this header with the token the service
# wrote next to its database at startup — readable by whoever can read
# the database itself, and good for looking only.
CONSOLE_HEADER = "x-moonlan-console"

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


# Wrong passwords and codes. Counted in this process's memory: MoonLan
# is one process, and starting the count over after a restart is fine.
#
# By address: the first five failures in a row cost nothing — people
# mistype — then every next attempt waits twice as long, up to a
# minute. By account: ten in a row lock it for fifteen minutes — not
# for good, or anybody could lock the administrator out by typing the
# name. `python -m moonlan.users unlock` lifts it early.
ADDRESS_FREE_FAILURES = 5
ADDRESS_MAX_DELAY = 60.0
ACCOUNT_LOCK_FAILURES = 10
ACCOUNT_LOCK_SECONDS = 15 * 60
# An address that has been quiet this long starts from zero
FORGET_SECONDS = 3600


class Throttle:
    def __init__(self):
        # address -> (failures in a row, no attempt before, last failure)
        self._addresses: dict[str, tuple[int, float, float]] = {}
        # name key -> failures in a row
        self._accounts: dict[str, int] = {}
        # name key -> locked until: names nobody has. They lock exactly
        # like real ones, or "locked" would tell which names are real.
        self._phantoms: dict[str, float] = {}

    def wait(self, address: str, now: float) -> float:
        """Seconds this address has to wait before its next attempt."""
        entry = self._addresses.get(address)
        return max(0.0, entry[1] - now) if entry else 0.0

    def failures(self, address: str) -> int:
        entry = self._addresses.get(address)
        return entry[0] if entry else 0

    def address_failed(self, address: str, now: float) -> None:
        for other, (_, _, last) in list(self._addresses.items()):
            if now - last > FORGET_SECONDS:
                del self._addresses[other]
        count = self.failures(address) + 1
        delay = 0.0
        if count >= ADDRESS_FREE_FAILURES:
            delay = min(ADDRESS_MAX_DELAY,
                        2.0 ** (count - ADDRESS_FREE_FAILURES))
        self._addresses[address] = (count, now + delay, now)

    def account_failed(self, key: str) -> bool:
        """Counts one; True when this one locks the account."""
        count = self._accounts.get(key, 0) + 1
        if count >= ACCOUNT_LOCK_FAILURES:
            self._accounts.pop(key, None)
            return True
        self._accounts[key] = count
        return False

    def phantom_locked(self, key: str, now: float) -> float:
        until = self._phantoms.get(key, 0.0)
        if until and until <= now:
            del self._phantoms[key]
            return 0.0
        return until

    def lock_phantom(self, key: str, until: float) -> None:
        self._phantoms[key] = until

    def succeeded(self, address: str, key: str) -> None:
        self._addresses.pop(address, None)
        self._accounts.pop(key, None)


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


class BindBody(BaseModel):
    code: str = Field(max_length=64)
    # binding a second factor asks for the password: over plain HTTP a
    # session can be taken on the way, and a second factor bound by
    # whoever took it would lock the owner out of every next sign-in
    password: str = Field(max_length=auth.PASSWORD_MAX)


class PasswordBody(BaseModel):
    current: str = Field(max_length=auth.PASSWORD_MAX)
    new: str = Field(max_length=auth.PASSWORD_MAX * 2)


class NewUserBody(BaseModel):
    name: str = Field(max_length=64)
    role: str


class UserPatch(BaseModel):
    role: str | None = None
    disabled: bool | None = None


def refuse(error: str, status: int, **extra) -> JSONResponse:
    return JSONResponse({"error": error, **extra}, status_code=status)


def log_plain_http() -> None:
    """What signing in over plain HTTP is worth, said where the operator
    looks: it keeps out a stranger at the keyboard and a guessed
    password, not somebody reading the traffic on the same segment."""
    log.warning(
        "Sign-in runs over plain HTTP: passwords and session cookies cross "
        "the network in clear text, and anyone who can read the traffic "
        "can take a session. HTTPS arrives in v0.7.5; until then bind "
        "TOTP with python -m moonlan.users totp <name> on this machine "
        "rather than from a browser."
    )


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
        self.throttle = Throttle()
        # sign-in as the last request found it: the first administrator
        # created from the console switches it on between two requests,
        # and the log should say so then, not at the next restart
        self._was_on: bool | None = None
        self._console: str = ""    # hash of the token in the console file
        self.console_path: str = ""

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
        log_plain_http()

    def _lookup(self, token: str, now: float) -> tuple[bool, Principal | None]:
        on = self.sign_in_on()
        if on and self._was_on is False:
            log.info("Sign-in switched on: an enabled administrator exists "
                     "now. Every open map asks to sign in.")
            log_plain_http()
        self._was_on = on
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
        on, who = await asyncio.to_thread(
            self._lookup, request.cookies.get(SESSION_COOKIE, ""),
            time.time(),
        )
        console = request.headers.get(CONSOLE_HEADER, "")
        if who is None and console and self._console and hmac.compare_digest(
            auth.token_hash(console), self._console
        ):
            who = Principal(name="@console", role="viewer", console=True)
        return on, who

    # ---------- diag, from the server's shell ----------

    def write_console_token(self, path: str) -> None:
        """A fresh token for diag at every start, in a file only the
        service's own user can read (0600) — the same people who can
        read the database. diag writes nothing to the database, so it
        cannot sign itself in; this is how it asks the service instead.
        """
        token = auth.new_token()
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="ascii") as handle:
                handle.write(token + "\n")
            os.chmod(path, 0o600)
        except OSError as exc:
            log.warning(
                "Could not write %s (%s): once sign-in is on, "
                "python -m moonlan.diag will not be able to ask this "
                "service for poll times, paused OIDs or the map's nodes",
                path, exc,
            )
            return
        self._console = auth.token_hash(token)
        self.console_path = path

    def remove_console_token(self) -> None:
        if self.console_path:
            try:
                os.unlink(self.console_path)
            except OSError:
                pass

    # ---------- signing in ----------

    def _open_session(self, row: dict, second_factor: bool, address: str,
                      now: float, rehashed: str | None = None,
                      recovery_left: int | None = None) -> str:
        self.throttle.succeeded(address, auth.name_key(row["name"]))
        token = auth.new_token()
        self.accounts.add_session(
            auth.token_hash(token), row["id"], now, second_factor, address
        )
        self.accounts.record_login(row["id"], now, rehashed)
        details = {"address": address}
        if recovery_left is not None:
            details["recovery_left"] = recovery_left
        self.journal.add_event(
            now, "login", row["name"], json.dumps(details), row["name"],
        )
        log.info(
            "Signed in: %s from %s%s", row["name"], address,
            "" if recovery_left is None else
            f" with a recovery code ({recovery_left} left)",
        )
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
        None when there is no such name.

        A name nobody has costs the same scrypt as a real one: answering
        at once for an unknown name and after 40 ms for a known one would
        tell anybody with a stopwatch which names exist.
        """
        row = None if auth.name_problem(name) else self.accounts.user(name)
        if row is None:
            auth.burn_a_check(password)
            return None, False
        return row, auth.verify_password(password, row["password"])

    def _too_soon(self, address: str, now: float) -> JSONResponse | None:
        wait = self.throttle.wait(address, now)
        if wait <= 0:
            return None
        seconds = math.ceil(wait)
        response = refuse("too_many_attempts", 429, retry_after=seconds)
        response.headers["Retry-After"] = str(seconds)
        return response

    def _locked(self, until: float, now: float) -> JSONResponse:
        seconds = math.ceil(until - now)
        response = refuse("locked", 423, retry_after=seconds)
        response.headers["Retry-After"] = str(seconds)
        return response

    async def _failed(self, name: str, row: dict | None, address: str,
                      now: float, what: str) -> JSONResponse | None:
        """Counts a wrong password or code, logs it, and locks the
        account on the tenth in a row — the answer to give then, or
        None for the usual one."""
        self.throttle.address_failed(address, now)
        key = auth.name_key(name)
        log.warning(
            "Sign-in failed for %r from %s: %s (%d in a row from this "
            "address)", name, address, what, self.throttle.failures(address),
        )
        if not self.throttle.account_failed(key):
            return None
        until = now + ACCOUNT_LOCK_SECONDS
        if row is None:
            self.throttle.lock_phantom(key, until)
        else:
            await asyncio.to_thread(self.accounts.lock, row["id"], until)
            await asyncio.to_thread(
                self.journal.add_event, now, "account_locked", row["name"],
                json.dumps({"address": address,
                            "minutes": ACCOUNT_LOCK_SECONDS // 60}),
            )
        log.warning(
            "Sign-in: %r locked for %d minutes after %d failures in a row "
            "(the last from %s); python -m moonlan.users unlock %s lifts it",
            name, ACCOUNT_LOCK_SECONDS // 60, ACCOUNT_LOCK_FAILURES, address,
            name,
        )
        return self._locked(until, now)

    async def login(self, name: str, password: str,
                    address: str) -> JSONResponse:
        """The first step: name and password."""
        now = time.time()
        too_soon = self._too_soon(address, now)
        if too_soon is not None:
            return too_soon
        row, right = await asyncio.to_thread(
            self._check_password, name, password
        )
        locked_until = (
            row["locked_until"] if row is not None
            else self.throttle.phantom_locked(auth.name_key(name), now)
        )
        if locked_until > now:
            # checked after the password, not before: a lock must not
            # answer any faster than a wrong password does
            self.throttle.address_failed(address, now)
            log.warning("Sign-in refused for %r from %s: locked", name, address)
            return self._locked(locked_until, now)
        if not right:
            # the same answer for a wrong name and a wrong password: which
            # names exist is nobody's business
            locked = await self._failed(name, row, address, now,
                                        "wrong name or password")
            return locked or refuse("invalid_credentials", 401)
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
        too_soon = self._too_soon(address, now)
        if too_soon is not None:
            return too_soon
        self._drop_old_tickets(now)
        key = auth.token_hash(ticket)
        entry = self._tickets.get(key)
        if entry is None:
            return refuse("ticket_expired", 401)
        row = await asyncio.to_thread(self.accounts.user_by_id, entry.user_id)
        if row is None or row["disabled"] or not row["totp_enabled"]:
            del self._tickets[key]
            return refuse("ticket_expired", 401)
        if row["locked_until"] > now:
            del self._tickets[key]
            return self._locked(row["locked_until"], now)
        step = auth.match_totp(row["totp_secret"], code, now)
        if step is None and len(auth.normal_recovery_code(code)) \
                == auth.RECOVERY_LENGTH:
            # a recovery code instead: a lost phone must not be a lost
            # account. It works once.
            left = await asyncio.to_thread(
                auth.spend_recovery_code, code, row["recovery"]
            )
            if left is not None and await asyncio.to_thread(
                self.accounts.use_recovery_code, row["id"], row["recovery"],
                left,
            ):
                del self._tickets[key]
                token = await asyncio.to_thread(
                    self._open_session, row, True, address, now,
                    entry.rehashed, auth.recovery_left(left),
                )
                return self._signed_in(token)
        if step is None:
            entry.tries += 1
            if entry.tries >= TICKET_TRIES:
                del self._tickets[key]
            locked = await self._failed(row["name"], row, address, now,
                                        "wrong code")
            if locked is not None:
                self._tickets.pop(key, None)
                return locked
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
                              new: str, address: str) -> JSONResponse:
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
            # counted like a wrong password at sign-in: a session taken
            # over on the way must not get to guess at leisure
            locked = await self._failed(who.name, row, address, time.time(),
                                        "wrong current password")
            if locked is not None:
                await asyncio.to_thread(self.accounts.close_sessions,
                                        who.name)
                return locked
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

    # ---------- one's own second factor ----------

    async def totp_setup(self, who: Principal | None) -> JSONResponse:
        """A new secret to scan. Nothing is bound yet: a mistake while
        scanning must not lock anybody out, so the secret waits until a
        code made from it comes back (totp_confirm)."""
        if who is None or who.user_id is None:
            return refuse("sign_in_required", 401)
        secret = auth.new_totp_secret()
        await asyncio.to_thread(self.accounts.set_totp_pending,
                                who.user_id, secret)
        return JSONResponse({
            "secret": secret, "uri": auth.otpauth_uri(who.name, secret),
        })

    async def totp_confirm(self, who: Principal | None, code: str,
                           password: str, address: str) -> JSONResponse:
        """Binds the secret shown by totp_setup, once a code from it and
        the password are right. Answers with the recovery codes — the
        only time anybody sees them."""
        if who is None or who.user_id is None:
            return refuse("sign_in_required", 401)
        now = time.time()
        row, right = await asyncio.to_thread(
            self._check_password, who.name, password
        )
        if row is None or not right:
            locked = await self._failed(who.name, row, address, now,
                                        "wrong password binding TOTP")
            if locked is not None:
                await asyncio.to_thread(self.accounts.close_sessions,
                                        who.name)
                return locked
            return refuse("wrong_password", 403)
        pending = row["totp_pending"]
        if not pending:
            return refuse("no_pending_secret", 409)
        step = auth.match_totp(pending, code, now)
        if step is None:
            return refuse("invalid_code", 400)
        codes = auth.new_recovery_codes()
        stored = await asyncio.to_thread(auth.hash_recovery_codes, codes)
        closed = await asyncio.to_thread(
            self.accounts.enable_totp, row["name"], pending, stored, step,
            who.session,
        )
        await asyncio.to_thread(
            self.journal.add_event, now, "user_totp_enabled", row["name"],
            json.dumps({"sessions": closed, "rebound": bool(
                row["totp_enabled"])}), row["name"],
        )
        log.info("TOTP bound by %s from %s%s; %d other session(s) closed",
                 row["name"], address,
                 " (a new secret in place of the old)"
                 if row["totp_enabled"] else "", closed)
        return JSONResponse({"status": "bound", "recovery": codes})

    # ---------- accounts, for an administrator ----------

    def users(self) -> list[dict]:
        sessions = self.accounts.session_counts()
        now = time.time()
        return [
            {
                "name": row["name"], "role": row["role"],
                "disabled": bool(row["disabled"]),
                "totp": bool(row["totp_enabled"]),
                "recovery_left": auth.recovery_left(row["recovery"]),
                "must_change": bool(row["must_change"]),
                "locked_until": (
                    row["locked_until"] if row["locked_until"] > now else 0
                ),
                "last_login": row["last_login"],
                "created_at": row["created_at"],
                "sessions": sessions.get(row["id"], 0),
            }
            for row in self.accounts.users()
        ]

    def account_event(self, event: str, name: str, **details) -> None:
        """An account change made in the browser, under the name of the
        administrator who made it."""
        self.journal.add_event(
            time.time(), event, name, json.dumps(details) if details else "",
            actor(),
        )
        log.info("Accounts: %s %s by %s%s", event, name, actor(),
                 f" {details}" if details else "")

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

    @app.post("/api/auth/totp/setup")
    async def auth_totp_setup():
        return await sign_in.totp_setup(principal())

    @app.post("/api/auth/totp/confirm")
    async def auth_totp_confirm(body: BindBody, request: Request):
        return await sign_in.totp_confirm(
            principal(), body.code, body.password, _address(request)
        )

    def account_refusal(error: AccountError) -> JSONResponse:
        status = {"unknown": 404, "exists": 409, "last_admin": 409}
        return refuse(error.code, status.get(error.code, 400),
                      name=error.name)

    async def run(call, *args):
        """An account change, with the database's refusals turned into
        answers: no such name, name taken, the last administrator."""
        try:
            return await asyncio.to_thread(call, *args), None
        except AccountError as error:
            return None, account_refusal(error)

    @app.get("/api/users")
    async def users_list():
        return {"users": await asyncio.to_thread(sign_in.users),
                "me": actor()}

    @app.post("/api/users")
    async def users_add(body: NewUserBody):
        """A new account with a temporary password, generated here and
        shown once: the person sets their own at the first sign-in."""
        if auth.name_problem(body.name):
            return refuse("bad_name", 400)
        if body.role not in auth.ROLES:
            return refuse("bad_role", 400)
        password = auth.temporary_password()
        row, refusal = await run(
            sign_in.accounts.add_user, body.name, body.role,
            await asyncio.to_thread(auth.hash_password, password), True,
        )
        if refusal:
            return refusal
        await asyncio.to_thread(sign_in.account_event, "user_added",
                                row["name"], role=body.role, temporary=True)
        return {"name": row["name"], "role": row["role"],
                "password": password}

    @app.patch("/api/users/{name}")
    async def users_change(name: str, body: UserPatch):
        """A role, or disabled / enabled. The last enabled
        administrator is refused, as on the console."""
        if body.role is not None:
            if body.role not in auth.ROLES:
                return refuse("bad_role", 400)
            result, refusal = await run(
                sign_in.accounts.set_role, name, body.role
            )
            if refusal:
                return refusal
            was, closed = result
            await asyncio.to_thread(sign_in.account_event, "user_role", name,
                                    role=body.role, was=was, sessions=closed)
        if body.disabled is not None:
            closed, refusal = await run(
                sign_in.accounts.set_disabled, name, body.disabled
            )
            if refusal:
                return refusal
            await asyncio.to_thread(
                sign_in.account_event,
                "user_disabled" if body.disabled else "user_enabled", name,
                sessions=closed,
            )
        return {"status": "changed"}

    @app.post("/api/users/{name}/password")
    async def users_password(name: str):
        """A new temporary password for somebody who forgot theirs."""
        password = auth.temporary_password()
        closed, refusal = await run(
            sign_in.accounts.set_password, name,
            await asyncio.to_thread(auth.hash_password, password), True,
        )
        if refusal:
            return refusal
        await asyncio.to_thread(sign_in.account_event, "user_password", name,
                                temporary=True, sessions=closed)
        return {"name": name, "password": password}

    @app.post("/api/users/{name}/reset-totp")
    async def users_reset_totp(name: str):
        closed, refusal = await run(sign_in.accounts.reset_totp, name)
        if refusal:
            return refusal
        await asyncio.to_thread(sign_in.account_event, "user_totp_reset",
                                name, sessions=closed)
        return {"status": "reset"}

    @app.post("/api/users/{name}/unlock")
    async def users_unlock(name: str):
        was_locked, refusal = await run(sign_in.accounts.unlock, name)
        if refusal:
            return refusal
        if was_locked:
            await asyncio.to_thread(sign_in.account_event, "user_unlocked",
                                    name)
        return {"status": "unlocked" if was_locked else "not_locked"}

    @app.post("/api/users/{name}/logout")
    async def users_logout(name: str):
        """Signs the account out everywhere."""
        closed, refusal = await run(sign_in.accounts.close_sessions, name)
        if refusal:
            return refusal
        await asyncio.to_thread(sign_in.account_event,
                                "user_sessions_closed", name, sessions=closed)
        return {"sessions_closed": closed}

    @app.delete("/api/users/{name}")
    async def users_delete(name: str):
        closed, refusal = await run(sign_in.accounts.delete_user, name)
        if refusal:
            return refusal
        await asyncio.to_thread(sign_in.account_event, "user_deleted", name,
                                sessions=closed)
        return {"status": "deleted"}

    @app.post("/api/auth/password")
    async def auth_password(body: PasswordBody, request: Request):
        return await sign_in.change_password(
            principal(), body.current, body.new, _address(request)
        )
