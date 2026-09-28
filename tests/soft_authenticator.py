"""A WebAuthn authenticator in software, for the tests: it answers the
options MoonLan sends the way a browser with a security key would —
JSON with base64url strings, as PublicKeyCredential.toJSON() makes it.

Everything a real key could get wrong, it can be told to get wrong: the
origin the browser saw, the RP ID it hashed, user verification, the
signature counter.
"""

from __future__ import annotations

import hashlib
import json
import os

from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from fido2 import cbor
from fido2.cose import ES256, EdDSA
from fido2.utils import websafe_decode, websafe_encode

UP, UV, AT = 0x01, 0x04, 0x40
AAGUID = bytes.fromhex("0123456789abcdef0123456789abcdef")


def b64(data: bytes) -> str:
    return websafe_encode(data)


class SoftKey:
    """One credential held by the software authenticator."""

    def __init__(self, alg: int, rp_id: str, user_handle: bytes,
                 credential_id: bytes | None = None):
        self.alg = alg
        self.rp_id = rp_id
        self.user_handle = user_handle
        self.credential_id = credential_id or os.urandom(32)
        self.counter = 0
        if alg == -7:
            self.private = ec.generate_private_key(ec.SECP256R1())
            self.cose = ES256.from_cryptography_key(self.private.public_key())
        elif alg == -8:
            self.private = ed25519.Ed25519PrivateKey.generate()
            self.cose = EdDSA.from_cryptography_key(self.private.public_key())
        else:
            raise ValueError(f"no such algorithm here: {alg}")

    def sign(self, data: bytes) -> bytes:
        if self.alg == -7:
            from cryptography.hazmat.primitives import hashes
            return self.private.sign(data, ec.ECDSA(hashes.SHA256()))
        return self.private.sign(data)


def _client_data(kind: str, challenge: str, origin: str) -> bytes:
    return json.dumps({
        "type": kind, "challenge": challenge, "origin": origin,
        "crossOrigin": False,
    }, separators=(",", ":")).encode()


def create(options: dict, origin: str, alg: int = -7, *, uv: bool = True,
           rk: bool | None = True, rp_id: str | None = None,
           transports=("usb",)) -> tuple[SoftKey, dict]:
    """navigator.credentials.create() — (the new key, the response)."""
    public = options["publicKey"] if "publicKey" in options else options
    offered = [p["alg"] for p in public["pubKeyCredParams"]]
    if alg not in offered:
        raise ValueError(f"{alg} not offered: {offered}")
    rp_id = rp_id or public["rp"]["id"]
    key = SoftKey(alg, rp_id, websafe_decode(public["user"]["id"]))
    excluded = {websafe_decode(c["id"])
                for c in public.get("excludeCredentials") or []}
    assert key.credential_id not in excluded
    credential_data = (
        AAGUID + len(key.credential_id).to_bytes(2, "big")
        + key.credential_id + cbor.encode(dict(key.cose))
    )
    flags = UP | AT | (UV if uv else 0)
    auth_data = (hashlib.sha256(rp_id.encode()).digest()
                 + bytes([flags]) + key.counter.to_bytes(4, "big")
                 + credential_data)
    attestation = cbor.encode({"fmt": "none", "attStmt": {},
                               "authData": auth_data})
    extensions = {} if rk is None else {"credProps": {"rk": rk}}
    return key, {
        "id": b64(key.credential_id), "rawId": b64(key.credential_id),
        "type": "public-key",
        "response": {
            "clientDataJSON": b64(_client_data(
                "webauthn.create", public["challenge"], origin
            )),
            "attestationObject": b64(attestation),
            "transports": list(transports),
        },
        "clientExtensionResults": extensions,
        "authenticatorAttachment": "cross-platform",
    }


def get(options: dict, origin: str, key: SoftKey, *, uv: bool = True,
        counter: int | None = None, rp_id: str | None = None,
        user_handle: bool = True) -> dict:
    """navigator.credentials.get() — the response. `counter` sets the
    signature counter; by default it goes up by one each time."""
    public = options["publicKey"] if "publicKey" in options else options
    allowed = [websafe_decode(c["id"])
               for c in public.get("allowCredentials") or []]
    if allowed:
        assert key.credential_id in allowed, "not one of the allowed keys"
    if counter is None:
        key.counter += 1
    else:
        key.counter = counter
    rp_id = rp_id or key.rp_id
    flags = UP | (UV if uv else 0)
    auth_data = (hashlib.sha256(rp_id.encode()).digest()
                 + bytes([flags]) + key.counter.to_bytes(4, "big"))
    client_data = _client_data("webauthn.get", public["challenge"], origin)
    signature = key.sign(auth_data + hashlib.sha256(client_data).digest())
    return {
        "id": b64(key.credential_id), "rawId": b64(key.credential_id),
        "type": "public-key",
        "response": {
            "clientDataJSON": b64(client_data),
            "authenticatorData": b64(auth_data),
            "signature": b64(signature),
            "userHandle": b64(key.user_handle) if user_handle else None,
        },
        "clientExtensionResults": {},
        "authenticatorAttachment": "cross-platform",
    }
