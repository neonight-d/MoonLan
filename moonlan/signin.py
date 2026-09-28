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

from . import access, auth, passkeys
from .access import Principal
from .config import AuthConfig
from .db import PASSKEYS_MAX, AccountError, Database

log = logging.getLogger("moonlan")

SESSION_COOKIE = "moonlan_session"
# The same over HTTPS (v0.7.6). The prefix makes the browser take the
# cookie only with Secure, Path=/ and no Domain, so a neighbouring
# subdomain cannot plant one. A session cookie that crossed the network
# in the clear is not carried over to HTTPS: everybody signs in once
# more after the switch.
SECURE_SESSION_COOKIE = "__Host-moonlan_session"


def is_secure(request: Request) -> bool:
    """HTTPS to MoonLan itself, or to a trusted proxy in front of it
    (uvicorn has put the proxy's X-Forwarded-Proto into the scheme)."""
    return request.url.scheme == "https"


def session_cookie(secure: bool) -> str:
    return SECURE_SESSION_COOKIE if secure else SESSION_COOKIE

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


class KeyAddBody(BaseModel):
    # adding a key asks for the password, as binding TOTP does: a
    # session taken over on the way must not be able to add a key of
    # its own to somebody else's account
    password: str = Field(max_length=auth.PASSWORD_MAX)
    # may the key sign in on its own, or only after the password
    passwordless: bool = False


class KeyBeginBody(BaseModel):
    # the password step's ticket for a key as the second step; none for
    # signing in with a key alone
    ticket: str = Field(default="", max_length=128)


