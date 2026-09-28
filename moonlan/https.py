"""HTTPS: the address people open the map at, the certificate MoonLan
serves itself, and the proxy that may stand in front of it (v0.7.6).

The service used to learn its own address from each request's Host
header. For signing in with a key that is not enough: the browser
signs the page's origin, and the server has to compare it with the
address it itself holds to be right, not with whatever the client sent.
That address is `listen.public_url`.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import ssl
import stat
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


# ---------- the certificate MoonLan serves itself ----------

class TlsProblem(Exception):
    """A certificate or key MoonLan cannot start with; the message is
    the one line the operator gets instead of uvicorn's traceback."""


@dataclass(frozen=True)
class CertInfo:
    subject: str
    names: tuple[str, ...]    # subjectAltName: DNS names and addresses
    not_after: float          # unix time


def check_tls_files(cert: str, key: str) -> None:
    """Before uvicorn starts: both files there, readable, PEM, and the
    key the one that belongs to the certificate. Raises TlsProblem."""
    if bool(cert) != bool(key):
        raise TlsProblem(
            "listen.tls_cert and listen.tls_key go together: set both, "
            "or neither"
        )
    for name, path in (("tls_cert", cert), ("tls_key", key)):
        try:
            with open(path, "rb"):
                pass
        except FileNotFoundError:
            raise TlsProblem(f"listen.{name}: {path} does not exist") from None
        except OSError as exc:
            raise TlsProblem(
                f"listen.{name}: {path} cannot be read ({exc.strerror}) — "
                f"is it readable by the user MoonLan runs as?"
            ) from None
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        context.load_cert_chain(cert, key)
    except ssl.SSLError as exc:
        raise TlsProblem(
            f"listen.tls_cert {cert} and listen.tls_key {key}: the key does "
            f"not belong to the certificate, or one of them is not PEM "
            f"({exc.reason or exc})"
        ) from None


def read_certificate(path: str) -> CertInfo:
    """Who the certificate is for and until when. With `cryptography`
    (installed with fido2) through its public API; without it, through
    the decoder the standard library ships for its own tests — the
    service has to run without fido2, and so without cryptography."""
    try:
        from cryptography import x509
    except ImportError:
        return _read_certificate_stdlib(path)
    with open(path, "rb") as handle:
        cert = x509.load_pem_x509_certificate(handle.read())
    try:
        san = cert.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value
        names = tuple(san.get_values_for_type(x509.DNSName)) + tuple(
            str(address) for address in san.get_values_for_type(x509.IPAddress)
        )
    except x509.ExtensionNotFound:
        names = ()
    return CertInfo(
        subject=cert.subject.rfc4514_string(), names=names,
        not_after=cert.not_valid_after_utc.timestamp(),
    )


def _read_certificate_stdlib(path: str) -> CertInfo:
    decoded = ssl._ssl._test_decode_cert(path)  # noqa: SLF001
    subject = ",".join(
        f"{key}={value}" for rdn in decoded.get("subject", ())
        for key, value in rdn
    )
    names = tuple(
        value for kind, value in decoded.get("subjectAltName", ())
        if kind in ("DNS", "IP Address")
    )
    return CertInfo(
        subject=subject, names=names,
        not_after=ssl.cert_time_to_seconds(decoded["notAfter"]),
    )


def covers(names: tuple[str, ...], host: str) -> bool:
    """Whether a browser would accept a certificate with these names for
    this host: an exact name, or a wildcard for one label."""
    host = host.lower()
    for name in names:
        name = name.lower()
        if name == host:
            return True
        if name.startswith("*.") and "." in host and \
                host.split(".", 1)[1] == name[2:]:
            return True
    return False


def key_mode_too_open(path: str) -> int | None:
    """The file's mode if anybody but its owner can read it, else None."""
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        return None
    return mode if mode & 0o077 else None


# ---------- the port that only sends people to HTTPS ----------

