"""What the node menu can link to, and how a link is filled in.

There is no sign-in yet (it comes in v0.7.3), so anything the menu
could make the server do, anybody who opens the page could make it do.
That draws the line for everything here:

- the server runs only its own built-in actions (ping, traceroute —
  see probes.py), against addresses it found itself;
- what config.yaml adds to the menu is links, opened on the machine of
  whoever is looking at the map. A command run on the server from a
  template in the config would be remote execution for the whole LAN
  without sign-in — even with a harmless template today, since
  tomorrow somebody writes another one.

So this module decides which links may exist at all (a whitelist of
schemes, `javascript:` and `data:` never) and fills them in, with every
value URL-encoded on the way in.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote

# What a link can say it applies to. A bridge LLDP found and a switch
# MoonLan polls are both "switch" here: both are network boxes with an
# interface of their own, which is what a menu link is usually for.
KINDS = ("switch", "host", "group")

# What a link can put into its address
FIELDS = ("ip", "mac", "name", "switch", "port")

# Schemes a link may use without anybody saying so
BASE_SCHEMES = ("http", "https", "ssh", "telnet")

# …and those it may never use, whatever allowed_schemes says. Each of
# them runs code in the browser of everybody looking at the map, and
# config.yaml would become the way to do it.
FORBIDDEN_SCHEMES = ("javascript", "data", "vbscript")

# The scheme has to be written out at the very start of the address.
# One filled in from a value ("{name}://…") could be anything.
_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*):")
_FIELD = re.compile(r"\{([^{}]*)\}")
# Browsers drop tabs and newlines inside a URL before reading its
# scheme — "java\tscript:" is javascript: to them — so an address with
# any control character or blank in it is not a link we write out
_UNSAFE = re.compile(r"[\x00-\x20\x7f]")


@dataclass(frozen=True)
class MenuLink:
    label: str
    url: str
    # empty: every kind of node
    applies_to: tuple[str, ...] = ()

    def applies(self, kind: str) -> bool:
        return not self.applies_to or kind in self.applies_to


def allowed_schemes(extra) -> tuple[set[str], list[str]]:
    """BASE_SCHEMES plus what the operator added, minus the forbidden."""
    allowed = set(BASE_SCHEMES)
    problems: list[str] = []
    if extra is None:
        extra = []
    if not isinstance(extra, (list, tuple)):
        extra = [extra]
    for raw in extra:
        scheme = str(raw).strip().lower().rstrip(":")
        if scheme in FORBIDDEN_SCHEMES:
            problems.append(
                f"allowed_schemes: {scheme!r} is never allowed — a link "
                f"with it would run code in the browser of everybody "
                f"looking at the map"
            )
        elif not re.fullmatch(r"[a-z][a-z0-9+.-]*", scheme):
            problems.append(
                f"allowed_schemes: {raw!r} is not a scheme — ignored"
            )
        else:
            allowed.add(scheme)
    return allowed, problems


def check_url(url: str, allowed: set[str]) -> str | None:
    """Why this address template may not be a menu link; None if it may."""
    if _UNSAFE.search(url):
        return "the address contains a blank or a control character"
    match = _SCHEME.match(url)
    if not match:
        return (
            "the address does not start with a scheme (http:, ssh:, …) "
            "written out in full"
        )
    scheme = match.group(1).lower()
    if scheme in FORBIDDEN_SCHEMES:
        return (
            f"the scheme {scheme}: is never allowed — it would run code "
            f"in the browser of everybody looking at the map"
        )
    if scheme not in allowed:
        return (
            f"the scheme {scheme}: is not allowed; add it to "
            f"context_menu.allowed_schemes if it is meant"
        )
    unknown = [f for f in _FIELD.findall(url) if f not in FIELDS]
    if unknown:
        return (
            "unknown placeholder "
            + ", ".join("{" + f + "}" for f in unknown)
            + " — known: " + ", ".join("{" + f + "}" for f in FIELDS)
        )
    return None


def parse_links(raw, allowed: set[str]) -> tuple[list[MenuLink], list[str]]:
    """`context_menu.links` -> (usable links, why the rest are not).

    A broken entry is dropped with a reason, never fatal: the service
    starts, the item is simply not in the menu, and the log and
    `diag --config` say why.
    """
    links: list[MenuLink] = []
    problems: list[str] = []
    if raw is None:
        return links, problems
    if not isinstance(raw, (list, tuple)):
        return links, [f"links: expected a list of items, got {raw!r}"]
    for number, entry in enumerate(raw, start=1):
        where = f"links item {number}"
        if not isinstance(entry, dict):
            problems.append(f"{where}: not a mapping with label: and url: — {entry!r}")
            continue
        label = str(entry.get("label") or "").strip()
        url = entry.get("url")
        if label:
            where += f" ({label!r})"
        if not label:
            problems.append(f"{where}: no label — not shown")
            continue
        if not isinstance(url, str) or not url:
            problems.append(f"{where}: no url — not shown")
            continue
        reason = check_url(url, allowed)
        if reason:
            problems.append(f"{where}: {reason} — not shown")
            continue
        applies = entry.get("applies_to")
        if applies is None:
            kinds: tuple[str, ...] = ()
        else:
            if not isinstance(applies, (list, tuple)):
                applies = [applies]
            kinds = tuple(str(k).strip().lower() for k in applies)
            wrong = [k for k in kinds if k not in KINDS]
            if wrong:
                problems.append(
                    f"{where}: applies_to {', '.join(wrong)} is not one of "
                    f"{', '.join(KINDS)} — not shown"
                )
                continue
        unknown = set(entry) - {"label", "url", "applies_to"}
        if unknown:
            problems.append(
                f"{where}: unknown key(s) {', '.join(sorted(unknown))} — ignored"
            )
        links.append(MenuLink(label=label, url=url, applies_to=kinds))
    return links, problems


def expand(url: str, facts: dict) -> tuple[str | None, str | None]:
    """The address with values filled in, or (None, the missing field).

    Every value is URL-encoded — a colon in a MAC included — so what
    comes from the network (a DNS name, an LLDP system name) cannot
    change the shape of the address it is put into.
    """
    for field in _FIELD.findall(url):
        if not facts.get(field):
            return None, field
    filled = _FIELD.sub(
        lambda m: quote(str(facts[m.group(1)]), safe=""), url
    )
    return filled, None
