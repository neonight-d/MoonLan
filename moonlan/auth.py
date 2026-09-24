"""Passwords, names and the other small pieces of signing in.

Standard library only. MoonLan added no dependency for this: scrypt is
in hashlib, and nothing here needs more than hashlib, hmac and secrets.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from urllib.parse import quote

ROLES = ("viewer", "user", "admin")

# scrypt's cost. n=2**14, r=8 is 16 MB and about 40 ms per check on the
# machine this was written on: nothing for one sign-in, a wall for
# somebody trying a list of passwords against a copy of the database.
# The parameters go into every stored hash, so raising them later
# leaves the old hashes readable — they are re-hashed at the next
# successful sign-in (see needs_rehash).
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16
HASH_BYTES = 32

PASSWORD_MIN = 10
# Anything longer is not a password somebody types, and scrypt should
# not be handed a megabyte by whoever cares to send one
PASSWORD_MAX = 1024

# Letters of any alphabet, digits, and . _ - : a name people type at a
# sign-in form, and one that can never look like the "@console" marker
# the journal uses for what was done from the server's shell.
NAME_RE = re.compile(r"[\w.-]{1,32}")


def name_key(name: str) -> str:
    """What makes two names the same name: "Anton" and "anton" are one
    person. casefold() rather than SQLite's NOCASE, which folds ASCII
    only and would let "Антон" and "антон" be two accounts."""
    return name.casefold()


def name_problem(name: str) -> str | None:
    """Why this cannot be an account name, or None."""
    if not NAME_RE.fullmatch(name or ""):
        return "bad_name"
    return None


def password_problem(name: str, password: str) -> str | None:
    """Why this password is refused, or None.

    Long enough, and not the name. No "a capital, a digit and a
    symbol": those rules make passwords shorter and more predictable
    (Password1!), not stronger — length is what counts.
    """
    if len(password) < PASSWORD_MIN:
        return "too_short"
    if len(password) > PASSWORD_MAX:
        return "too_long"
    if name_key(password) == name_key(name):
        return "same_as_name"
    return None


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int,
            length: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=length,
        # 128 * r * n is what it needs; OpenSSL's default ceiling is
        # 32 MB, which parameters raised later could run into
        maxmem=256 * r * n + 1024 * 1024,
    )


def hash_password(password: str) -> str:
    """scrypt$n$r$p$salt$hash — the parameters travel with the hash."""
    salt = secrets.token_bytes(SALT_BYTES)
    digest = _scrypt(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P, HASH_BYTES)
    return "$".join((
        "scrypt", str(SCRYPT_N), str(SCRYPT_R), str(SCRYPT_P),
        _b64(salt), _b64(digest),
    ))


def verify_password(password: str, stored: str) -> bool:
    """True when the password matches; never raises on a bad record."""
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = _scrypt(
            password, base64.b64decode(salt), int(n), int(r), int(p),
            len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def needs_rehash(stored: str) -> bool:
    """A hash made with other parameters than today's."""
    parts = stored.split("$")
    return len(parts) != 6 or parts[:4] != [
        "scrypt", str(SCRYPT_N), str(SCRYPT_R), str(SCRYPT_P),
    ]


_dummy_hash: str | None = None


def burn_a_check(password: str) -> None:
    """Spends what verify_password would, and answers nothing.

    For a name nobody has: a sign-in that returned at once for an
    unknown name and after 40 ms for a known one would say which names
    exist to anyone with a stopwatch.
    """
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_password(secrets.token_urlsafe(16))
    verify_password(password, _dummy_hash)


# ---------- TOTP (RFC 6238) ----------

# HMAC-SHA1, six digits, thirty seconds: what every authenticator app
# and every OATH hardware key takes without asking, so the secret can
# live on a key rather than on a phone.
TOTP_DIGITS = 6
TOTP_STEP = 30
TOTP_SECRET_BYTES = 20
ISSUER = "MoonLan"


def new_totp_secret() -> str:
    """Twenty random bytes, in the base32 the apps expect."""
    return base64.b32encode(
        secrets.token_bytes(TOTP_SECRET_BYTES)
    ).decode("ascii").rstrip("=")