async def serve_redirect(host: str, port: int, target: str):
    """listen.http_redirect_port: a listener that answers every request
    with a redirect to the same path at the public address. Somebody who
    types http:// out of habit lands on the page where signing in with a
    key works, not on one where it cannot.

    307, not 301 or 308: a permanent redirect is cached by the browser,
    and if HTTPS is ever switched off again the cached one keeps sending
    people to an address that no longer answers — the same trap as HSTS.
    """

    async def answer(reader, writer):
        try:
            line = await asyncio.wait_for(reader.readline(), 10)
            while True:  # the headers: read and forget
                header = await asyncio.wait_for(reader.readline(), 10)
                if header in (b"\r\n", b"\n", b""):
                    break
            parts = line.decode("latin-1").split()
            path = parts[1] if len(parts) >= 2 else "/"
            # only a plain absolute path goes into Location: nothing that
            # could end the header or point somewhere else
            if not path.startswith("/") or path.startswith("//") or not all(
                33 <= ord(ch) < 127 for ch in path
            ):
                path = "/"
            writer.write(
                f"HTTP/1.1 307 Temporary Redirect\r\n"
                f"Location: {target}{path}\r\n"
                f"Content-Length: 0\r\nConnection: close\r\n\r\n"
                .encode("ascii")
            )
            await writer.drain()
        except (asyncio.TimeoutError, OSError, UnicodeError):
            pass
        finally:
            writer.close()

    return await asyncio.start_server(answer, host, port)


# ---------- the proxy in front ----------

def parse_trusted_proxies(values) -> tuple[list[str], list[str]]:
    """listen.trusted_proxies: (the addresses and networks to trust,
    what was refused). X-Forwarded-For and X-Forwarded-Proto are taken
    from these alone; from anybody else they are ignored, or any client
    could claim another address and walk past the delay and the lock
    that are kept per address. "*" is refused for exactly that reason."""
    trusted, problems = [], []
    for value in values or []:
        text = str(value).strip()
        if text == "*":
            problems.append(
                "listen.trusted_proxies: \"*\" would let any client claim any "
                "address — list the proxy's own address"
            )
            continue
        try:
            ipaddress.ip_network(text, strict=False)
        except ValueError:
            problems.append(
                f"listen.trusted_proxies: {text!r} is not an address or a "
                f"network"
            )
            continue
        trusted.append(text)
    return trusted, problems


def uvicorn_proxy_options(trusted: list[str]) -> dict:
    """What run.py gives uvicorn. With nobody trusted, proxy headers are
    switched off altogether: uvicorn otherwise trusts 127.0.0.1 by
    default, and any process on the machine could claim to be anybody."""
    if not trusted:
        return {"proxy_headers": False}
    return {"proxy_headers": True, "forwarded_allow_ips": list(trusted)}


# ---------- how the map reaches people ----------

def transport_mode(tls_cert: str, trusted: list[str],
                   public: PublicUrl | None) -> str:
    """"tls" — MoonLan serves HTTPS itself; "proxy" — a trusted proxy in
    front holds it (and, if it is set, the public address is https);
    "http" — the password and the session cross the network in the
    clear."""
    if tls_cert:
        return "tls"
    if trusted and (public is None or public.secure):
        return "proxy"
    return "http"


class Hsts:
    """listen.hsts_max_age: Strict-Transport-Security on every answer
    that went over HTTPS. Off unless asked for: once a browser has seen
    it, it refuses plain http to this host name for max-age seconds —
    and if the certificate is ever let lapse, or HTTPS switched off, that
    browser cannot open the map at all until the time runs out."""

    def __init__(self, app, max_age: int):
        self.app = app
        self.value = f"max-age={int(max_age)}".encode("ascii")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("scheme") != "https":
            await self.app(scope, receive, send)
            return

        async def with_header(message):
            if message["type"] == "http.response.start":
                headers = [
                    (k, v) for k, v in message.get("headers", [])
                    if k.lower() != b"strict-transport-security"
                ]
                headers.append((b"strict-transport-security", self.value))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, with_header)
