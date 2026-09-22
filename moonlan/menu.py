"""How a link in the node menu is filled in.

The menu's links are opened on the machine of whoever is looking at the
map, never run on the server. Their addresses are templates filled with
what MoonLan knows about the node — and every value is URL-encoded on
the way in, so what comes from the network (a DNS name, an LLDP system
name) cannot change the shape of the address it is put into.
"""

from __future__ import annotations

import re
from urllib.parse import quote

# What a link can put into its address
FIELDS = ("ip", "mac", "name", "switch", "port")

_FIELD = re.compile(r"\{([^{}]*)\}")


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
