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
import secrets
import time
from dataclasses import dataclass

from . import https

log = logging.getLogger("moonlan")

try:
    from fido2 import cbor
    from fido2.cose import CoseKey
    from fido2.server import Fido2Server
    from fido2.utils import websafe_decode
    from fido2.webauthn import (
        Aaguid,
        AttestationConveyancePreference,
        AttestedCredentialData,
        AuthenticationResponse,
        PublicKeyCredentialDescriptor,
        PublicKeyCredentialParameters,
        PublicKeyCredentialRpEntity,
        PublicKeyCredentialType,
        PublicKeyCredentialUserEntity,
        ResidentKeyRequirement,
        UserVerificationRequirement,
    )
except ImportError as exc:  # the service runs without it: keys are off
    Fido2Server = None
    FIDO2_MISSING = f"{exc.name or 'fido2'} is not installed ({exc})"
else:
    FIDO2_MISSING = ""

# ES256, EdDSA, RS256: what keys and platforms make today. Nothing
# post-quantum (ML-DSA) yet — no key in a drawer can do it, and an
# algorithm offered is an algorithm that has to be checked right.
ALGORITHMS = (-7, -8, -257)
# A challenge is good once, for five minutes, for the person it was
# made for
CHALLENGE_SECONDS = 300
# How long the browser waits for somebody to touch the key
TIMEOUT_MS = 120_000
# Challenges waiting for an answer. Asking for one needs no sign-in,
# so the table has a ceiling: past it the oldest goes.
PENDING_MAX = 256
LABEL_MAX = 64
TRANSPORTS = ("usb", "nfc", "ble", "hybrid", "internal", "smart-card")


class PasskeyError(Exception):
    """A key's answer refused. `code` goes to the page (i18n
    `err_<code>`); `detail` to the log only."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass
class Pending:
    """A challenge sent to a browser, waiting for the key's answer."""

    kind: str               # "add", "sign_in" or "second"
    state: dict             # fido2's: the challenge, user verification
    expires: float
    user_id: int | None = None
    ticket: str = ""        # "second": the password step's ticket hash
    passwordless: bool = False  # "add": what the person asked for


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


def _descriptor(key: dict) -> "PublicKeyCredentialDescriptor":
    transports = [t for t in (key.get("transports") or "").split(",") if t]
    return PublicKeyCredentialDescriptor(
        type=PublicKeyCredentialType.PUBLIC_KEY, id=key["credential_id"],
        transports=transports or None,
    )


def _plain(options) -> dict:
    """Options as the page gets them: JSON, bytes in base64url."""
    return dict(options)["publicKey"]