class KeyAnswerBody(BaseModel):
    request: str = Field(max_length=64)
    # what navigator.credentials answered, in PublicKeyCredential's
    # JSON form; fido2 checks every part of it
    credential: dict
    label: str = Field(default="", max_length=passkeys.LABEL_MAX)


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
        "can take a session. Serve HTTPS (listen.tls_cert, or a proxy in "
        "front — docs/HTTPS.md); until then bind TOTP with "
        "python -m moonlan.users totp <name> on this machine rather than "
        "from a browser."
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
                 settings: AuthConfig, public_url=None, transport="http"):
        self.accounts = accounts
        self.journal = journal
        self.settings = settings
        # listen.public_url (https.PublicUrl), or None
        self.public_url = public_url
        # the relying party for keys: that address's host and origin
        self.webauthn = passkeys.Passkeys(public_url)
        # how the map reaches people: "tls" (MoonLan's own), "proxy"
        # (HTTPS at a proxy in front) or "http"
        self.transport = transport
        self._tickets: dict[str, Ticket] = {}
        self._purged = 0.0
        self.throttle = Throttle()
        # sign-in as the last request found it: the first administrator
        # created from the console switches it on between two requests,
        # and the log should say so then, not at the next restart
        self._was_on: bool | None = None
        self._console: str = ""    # hash of the token in the console file
        self.console_path: str = ""
        # raises an alarm about MoonLan itself: (type, subject, message);
        # server.py sets it to the alarm engine's
        self.raise_alarm = None

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
        if self.transport == "http":
            log_plain_http()
        with_keys = len(self.accounts.passkey_counts())
        if with_keys and self.webauthn.why:
            log.warning(
                "Keys: %d account(s) have keys, but signing in with a key is "
                "off (%s) — they sign in with TOTP or a recovery code; "
                "python -m moonlan.users reset-passkeys <name> lets somebody "
                "without either bind something new", with_keys,
                self.webauthn.why,
            )

    def _lookup(self, token: str, now: float) -> tuple[bool, Principal | None]:
        on = self.sign_in_on()
        if on and self._was_on is False:
            log.info("Sign-in switched on: an enabled administrator exists "
                     "now. Every open map asks to sign in.")
            if self.transport == "http":
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
            if row["totp_enabled"] or row["passkeys"]:
                # a second factor exists and this session never gave it
                return None
            # no second factor at all: TOTP or a key, before anything else
            step = "totp"
        return Principal(
            name=row["name"], role=row["role"], user_id=row["user_id"],
            session=row["token_hash"], step=step,
        )

    async def who(self, request: Request) -> tuple[bool, Principal | None]:
        """(is sign-in on, who is asking — None for nobody)."""
        on, who = await asyncio.to_thread(
            self._lookup,
            request.cookies.get(session_cookie(is_secure(request)), ""),
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
                      recovery_left: int | None = None,
                      method: str = "password") -> str:
        """`method` goes to the journal: "password", "password+totp",
        "password+recovery", "password+passkey" or "passkey"."""
        self.throttle.succeeded(address, auth.name_key(row["name"]))
        token = auth.new_token()
        self.accounts.add_session(
            auth.token_hash(token), row["id"], now, second_factor, address
        )
        self.accounts.record_login(row["id"], now, rehashed)
        details = {"address": address, "method": method}
        if recovery_left is not None:
            details["recovery_left"] = recovery_left
        self.journal.add_event(
            now, "login", row["name"], json.dumps(details), row["name"],
        )
        log.info(
            "Signed in: %s from %s (%s)%s", row["name"], address, method,
            "" if recovery_left is None else
            f", {recovery_left} recovery code(s) left",
        )
        return token

    def _signed_in(self, token: str, secure: bool = False) -> JSONResponse:
        """The cookie: HttpOnly — no script on the page reads it;
        SameSite=Strict — no other site's page sends it; Path=/. Over
        HTTPS also Secure, under the __Host- name. It lasts as long as a
        session may, so a browser restart does not sign a wall monitor
        out."""
        response = JSONResponse({"status": "signed_in"})
        response.set_cookie(
            session_cookie(secure), token,
            max_age=int(self.settings.session_max_days * 86400),
            httponly=True, samesite="strict", path="/", secure=secure,
        )
        return response

    def https_url(self, request: Request) -> str | None:
        """The protected address to send this browser to, when it came
        over plain HTTP although listen.public_url is https."""
        if self.public_url is None or not self.public_url.secure \
                or is_secure(request):
            return None
        return self.public_url.origin + "/"

    def https_refusal(self, request: Request) -> JSONResponse | None:
        """With an https public address, a password sent over plain HTTP
        is refused — whoever opened the service's port directly would
        otherwise go on sending it in the clear although HTTPS is there.
        The answer carries the address to go to."""
        url = self.https_url(request)
        return refuse("https_required", 403, url=url) if url else None

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
                    address: str, secure: bool = False) -> JSONResponse:
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
        keys = await asyncio.to_thread(self.accounts.passkeys, row["id"])
        if row["totp_enabled"] or keys:
            ticket = auth.new_token()
            self._drop_old_tickets(now)
            self._tickets[auth.token_hash(ticket)] = Ticket(
                row["id"], now + TICKET_SECONDS, address, rehashed=rehashed
            )
            # what the second step can be, for the page to offer: a key
            # counts only while the server can check one
            methods = []
            if row["totp_enabled"]:
                methods.append("totp")
            if keys and not self.webauthn.why:
                methods.append("passkey")
            if auth.recovery_left(row["recovery"]):
                methods.append("recovery")
            return JSONResponse({"status": "code_required", "ticket": ticket,
                                 "methods": methods})
        token = await asyncio.to_thread(
            self._open_session, row, False, address, now, rehashed
        )
        return self._signed_in(token, secure)

    def _drop_old_tickets(self, now: float) -> None:
        for key in [k for k, t in self._tickets.items() if t.expires < now]:
            del self._tickets[key]

    async def second_step(self, ticket: str, code: str,
                          address: str, secure: bool = False) -> JSONResponse:
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
        if row is None or row["disabled"]:
            del self._tickets[key]
            return refuse("ticket_expired", 401)
        if row["locked_until"] > now:
            del self._tickets[key]
            return self._locked(row["locked_until"], now)
        # a code from the app, if there is one; a recovery code works for
        # an account whose second factor is a key just the same
        step = auth.match_totp(row["totp_secret"], code, now) \
            if row["totp_enabled"] else None
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
                    "password+recovery",
                )
                return self._signed_in(token, secure)
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
            self._open_session, row, True, address, now, entry.rehashed,
            None, "password+totp",
        )
        return self._signed_in(token, secure)

    # ---------- signing in with a key ----------

    async def passkey_begin(self, ticket: str, address: str) -> JSONResponse:
        """A challenge for a key: without a ticket, to sign in with the
        key alone (it says whose it is); with the password step's
        ticket, as the second step, offered that account's keys only."""
        off = self._keys_off()
        if off is not None:
            return off
        now = time.time()
        too_soon = self._too_soon(address, now)
        if too_soon is not None:
            return too_soon
        if not ticket:
            request, options = self.webauthn.sign_in_begin()
            return JSONResponse({"request": request, "options": options})
        self._drop_old_tickets(now)
        key = auth.token_hash(ticket)
        entry = self._tickets.get(key)
        if entry is None:
            return refuse("ticket_expired", 401)
        keys = await asyncio.to_thread(self.accounts.passkeys, entry.user_id)
        if not keys:
            return refuse("no_keys", 409)
        request, options = self.webauthn.second_begin(entry.user_id, key, keys)
        return JSONResponse({"request": request, "options": options})

    async def passkey_finish(self, request: str, answer: dict, address: str,
                             secure: bool = False) -> JSONResponse:
        """The key's answer. Checked: the challenge (once, five
        minutes), the origin, the RP ID hash, the signature, whose key it
        is, user verification without a password, and the signature
        counter. A failure counts like a wrong password — per address,
        and per account once it is known whose key answered."""
        off = self._keys_off()
        if off is not None:
            return off
        now = time.time()
        too_soon = self._too_soon(address, now)
        if too_soon is not None:
            return too_soon
        try:
            pending = self.webauthn.take(request, "sign_in", "second")
            key = await asyncio.to_thread(
                self.accounts.passkey_by_credential,
                self.webauthn.credential_id(answer),
            )
        except passkeys.PasskeyError as error:
            self.throttle.address_failed(address, now)
            log.warning("Sign-in with a key failed from %s: %s", address, error)
            return refuse(error.code, 401)
        row = None if key is None else await asyncio.to_thread(
            self.accounts.user_by_id, key["user_id"]
        )
        if row is None:
            self.throttle.address_failed(address, now)
            log.warning("Sign-in with a key failed from %s: a key this "
                        "service does not know", address)
            return refuse("key_unknown", 401)
        second = pending.kind == "second"
        entry = self._tickets.get(pending.ticket) if second else None
        if entry is not None and entry.expires < now:
            # five minutes for the whole second step, a key or a code
            self._tickets.pop(pending.ticket, None)
            entry = None
        if second and (entry is None or entry.user_id != row["id"]):
            # the ticket ran out, or another account's key answered
            locked = await self._failed(
                row["name"], row, address, now,
                "a key that is not this account's" if entry else
                "the ticket ran out",
            )
            return locked or refuse(
                "key_refused" if entry else "ticket_expired", 401
            )
        if row["locked_until"] > now:
            return self._locked(row["locked_until"], now)
        try:
            counter, _, handle = self.webauthn.check(pending, key, answer)
        except passkeys.PasskeyError as error:
            locked = await self._failed(row["name"], row, address, now,
                                        f"key refused ({error.detail})")
            return locked or refuse(error.code, 401)
        if not second:
            # the key says whose it is, and must say what the table says
            owner = row["webauthn_user_id"] or b""
            if not handle or not hmac.compare_digest(bytes(handle),
                                                     bytes(owner)):
                locked = await self._failed(
                    row["name"], row, address, now,
                    "the key's user handle is not its owner's",
                )
                return locked or refuse("key_refused", 401)
            if not key["passwordless"]:
                log.info("Sign-in with key %r of %s refused from %s: it was "
                         "added to work after the password only",
                         key["label"], row["name"], address)
                return refuse("key_second_only", 401)
        stored = key["sign_count"]
        if stored > 0 and counter > 0 and counter <= stored:
            await self._clone_suspected(row, key, counter, address, now)
            locked = await self._failed(row["name"], row, address, now,
                                        "the key's counter went back")
            return locked or refuse("key_refused", 401)
        # a key that does not count (0) is taken; the highest count seen
        # is kept for the next comparison
        await asyncio.to_thread(self.accounts.use_passkey, key["id"],
                                max(stored, counter), now)
        if row["disabled"]:
            log.warning("Sign-in refused for %s from %s: account disabled",
                        row["name"], address)
            return refuse("disabled", 403)
        if second:
            self._tickets.pop(pending.ticket, None)
        token = await asyncio.to_thread(
            self._open_session, row, True, address, now,
            entry.rehashed if entry else None, None,
            "password+passkey" if second else "passkey",
        )
        return self._signed_in(token, secure)

    async def _clone_suspected(self, row: dict, key: dict, counter: int,
                               address: str, now: float) -> None:
        """The key answered with a signature counter at or below the one
        it had already reached. A key counts up with every signature;
        going back means two keys hold the same secret — a copy."""
        keys = await asyncio.to_thread(self.accounts.passkeys, row["id"])
        number = next((k["number"] for k in keys if k["id"] == key["id"]), 0)
        message = (
            f"key {key['label']!r} of {row['name']} answered with signature "
            f"counter {counter} after {key['sign_count']} (from {address}): "
            f"a copy of the key may exist. Signing in with it was refused; "
            f"if the key is not with its owner, remove it — python -m "
            f"moonlan.users remove-passkey {row['name']} {number}"
        )
        log.error("Keys: %s", message)
        await asyncio.to_thread(
            self.journal.add_event, now, "passkey_clone_suspected",
            row["name"], json.dumps({
                "label": key["label"], "address": address,
                "counter": counter, "stored": key["sign_count"],
            }),
        )
        if self.raise_alarm is not None:
            await self.raise_alarm(
                "passkey_clone_suspected", f"{row['name']}: {key['label']}",
                message,
            )

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
        response.delete_cookie(SECURE_SESSION_COOKIE, path="/", secure=True)
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

    # ---------- one's own keys ----------

    def _keys_off(self) -> JSONResponse | None:
        if self.webauthn.why:
            return refuse("passkeys_off", 409, why=self.webauthn.why)
        return None

    async def passkey_add_begin(self, who: Principal | None, password: str,
                                passwordless: bool,
                                address: str) -> JSONResponse:
        """The challenge for a new key, once the password is right."""
        if who is None or who.user_id is None:
            return refuse("sign_in_required", 401)
        off = self._keys_off()
        if off is not None:
            return off
        row, right = await asyncio.to_thread(
            self._check_password, who.name, password
        )
        if row is None or not right:
            locked = await self._failed(who.name, row, address, time.time(),
                                        "wrong password adding a key")
            if locked is not None:
                await asyncio.to_thread(self.accounts.close_sessions,
                                        who.name)
                return locked
            return refuse("wrong_password", 403)
        keys = await asyncio.to_thread(self.accounts.passkeys, row["id"])
        if len(keys) >= PASSKEYS_MAX:
            return refuse("too_many_keys", 409, most=PASSKEYS_MAX)
        handle = await asyncio.to_thread(self.accounts.webauthn_user_id,
                                         row["id"])
        request, options = self.webauthn.add_begin(
            row["id"], row["name"], handle, keys, passwordless
        )
        return JSONResponse({"request": request, "options": options})

    async def passkey_add_finish(self, who: Principal | None, request: str,
                                 answer: dict, label: str,
                                 address: str) -> JSONResponse:
        """The key's answer: checked, stored, journaled. The first key of
        an account that has no recovery codes brings them — the same
        way back in as with TOTP, shown this once."""
        if who is None or who.user_id is None:
            return refuse("sign_in_required", 401)
        off = self._keys_off()
        if off is not None:
            return off
        try:
            key = self.webauthn.add_finish(request, who.user_id, answer)
        except passkeys.PasskeyError as error:
            log.warning("Adding a key refused for %s from %s: %s",
                        who.name, address, error)
            return refuse(error.code, 400)
        # "Without a password" needs a key that keeps the credential
        # itself (nothing to list it from) and asked for a PIN or a
        # finger (else whoever holds it is in). One that cannot is kept
        # as a second factor, and the answer says so.
        note = None
        if key["passwordless"]:
            if not key["user_verified"]:
                note = "no_user_verification"
            elif key["discoverable"] == 0:
                note = "not_discoverable"
            if note:
                key["passwordless"] = 0
        row = await asyncio.to_thread(self.accounts.user_by_id, who.user_id)
        if row is None:
            return refuse("sign_in_required", 401)
        keys = await asyncio.to_thread(self.accounts.passkeys, row["id"])
        key["label"] = label.strip()[:passkeys.LABEL_MAX] or f"#{len(keys) + 1}"
        codes, stored = None, None
        if not row["recovery"]:
            codes = auth.new_recovery_codes()
            stored = await asyncio.to_thread(auth.hash_recovery_codes, codes)
        try:
            number, closed = await asyncio.to_thread(
                self.accounts.add_passkey, row["id"], key, stored, who.session
            )
        except AccountError as error:
            return refuse(error.code, 409)
        await asyncio.to_thread(
            self.journal.add_event, time.time(), "user_passkey_added",
            row["name"], json.dumps({
                "number": number, "label": key["label"],
                "passwordless": bool(key["passwordless"]), "sessions": closed,
            }), row["name"],
        )
        log.info(
            "Key added by %s from %s: #%d %r, %s%s; %d other session(s) "
            "closed", row["name"], address, number, key["label"],
            "signs in without a password" if key["passwordless"]
            else "a second factor", f" ({note})" if note else "", closed,
        )
        return JSONResponse({
            "status": "added", "number": number,
            "passwordless": bool(key["passwordless"]), "note": note,
            "recovery": codes,
        })

    async def passkey_remove(self, who: Principal | None,
                             key_id: int) -> JSONResponse:
        """One of one's own keys. An administrator's last second factor
        is refused. The account's other sessions are closed: a key is
        removed because it was lost, and whoever found it may be signed
        in with it."""
        if who is None or who.user_id is None:
            return refuse("sign_in_required", 401)
        try:
            key, closed = await asyncio.to_thread(
                self.accounts.remove_passkey, who.name, key_id, who.session
            )
        except AccountError as error:
            return refuse(error.code, 404 if error.code == "unknown_key"
                          else 409)
        await asyncio.to_thread(
            self.journal.add_event, time.time(), "user_passkey_removed",
            who.name, json.dumps({"label": key["label"], "sessions": closed}),
            who.name,
        )
        log.info("Key %r removed by %s themselves; %d other session(s) "
                 "closed", key["label"], who.name, closed)
        return JSONResponse({"status": "removed", "sessions_closed": closed})

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
                "passkeys": [key_summary(key)
                             for key in self.accounts.passkeys(row["id"])],
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

    def passkey_info(self) -> dict:
        """What the page needs to offer a key, or to say why not: the
        server's reason (None when it can take one) and the address the
        keys are bound to, for the page to compare with its own."""
        public = self.webauthn.public_url
        return {
            "why": self.webauthn.why,
            "origin": public.origin if public else None,
        }

    def me(self, who: Principal) -> dict:
        row = self.accounts.user(who.name) or {}
        keys = self.accounts.passkeys(row["id"]) if row else []
        return {
            "sign_in": True, "name": who.name, "role": who.role,
            "step": who.step,
            "totp": bool(row.get("totp_enabled")),
            "recovery_left": auth.recovery_left(row.get("recovery", "")),
            "keys": [key_summary(key) for key in keys],
        }