def secret_bytes(secret: str) -> bytes:
    """base32 as people and apps write it: any case, spaces, no padding."""
    text = "".join(secret.split()).upper()
    return base64.b32decode(text + "=" * (-len(text) % 8))


def hotp(key: bytes, counter: int, digits: int = TOTP_DIGITS,
         digest: str = "sha1") -> str:
    """RFC 4226: the code for one counter value."""
    mac = hmac.new(key, counter.to_bytes(8, "big"), digest).digest()
    offset = mac[-1] & 0x0F
    value = int.from_bytes(mac[offset:offset + 4], "big") & 0x7FFFFFFF
    return str(value % 10 ** digits).zfill(digits)


def totp_code(key: bytes, at: float, digits: int = TOTP_DIGITS,
              step: int = TOTP_STEP, digest: str = "sha1") -> str:
    """RFC 6238: the code for the moment `at` (unix time). The number
    of digits is a parameter because the RFC's own test vectors have
    eight."""
    return hotp(key, int(at // step), digits, digest)


def totp_step(at: float) -> int:
    return int(at // TOTP_STEP)


def match_totp(secret: str, code: str, at: float) -> int | None:
    """The step `code` belongs to, or None.

    One step either side is accepted: a phone or a key whose clock is
    half a minute off still works. Whether that step has been spent
    already is the caller's question — see Database.spend_totp_step.
    """
    code = "".join((code or "").split())
    if len(code) != TOTP_DIGITS or not code.isdigit():
        return None
    try:
        key = secret_bytes(secret)
    except (ValueError, TypeError):
        return None
    now = totp_step(at)
    for step in (now - 1, now, now + 1):
        if hmac.compare_digest(hotp(key, step), code):
            return step
    return None


def otpauth_uri(name: str, secret: str) -> str:
    """The otpauth:// line an app scans as a QR code, and the one
    `ykman oath accounts uri` writes to a key."""
    label = quote(f"{ISSUER}:{name}", safe=":@")
    return (
        f"otpauth://totp/{label}?secret={secret}&issuer={quote(ISSUER)}"
        f"&algorithm=SHA1&digits={TOTP_DIGITS}&period={TOTP_STEP}"
    )


# ---------- recovery codes ----------

RECOVERY_CODES = 8
# No 0/o, 1/l/i: a code copied onto paper has to survive being read
# back off it
RECOVERY_ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"
RECOVERY_LENGTH = 10


def new_recovery_codes() -> list[str]:
    """Eight one-time codes, xxxxx-xxxxx, shown once."""
    codes = []
    for _ in range(RECOVERY_CODES):
        raw = "".join(
            secrets.choice(RECOVERY_ALPHABET) for _ in range(RECOVERY_LENGTH)
        )
        codes.append(raw[:5] + "-" + raw[5:])
    return codes


def normal_recovery_code(code: str) -> str:
    return "".join(ch for ch in (code or "").lower() if ch.isalnum())


def hash_recovery_codes(codes: list[str]) -> str:
    """What the database keeps: a hash per code, one per line. The
    same scrypt as passwords — a copy of the database must not hand
    out working codes."""
    return "\n".join(hash_password(normal_recovery_code(c)) for c in codes)


def spend_recovery_code(code: str, stored: str) -> str | None:
    """The stored hashes minus the one `code` matches, or None when it
    matches none. A code used is a code gone."""
    code = normal_recovery_code(code)
    hashes = [h for h in (stored or "").splitlines() if h]
    if len(code) != RECOVERY_LENGTH:
        return None
    for i, stored_hash in enumerate(hashes):
        if verify_password(code, stored_hash):
            return "\n".join(hashes[:i] + hashes[i + 1:])
    return None


def recovery_left(stored: str) -> int:
    return len([h for h in (stored or "").splitlines() if h])


# ---------- sessions ----------

def new_token() -> str:
    """A session or sign-in token: 32 random bytes, URL-safe."""
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    """What the database keeps instead of the token: a stolen copy of
    the database signs nobody in."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