class Passkeys:
    """The relying party: listen.public_url's host, its origin, and the
    challenges waiting for an answer. Kept in this process's memory —
    a restart costs whoever was touching their key one more touch."""

    def __init__(self, public_url):
        self.public_url = public_url
        self.why = unavailable(public_url)
        self._pending: dict[str, Pending] = {}
        self._server = None
        if self.why is None:
            origin = public_url.origin
            self._server = Fido2Server(
                PublicKeyCredentialRpEntity(name="MoonLan",
                                            id=public_url.rp_id),
                attestation=AttestationConveyancePreference.NONE,
                # exactly the public address: not "any page under this
                # host name", which fido2 would accept by default
                verify_origin=lambda seen: seen == origin,
            )
            self._server.allowed_algorithms = [
                PublicKeyCredentialParameters(
                    type=PublicKeyCredentialType.PUBLIC_KEY, alg=alg
                )
                for alg in ALGORITHMS
            ]
            self._server.timeout = TIMEOUT_MS

    # ---------- challenges ----------

    def _keep(self, pending: Pending) -> str:
        now = time.time()
        for key in [k for k, p in self._pending.items() if p.expires < now]:
            del self._pending[key]
        while len(self._pending) >= PENDING_MAX:
            del self._pending[next(iter(self._pending))]
        request = secrets.token_urlsafe(24)
        self._pending[request] = pending
        return request

    def take(self, request: str, *kinds: str) -> Pending:
        """The challenge an answer is for — gone from the table whatever
        happens next: an answer is checked once."""
        pending = self._pending.pop(request, None)
        if pending is None or pending.kind not in kinds \
                or pending.expires < time.time():
            raise PasskeyError("challenge_expired")
        return pending

    # ---------- adding a key ----------

    def add_begin(self, user_id: int, name: str, handle: bytes,
                  keys: list[dict], passwordless: bool) -> tuple[str, dict]:
        """(the request id, the options for navigator.credentials.create).

        residentKey and userVerification "preferred": a key that can
        keep the credential and ask for a PIN does, one that cannot is
        still a good second factor. No attestation: which make of key it
        is does not decide anything here. The keys already added are
        excluded, so the same key is not added twice."""
        options, state = self._server.register_begin(
            PublicKeyCredentialUserEntity(name=name, id=handle,
                                          display_name=name),
            [_descriptor(key) for key in keys],
            resident_key_requirement=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED,
            extensions={"credProps": True},
        )
        request = self._keep(Pending(
            "add", state, time.time() + CHALLENGE_SECONDS, user_id,
            passwordless=passwordless,
        ))
        return request, _plain(options)

    def add_finish(self, request: str, user_id: int, answer: dict) -> dict:
        """Checks the new key's answer; the passkeys row to store."""
        pending = self.take(request, "add")
        if pending.user_id != user_id:
            raise PasskeyError("challenge_expired", "made for somebody else")
        try:
            auth_data = self._server.register_complete(pending.state, answer)
        except Exception as exc:  # noqa: BLE001 — any malformed answer
            raise PasskeyError("key_refused", str(exc)) from None
        credential = auth_data.credential_data
        algorithm = credential.public_key.get(3)  # COSE "alg"
        if algorithm not in ALGORITHMS:
            # fido2 checks the signature, not that the algorithm was one
            # offered; a client may send any
            raise PasskeyError("key_refused", f"algorithm {algorithm}")
        extensions = answer.get("clientExtensionResults") or {}
        rk = (extensions.get("credProps") or {}).get("rk")
        transports = (answer.get("response") or {}).get("transports") or []
        return {
            "credential_id": bytes(credential.credential_id),
            "public_key": cbor.encode(dict(credential.public_key)),
            "sign_count": auth_data.counter,
            "aaguid": "" if credential.aaguid == Aaguid.NONE
            else str(credential.aaguid),
            "transports": ",".join(
                t for t in transports if isinstance(t, str) and t in TRANSPORTS
            ),
            "discoverable": None if rk is None else int(bool(rk)),
            "user_verified": int(auth_data.is_user_verified()),
            "passwordless": int(pending.passwordless),
        }

    # ---------- signing in with a key ----------

    def sign_in_begin(self) -> tuple[str, dict]:
        """Without a password. allowCredentials is empty — the key offers
        what it keeps for this host, and says whose it is — and user
        verification is required: the key stands for the password too,
        so it has to have asked for its PIN or a finger."""
        options, state = self._server.authenticate_begin(
            [], user_verification=UserVerificationRequirement.REQUIRED
        )
        request = self._keep(
            Pending("sign_in", state, time.time() + CHALLENGE_SECONDS)
        )
        return request, _plain(options)

    def second_begin(self, user_id: int, ticket: str,
                     keys: list[dict]) -> tuple[str, dict]:
        """After the password: that account's keys only, and no PIN
        needed — the password was the thing it knows, the key is the
        thing it has."""
        options, state = self._server.authenticate_begin(
            [_descriptor(key) for key in keys],
            user_verification=UserVerificationRequirement.DISCOURAGED,
        )
        request = self._keep(Pending(
            "second", state, time.time() + CHALLENGE_SECONDS, user_id,
            ticket=ticket,
        ))
        return request, _plain(options)

    @staticmethod
    def credential_id(answer: dict) -> bytes:
        """Which key answered — to find it before anything is checked."""
        try:
            return websafe_decode(answer["rawId"])
        except Exception:  # noqa: BLE001 — anything malformed
            raise PasskeyError("key_refused", "no credential id") from None

    def check(self, pending: Pending, key: dict,
              answer: dict) -> tuple[int, bool, bytes | None]:
        """The answer against the stored key: the origin, the RP ID
        hash, the challenge, the key's presence and its signature (all
        fido2's), then user verification where it was required. (the
        signature counter, user verified, the user handle)."""
        credential = AttestedCredentialData.create(
            Aaguid.NONE, key["credential_id"],
            CoseKey.parse(cbor.decode(key["public_key"])),
        )
        # user verification is checked here after the signature, not by
        # fido2 before it: a key that did not ask for a PIN gets its own
        # answer, and only once it is known to be the key it claims
        state = {**pending.state,
                 "user_verification": UserVerificationRequirement.DISCOURAGED}
        try:
            self._server.authenticate_complete(state, [credential], answer)
            parsed = AuthenticationResponse.from_dict(answer)
        except Exception as exc:  # noqa: BLE001 — any malformed answer
            raise PasskeyError("key_refused", str(exc)) from None
        data = parsed.response.authenticator_data
        verified = data.is_user_verified()
        if pending.kind == "sign_in" and not verified:
            raise PasskeyError("no_user_verification",
                               "the key did not verify its user")
        return data.counter, verified, parsed.response.user_handle
