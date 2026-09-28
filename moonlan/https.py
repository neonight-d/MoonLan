"""HTTPS: the address people open the map at, the certificate MoonLan
serves itself, and the proxy that may stand in front of it (v0.7.6).

The service used to learn its own address from each request's Host
header. For signing in with a key that is not enough: the browser
signs the page's origin, and the server has to compare it with the
address it itself holds to be right, not with whatever the client sent.
That address is `listen.public_url`.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from urllib.parse import urlsplit

DEFAULT_PORTS = {"http": 80, "https": 443}


@dataclass(frozen=True)
class PublicUrl:
    """listen.public_url, taken apart."""

    scheme: str        # "https" or "http"
    host: str          # lower case, no brackets
    port: int | None   # only when it is not the scheme's default
    is_ip: bool

    @property
    def origin(self) -> str:
        """scheme://host[:port] — exactly what a browser sends as Origin
        and signs into a WebAuthn response."""
        host = f"[{self.host}]" if ":" in self.host else self.host
        port = f":{self.port}" if self.port else ""
        return f"{self.scheme}://{host}{port}"

    @property
    def secure(self) -> bool:
        return self.scheme == "https"

    @property
    def rp_id(self) -> str:
        """The WebAuthn relying party id: the host name, without the port.
        Empty for an address: a passkey cannot be bound to an IP."""
        return "" if self.is_ip else self.host

    @property
    def local(self) -> bool:
        """localhost is a secure context over plain http too — every
        browser allows WebAuthn there, which is how it is developed."""
        return self.host == "localhost" or self.host.endswith(".localhost")


def parse_public_url(text: str) -> tuple[PublicUrl | None, list[str]]:
    """(the parsed address or None, what was wrong with it). An address
    that cannot be used is ignored — everything that does not need it
    works as without it — and the problems go to the log and diag."""
    text = (text or "").strip()
    if not text:
        return None, []
    parts = urlsplit(text)
    problems = []
    if parts.scheme not in DEFAULT_PORTS:
        problems.append(
            f"listen.public_url {text!r}: the scheme must be https or http"
        )
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        problems.append(
            f"listen.public_url {text!r}: an address only — no path, "
            f"query or fragment"
        )
    if parts.username or parts.password:
        problems.append(f"listen.public_url {text!r}: no user or password")
    try:
        port = parts.port
    except ValueError:
        problems.append(f"listen.public_url {text!r}: the port is not a number")
        port = None
    if not parts.hostname:
        problems.append(f"listen.public_url {text!r}: there is no host name")
    if problems:
        return None, problems
    host = parts.hostname.lower()
    try:
        ipaddress.ip_address(host)
        is_ip = True
    except ValueError:
        is_ip = False
    if port == DEFAULT_PORTS[parts.scheme]:
        port = None
    return PublicUrl(parts.scheme, host, port, is_ip), []


def passkey_problem(public: PublicUrl | None) -> str | None:
    """Why this address cannot carry signing in with a key, or None.

    The codes are shown to people by the page (i18n `passkeyWhy_<code>`),
    so they name the cause, not the symptom.
    """
    if public is None:
        return "no_public_url"
    if public.is_ip:
        return "public_url_ip"
    if not public.secure and not public.local:
        # WebAuthn exists only in a secure context: https, or localhost
        return "public_url_http"
    return None
