"""Who may do what: every route of the service, and the least it needs.

One table, here, and nowhere else. The dangerous mistake with rights
is not a wrong entry — it is a route nobody thought about, a check
forgotten on one handler among forty. So rights are not decorators
scattered through server.py: a request is matched against the app's
routes, looked up in RULES, and a route missing from RULES is for
administrators only. tests/test_access.py lists every route the app
has and fails on any that is not written down here.

The table does not apply while sign-in is off (no enabled
administrator yet): then the service works as it did before v0.7.4,
open to everyone — except for managing accounts, which is never open.
"""

from __future__ import annotations

from dataclasses import dataclass

# No sign-in: the page itself, signing in, the health check
PUBLIC = "public"
# Any role, signed in: signing out, one's own password and TOTP
SIGNED_IN = "signed_in"
VIEWER = "viewer"
USER = "user"
ADMIN = "admin"
# Administrator — and never while sign-in is off: a page that could
# create accounts before the first administrator exists would make an
# administrator of whoever found it first
ACCOUNTS = "accounts"

RANK = {"viewer": 1, "user": 2, "admin": 3}

# The web UI's own files: the static mount at "/"
STATIC = "STATIC /"

RULES: dict[str, str] = {
    # the page, and what it needs before anyone has signed in
    "GET /": PUBLIC,
    "GET /index.html": PUBLIC,
    STATIC: PUBLIC,
    # for monitoring from outside: status and version, nothing about
    # the network
    "GET /api/health": PUBLIC,
    # who am I — or "sign-in is off", which the page has to be able to
    # learn before anybody has signed in
    "GET /api/auth/me": PUBLIC,
    "POST /api/auth/login": PUBLIC,
    "POST /api/auth/totp": PUBLIC,

    # one's own session and password, whatever the role
    "POST /api/auth/logout": SIGNED_IN,
    "POST /api/auth/password": SIGNED_IN,

    # looking: the map, cards, ports, STP, the journal, alarms
    "GET /api/topology": VIEWER,
    "GET /api/switch/{ip}/ports": VIEWER,
    "GET /api/stp": VIEWER,
    "GET /api/alarms": VIEWER,
    "GET /api/journal": VIEWER,
    "GET /api/node-menu": VIEWER,
    "GET /api/actions": VIEWER,
    "GET /api/layout": VIEWER,
    "GET /api/search": VIEWER,
    "GET /api/skipped-oids": VIEWER,
    "GET /api/polling": VIEWER,
    "GET /api/status": VIEWER,

    # doing: the server runs something, or the shared picture changes.
    # Even a ping is the server acting on somebody's request, so a
    # viewer has none of it.
    "POST /api/actions": USER,
    # the result is also checked against who started it (server.py)
    "GET /api/actions/{job_id}": USER,
    "POST /api/alarms/{alarm_id}/clear": USER,
    "PATCH /api/host/{mac}": USER,
    "PATCH /api/layout": USER,
    "PATCH /api/layout/{node_id:path}": USER,
    "DELETE /api/layout/{node_id:path}": USER,
    "POST /api/scan": USER,

    # the whole shared picture at once, for everybody at once
    "PUT /api/layout": ADMIN,
    "DELETE /api/layout": ADMIN,

    # FastAPI's own description of the API: a map of every door, so
    # not for everybody
    "GET /openapi.json": ADMIN,
    "GET /docs": ADMIN,
    "GET /docs/oauth2-redirect": ADMIN,
    "GET /redoc": ADMIN,
}


def rule_for(key: str) -> str:
    """The rule for "METHOD /path/template"; not written down means
    administrators only."""
    return RULES.get(key, ADMIN)


@dataclass(frozen=True)
class Principal:
    """Who is asking."""

    name: str
    role: str
    user_id: int | None = None
    session: str = ""          # the session's token hash
    # "password" or "totp": signed in, but with one thing to do before
    # anything else — see STEP_ROUTES
    step: str | None = None
    # diag, signed in from the server's shell: reads, nothing more
    console: bool = False


@dataclass(frozen=True)
class Refusal:
    status: int
    body: dict


# What a session with a step still to do may call. Nothing else, or a
# password marked "change at next sign-in" and an administrator with
# no second factor would be only a suggestion.
STEP_ROUTES: dict[str, set[str]] = {
    "password": {"POST /api/auth/password", "POST /api/auth/logout"},
    "totp": {"POST /api/auth/logout"},
}


def decide(key: str, principal: Principal | None,
           sign_in_on: bool) -> Refusal | None:
    """None when the request may go ahead; otherwise why not."""
    rule = rule_for(key)
    if rule == PUBLIC:
        return None
    if not sign_in_on:
        if rule in (SIGNED_IN, ACCOUNTS):
            return Refusal(403, {"error": "sign_in_off"})
        return None
    if principal is None:
        return Refusal(401, {"error": "sign_in_required"})
    if principal.console:
        if rule == VIEWER and key.startswith("GET "):
            return None
        return Refusal(403, {"error": "forbidden", "need": rule})
    if principal.step:
        if key in STEP_ROUTES.get(principal.step, ()):
            return None
        return Refusal(403, {"error": "step_required", "step": principal.step})
    if rule == SIGNED_IN:
        return None
    need = ADMIN if rule == ACCOUNTS else rule
    if RANK.get(principal.role, 0) >= RANK[need]:
        return None
    return Refusal(
        403, {"error": "forbidden", "need": need, "role": principal.role}
    )
