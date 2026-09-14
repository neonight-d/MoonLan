"""Rewriting a diagnostic report so it can be shown to strangers.

`diag` prints a map of somebody's network: every address, every MAC,
every host name, the port labels a previous administrator typed in.
All of it is exactly what a bug report needs and none of it is
anything the reporter wants indexed for ever — nine such reports went
into this repository's history before anyone noticed.

So `--anonymize` puts the output through a substitution table that is
**stable for the run**: one address always becomes the same
replacement, so a report still reads as a report — the same switch is
the same switch on every line, and a MAC seen on two ports is still
one device. Across runs nothing is promised, and nothing should be:
a table that survived would be a table worth stealing.

Replacements come from the documentation ranges, so nobody mistakes
one for a real device: addresses from RFC 5737 (198.51.100.0/24, then
203.0.113.0/24, then 192.0.2.0/24), MACs from RFC 7042
(00:00:5e:00:53:xx).

What is covered, and what is not:

- every IPv4 address and every MAC in the text, by pattern;
- every name the tool itself learned — switch sysNames, LLDP
  neighbour names, host names out of the database — by registration,
  because those are the ones that can be recognised with certainty;
- a bare word somebody typed into a port description is not
  detectable and is left alone. The output is shorter than it looks;
  skim it before attaching it.
"""

from __future__ import annotations

import re

# A dotted quad, but not four components of an OID. The lookarounds do
# the work: "1.3.6.1.2.1.1.5.0" contains no address, and every attempt
# to find one in it is rejected for having a digit on one side or the
# other.
IPV4_RE = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?!\.?\d)")
MAC_RE = re.compile(
    r"(?<![0-9a-fA-F:])([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5})"
    r"(?![0-9a-fA-F:])"
)

# RFC 5737 documentation ranges, used in that order
IP_RANGES = ("198.51.100", "203.0.113", "192.0.2")
# RFC 7042 documentation unicast MACs
MAC_PREFIX = "00:00:5e:00:53"


def _decimal_mac(mac: str) -> str:
    """"18:fd:74:fd:b5:cf" as the six decimal octets of an OID suffix.

    A MAC forwarding entry is indexed by its address, so `diag --fdb`
    prints every one of them twice: once as an address and once as the
    tail of the raw OID. Substituting only the first leaves the second
    perfectly readable.
    """
    try:
        return ".".join(str(int(part, 16)) for part in mac.split(":"))
    except ValueError:
        return ""


def _is_address(text: str) -> bool:
    parts = text.split(".")
    return len(parts) == 4 and all(
        part.isdigit() and len(part) <= 3 and int(part) <= 255
        for part in parts
    )


class Anonymizer:
    """A substitution table that stays consistent for one run."""

    def __init__(self) -> None:
        self._ips: dict[str, str] = {}
        self._macs: dict[str, str] = {}
        self._names: dict[str, str] = {}
        self._switches: dict[str, str] = {}

    # ---------- allocation ----------

    def ip(self, address: str) -> str:
        known = self._ips.get(address)
        if known is None:
            index = len(self._ips)
            block, host = divmod(index, 254)
            prefix = IP_RANGES[min(block, len(IP_RANGES) - 1)]
            known = self._ips[address] = f"{prefix}.{host + 1}"
        return known

    def mac(self, address: str) -> str:
        key = address.lower()
        known = self._macs.get(key)
        if known is None:
            known = self._macs[key] = f"{MAC_PREFIX}:{len(self._macs) % 256:02x}"
        return known

    def register_switch(self, name: str) -> str:
        """A sysName. Switches are numbered apart from hosts: a report
        in which core-sw and a workstation share a naming scheme is
        harder to read than the original."""
        name = (name or "").strip()
        if not name or _is_address(name):
            return name
        known = self._switches.get(name)
        if known is None:
            known = self._switches[name] = f"switch-{len(self._switches) + 1}"
            self._names[name] = known
        return known

    def register_name(self, name: str) -> str:
        """A host or neighbour name."""
        name = (name or "").strip()
        if not name or _is_address(name):
            return name
        known = self._names.get(name)
        if known is None:
            known = self._names[name] = f"host-{len(self._names) + 1:02d}"
        return known

    def register_names(self, names) -> None:
        for name in names:
            self.register_name(name)

    # ---------- rewriting ----------

    def text(self, text: str) -> str:
        """One chunk of output with everything known taken out of it."""
        if not text:
            return text
        # Registered names first: they can contain dots and would
        # otherwise survive as the most identifying thing in the file.
        # Longest first, so a name is not eaten by its own suffix.
        for name in sorted(self._names, key=len, reverse=True):
            if name in text:
                text = text.replace(name, self._names[name])
        text = MAC_RE.sub(lambda m: self.mac(m.group(1)), text)
        text = IPV4_RE.sub(
            lambda m: self.ip(m.group(1)) if _is_address(m.group(1))
            else m.group(1),
            text,
        )
        # An address already in the table is replaced wherever it
        # appears, the pattern's lookarounds notwithstanding: an ARP
        # row carries the address inside the OID, where it does not
        # look like one, and a MAC table row carries the MAC there as
        # six decimal octets.
        for address in sorted(self._ips, key=len, reverse=True):
            if address in text:
                text = text.replace(address, self._ips[address])
        for mac, replacement in self._macs.items():
            decimal = _decimal_mac(mac)
            if decimal and decimal in text:
                text = text.replace(decimal, _decimal_mac(replacement))
        return text


class AnonymizingWriter:
    """A stdout that rewrites what is printed through it.

    Wrapping the stream rather than every print keeps the substitution
    impossible to forget in a report added later — which is the whole
    point: the next dump has to be clean by default, not because
    someone remembered.
    """

    def __init__(self, stream, anonymizer: Anonymizer):
        self._stream = stream
        self._anon = anonymizer

    def write(self, text: str) -> int:
        return self._stream.write(self._anon.text(text))

    def flush(self) -> None:
        self._stream.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)
