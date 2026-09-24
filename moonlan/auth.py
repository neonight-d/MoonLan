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
