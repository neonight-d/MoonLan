"""Signing in with a key: WebAuthn — passkeys and security keys (v0.7.6).

The browser does the talking to the key; this checks what comes back.
A signature over the page's origin and a one-time challenge, made by a
key whose public half was stored when it was added — that is what makes
a key worth more than a password: it cannot be typed into a fake page,
because the fake page's origin is not the one signed.

Checking it needs CBOR and elliptic curves the standard library does
not have: python-fido2, with `cryptography` under it — the one
dependency MoonLan takes for this. Without it the service runs as
before and says why signing in with a key is off.
"""

from __future__ import annotations

import importlib.metadata
import logging

from . import https

log = logging.getLogger("moonlan")

try:
    from fido2.server import Fido2Server
except ImportError as exc:  # the service runs without it: keys are off
    Fido2Server = None
    FIDO2_MISSING = f"{exc.name or 'fido2'} is not installed ({exc})"
else:
    FIDO2_MISSING = ""


def fido2_version() -> str | None:
    """The installed python-fido2, or None."""
    if Fido2Server is None:
        return None
    try:
        return importlib.metadata.version("fido2")
    except importlib.metadata.PackageNotFoundError:
        return "?"


def unavailable(public_url) -> str | None:
    """Why the server cannot take a key, or None. What only the browser
    knows — a secure page, the address it is at, WebAuthn itself — the
    page checks; these codes are the server's part of the same list, in
    the same order (i18n `passkeyWhy_<code>`)."""
    problem = https.passkey_problem(public_url)
    if problem:
        return problem
    if Fido2Server is None:
        return "no_fido2"
    return None


def log_state(public_url) -> None:
    """One line at startup, only when there is something to act on: the
    address is right and yet keys are off because the package is not
    there. Problems with the address itself are _log_public_url's."""
    if unavailable(public_url) == "no_fido2":
        log.warning(
            "Signing in with a key is off: %s. Install it into MoonLan's "
            "environment — pip install -r requirements.txt — and restart; "
            "docs/OPERATIONS.md says what to do when cryptography will not "
            "build.", FIDO2_MISSING,
        )