def key_summary(key: dict) -> dict:
    """A key as the page shows it: not its credential id, not its
    public half."""
    return {
        "id": key["id"], "number": key["number"], "label": key["label"],
        "passwordless": bool(key["passwordless"]),
        "created_at": key["created_at"], "last_used": key["last_used"],
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


def same_origin(request: Request, public_url=None) -> bool:
    """The page that sent this is the map itself.

    With SameSite=Strict on the cookie this closes cross-site request
    forgery: a page elsewhere can make the browser send a request, but
    not with our cookie and not with our Origin. A request without an
    Origin is refused — every browser sends one with a POST, PUT, PATCH
    or DELETE, and a script can send one too.

    "The map itself" is the public address when one is set — what a
    proxy in front shows people, whatever it forwards as the scheme —
    or the address this request was made to: the map opened by IP or by
    another name keeps working with a password (it is still the map, and
    a page on another site cannot send our Host with its own Origin).
    Behind a trusted proxy the scheme of that address is the one the
    proxy reports (X-Forwarded-Proto, applied by uvicorn), not http.
    """
    origin = request.headers.get("origin", "").lower()
    if not origin:
        return False
    if public_url is not None and origin == public_url.origin:
        return True
    host = request.headers.get("host", "")
    return origin == f"{request.url.scheme}://{host}".lower()


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
                and not same_origin(request, self.signin.public_url):
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
        # over plain HTTP with an https public address: where to go
        # instead — the form says so before a password is typed
        extra = {"passkeys": sign_in.passkey_info()}
        if url := sign_in.https_url(request):
            extra["https_url"] = url
        if who is None:
            return JSONResponse(
                {"error": "sign_in_required", "sign_in": True, **extra},
                status_code=401,
            )
        return {**await asyncio.to_thread(sign_in.me, who), **extra}

    @app.post("/api/auth/login")
    async def auth_login(body: LoginBody, request: Request):
        """Name and password. With TOTP on, the answer is "a code is
        needed" and a ticket for /api/auth/totp — not a session."""
        if not await asyncio.to_thread(sign_in.sign_in_on):
            return refuse("sign_in_off", 409)
        return sign_in.https_refusal(request) or await sign_in.login(
            body.name, body.password, _address(request), is_secure(request)
        )

    @app.post("/api/auth/totp")
    async def auth_totp(body: CodeBody, request: Request):
        return sign_in.https_refusal(request) or await sign_in.second_step(
            body.ticket, body.code, _address(request), is_secure(request)
        )

    @app.post("/api/auth/passkey/begin")
    async def auth_passkey_begin(body: KeyBeginBody, request: Request):
        """Signing in with a key: the options for
        navigator.credentials.get(), alone or after the password."""
        if not await asyncio.to_thread(sign_in.sign_in_on):
            return refuse("sign_in_off", 409)
        return await sign_in.passkey_begin(body.ticket, _address(request))

    @app.post("/api/auth/passkey/finish")
    async def auth_passkey_finish(body: KeyAnswerBody, request: Request):
        return await sign_in.passkey_finish(
            body.request, body.credential, _address(request),
            is_secure(request),
        )

    @app.post("/api/auth/logout")
    async def auth_logout():
        return await sign_in.logout(principal())

    @app.post("/api/auth/totp/setup")
    async def auth_totp_setup():
        return await sign_in.totp_setup(principal())

    @app.post("/api/auth/passkeys/begin")
    async def auth_passkeys_begin(body: KeyAddBody, request: Request):
        """A new key for oneself: the password, then the options for
        navigator.credentials.create()."""
        return sign_in.https_refusal(request) or \
            await sign_in.passkey_add_begin(
                principal(), body.password, body.passwordless,
                _address(request),
            )

    @app.post("/api/auth/passkeys/finish")
    async def auth_passkeys_finish(body: KeyAnswerBody, request: Request):
        return await sign_in.passkey_add_finish(
            principal(), body.request, body.credential, body.label,
            _address(request),
        )

    @app.delete("/api/auth/passkeys/{key_id}")
    async def auth_passkeys_remove(key_id: int):
        return await sign_in.passkey_remove(principal(), key_id)

    @app.post("/api/auth/totp/confirm")
    async def auth_totp_confirm(body: BindBody, request: Request):
        return sign_in.https_refusal(request) or await sign_in.totp_confirm(
            principal(), body.code, body.password, _address(request)
        )

    def account_refusal(error: AccountError) -> JSONResponse:
        status = {"unknown": 404, "exists": 409, "last_admin": 409,
                  "unknown_key": 404, "last_factor": 409}
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

    @app.delete("/api/users/{name}/passkeys/{key_id}")
    async def users_remove_passkey(name: str, key_id: int):
        """One key of somebody's — a lost one. Not an administrator's
        last second factor; resetting all of them is the way for that."""
        result, refusal = await run(
            sign_in.accounts.remove_passkey, name, key_id
        )
        if refusal:
            return refusal
        key, closed = result
        await asyncio.to_thread(sign_in.account_event, "user_passkey_removed",
                                name, label=key["label"], sessions=closed)
        return {"status": "removed", "sessions_closed": closed}

    @app.post("/api/users/{name}/reset-passkeys")
    async def users_reset_passkeys(name: str):
        """Every key of an account: for somebody who lost them all. An
        administrator without TOTP binds a new second factor at the next
        sign-in, before anything else."""
        result, refusal = await run(sign_in.accounts.reset_passkeys, name)
        if refusal:
            return refusal
        removed, closed = result
        await asyncio.to_thread(sign_in.account_event, "user_passkeys_reset",
                                name, keys=removed, sessions=closed)
        return {"status": "reset", "removed": removed,
                "sessions_closed": closed}

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
        return sign_in.https_refusal(request) or await sign_in.change_password(
            principal(), body.current, body.new, _address(request)
        )
