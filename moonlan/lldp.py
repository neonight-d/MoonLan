"""LLDP neighbour discovery (LLDP-MIB, 1.0.8802.1.1.2).

The MAC forwarding tables say which addresses are reachable through a
port; they never say what the device on the other end IS. LLDP does:
a neighbour announces its chassis id, its port, its name, its model
and — the part this project needed — its capabilities. A neighbour
that says "bridge" behind an access port is a switch nobody polls,
and until v0.6 it could only appear on the map as an anonymous
"switch without SNMP".

Two things make the data less straightforward than the MIB suggests:

- `lldpRemLocalPortNum` is NOT an ifIndex. It indexes the LOCAL port
  table (lldpLocPortTable), whose lldpLocPortId has to be matched
  against ifName / ifDescr / the port MAC / the ifIndex itself. An
  entry that cannot be matched is kept, flagged `port_unmatched`, and
  never used to infer a link.
- Several switches (D-Link DES-1210 among them) can be configured to
  FORWARD foreign LLDP frames. The neighbour then shows up on a port
  it is not connected to, and two devices show up on one port. Such
  ports are detected and excluded from link inference — see
  `forwarded_ports`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# Local port table: what our own ports are called in LLDP terms
OID_LLDP_LOC_PORT_ID_SUBTYPE = "1.0.8802.1.1.2.1.3.7.1.2"
OID_LLDP_LOC_PORT_ID = "1.0.8802.1.1.2.1.3.7.1.3"
OID_LLDP_LOC_PORT_DESC = "1.0.8802.1.1.2.1.3.7.1.4"

# Remote table, indexed by lldpRemTimeMark.lldpRemLocalPortNum.lldpRemIndex
OID_LLDP_REM_CHASSIS_SUBTYPE = "1.0.8802.1.1.2.1.4.1.1.4"
OID_LLDP_REM_CHASSIS_ID = "1.0.8802.1.1.2.1.4.1.1.5"
OID_LLDP_REM_PORT_SUBTYPE = "1.0.8802.1.1.2.1.4.1.1.6"
OID_LLDP_REM_PORT_ID = "1.0.8802.1.1.2.1.4.1.1.7"
OID_LLDP_REM_PORT_DESC = "1.0.8802.1.1.2.1.4.1.1.8"
OID_LLDP_REM_SYS_NAME = "1.0.8802.1.1.2.1.4.1.1.9"
OID_LLDP_REM_SYS_DESC = "1.0.8802.1.1.2.1.4.1.1.10"
OID_LLDP_REM_SYS_CAP_SUPPORTED = "1.0.8802.1.1.2.1.4.1.1.11"
OID_LLDP_REM_SYS_CAP_ENABLED = "1.0.8802.1.1.2.1.4.1.1.12"
# lldpRemManAddrIfSubtype: the management address itself is in the
# INDEX, which is why this column is walked rather than read
OID_LLDP_REM_MAN_ADDR_IF_SUBTYPE = "1.0.8802.1.1.2.1.4.2.1.3"

# Chassis / port id subtypes (802.1AB)
CHASSIS_SUBTYPE_MAC = 4
CHASSIS_SUBTYPE_NET_ADDR = 5
PORT_SUBTYPE_MAC = 3
PORT_SUBTYPE_NET_ADDR = 4

# lldpSystemCapabilitiesMap, a BITS value: bit 0 is the most significant
# bit of the first octet
CAPABILITIES = (
    "other", "repeater", "bridge", "wlanAccessPoint",
    "router", "telephone", "docsisCableDevice", "stationOnly",
)

MAN_ADDR_SUBTYPE_IPV4 = 1
MAN_ADDR_SUBTYPE_IPV6 = 2


@dataclass
class LldpNeighbor:
    """One row of lldpRemTable, resolved against our own port table."""

    local_ifindex: int | None       # None — the local port was not matched
    local_port_num: int             # lldpRemLocalPortNum, as reported
    chassis_id: str = ""            # normalized MAC when subtype = macAddress
    chassis_subtype: int = 0
    port_id: str = ""
    port_subtype: int = 0
    port_desc: str = ""
    sys_name: str = ""
    sys_desc: str = ""
    cap_enabled: set[str] = field(default_factory=set)
    # True when the device sent no capabilities TLV at all: the absence
    # of "bridge" then proves nothing (D-Link DES-3526 ships with the
    # optional TLVs disabled)
    cap_known: bool = False
    mgmt_ip: str = ""

    @property
    def port_unmatched(self) -> bool:
        return self.local_ifindex is None

    @property
    def is_bridge(self) -> bool:
        return "bridge" in self.cap_enabled

    def label(self) -> str:
        return self.sys_name or self.chassis_id or f"port {self.local_port_num}"


def _fmt_mac(raw: bytes) -> str:
    return ":".join(f"{b:02x}" for b in raw)


def _printable(raw: bytes) -> str:
    """The text an OctetString carries, or '' when it is not text."""
    try:
        text = raw.decode("utf-8").strip("\x00").strip()
    except UnicodeDecodeError:
        return ""
    return text if all(c == "\t" or c >= " " for c in text) else ""


def _as_bytes(value) -> bytes:
    try:
        return bytes(value)
    except (TypeError, ValueError):
        return str(value).encode("utf-8", "replace")


def normalize_id(subtype: int, raw: bytes, mac_subtype: int) -> str:
    """Chassis / port id as a string: a MAC when the subtype says so,
    the text when it is text, hex otherwise."""
    if not raw:
        return ""
    if subtype == mac_subtype and len(raw) == 6:
        return _fmt_mac(raw)
    if subtype == CHASSIS_SUBTYPE_NET_ADDR and len(raw) == 5 and raw[0] == 1:
        return ".".join(str(octet) for octet in raw[1:])
    text = _printable(raw)
    if text:
        return text
    if len(raw) == 6:
        return _fmt_mac(raw)  # a MAC that forgot to say it is one
    return raw.hex()


def parse_capabilities(raw: bytes) -> set[str]:
    """lldpRemSysCapEnabled (BITS) -> {'bridge', 'router', …}."""
    caps: set[str] = set()
    for bit, name in enumerate(CAPABILITIES):
        octet, mask = bit // 8, 0x80 >> (bit % 8)
        if octet < len(raw) and raw[octet] & mask:
            caps.add(name)
    return caps


def parse_man_addr(suffix: tuple[int, ...]) -> str:
    """Management address out of an lldpRemManAddrTable index.

    The index is timeMark.localPortNum.remIndex.addrSubtype.addrLen.<addr>.
    """
    if len(suffix) < 6:
        return ""
    addr_subtype, addr_len = suffix[3], suffix[4]
    addr = suffix[5:5 + addr_len]
    if addr_subtype == MAN_ADDR_SUBTYPE_IPV4 and len(addr) == 4:
        return ".".join(str(octet) for octet in addr)
    if addr_subtype == MAN_ADDR_SUBTYPE_IPV6 and len(addr) == 16:
        pairs = [f"{addr[i]:02x}{addr[i + 1]:02x}" for i in range(0, 16, 2)]
        return ":".join(pairs)
    return ""


def match_local_port(
    port_num: int,
    port_id: str,
    port_desc: str,
    port_names: dict[str, int],
    if_indexes: set[int],
) -> int | None:
    """lldpRemLocalPortNum -> ifIndex, or None when nothing matches.

    port_names maps a lowercased port name (ifName, ifDescr, the port
    MAC) to its ifIndex. The local port number is tried last: on many
    agents it coincides with the ifIndex, but assuming that up front
    would silently attach neighbours to the wrong ports elsewhere.
    """
    for candidate in (port_id, port_desc):
        key = (candidate or "").strip().lower()
        if key and key in port_names:
            return port_names[key]
        if key.isdigit() and int(key) in if_indexes:
            return int(key)
    if port_num in if_indexes:
        return port_num
    return None


def build_port_names(ports, phys_addr: dict[int, str]) -> dict[str, int]:
    """Everything a switch may call its own port -> ifIndex."""
    names: dict[str, int] = {}
    for if_index, port in ports.items():
        if if_index < 0:
            continue  # synthetic bridge-port, not a real interface
        for key in (port.name, str(if_index), phys_addr.get(if_index, "")):
            key = (key or "").strip().lower()
            if key:
                names.setdefault(key, if_index)
    return names


async def collect_lldp(
    collector, host: str, port_names: dict[str, int], if_indexes: set[int]
) -> list[LldpNeighbor]:
    """Walks lldpRemTable of one switch and resolves the local ports.

    A switch without LLDP simply yields nothing — the walks return no
    rows and the caller gets an empty list.
    """
    # 1. Our own port table: lldpLocPortNum -> what this port is called
    loc_id: dict[int, str] = {}
    loc_subtype: dict[int, int] = {}
    loc_desc: dict[int, str] = {}
    async for suffix, value in collector._walk(host, OID_LLDP_LOC_PORT_ID_SUBTYPE):
        loc_subtype[suffix[0]] = int(value)
    async for suffix, value in collector._walk(host, OID_LLDP_LOC_PORT_ID):
        loc_id[suffix[0]] = normalize_id(
            loc_subtype.get(suffix[0], 0), _as_bytes(value), PORT_SUBTYPE_MAC
        )
    async for suffix, value in collector._walk(host, OID_LLDP_LOC_PORT_DESC):
        loc_desc[suffix[0]] = _printable(_as_bytes(value))

    # 2. The remote table, one entry per (timeMark, localPort, remIndex)
    rows: dict[tuple[int, ...], dict] = {}

    def row(suffix: tuple[int, ...]) -> dict:
        return rows.setdefault(suffix[:3], {})

    async for suffix, value in collector._walk(host, OID_LLDP_REM_CHASSIS_SUBTYPE):
        row(suffix)["chassis_subtype"] = int(value)
    async for suffix, value in collector._walk(host, OID_LLDP_REM_CHASSIS_ID):
        row(suffix)["chassis_raw"] = _as_bytes(value)
    async for suffix, value in collector._walk(host, OID_LLDP_REM_PORT_SUBTYPE):
        row(suffix)["port_subtype"] = int(value)
    async for suffix, value in collector._walk(host, OID_LLDP_REM_PORT_ID):
        row(suffix)["port_raw"] = _as_bytes(value)
    async for suffix, value in collector._walk(host, OID_LLDP_REM_PORT_DESC):
        row(suffix)["port_desc"] = _printable(_as_bytes(value))
    async for suffix, value in collector._walk(host, OID_LLDP_REM_SYS_NAME):
        row(suffix)["sys_name"] = _printable(_as_bytes(value))
    async for suffix, value in collector._walk(host, OID_LLDP_REM_SYS_DESC):
        row(suffix)["sys_desc"] = _printable(_as_bytes(value))
    async for suffix, value in collector._walk(host, OID_LLDP_REM_SYS_CAP_ENABLED):
        raw = _as_bytes(value)
        row(suffix)["cap_raw"] = raw
    # The management address lives in the index of its own table
    async for suffix, _value in collector._walk(
        host, OID_LLDP_REM_MAN_ADDR_IF_SUBTYPE
    ):
        address = parse_man_addr(suffix)
        if address:
            row(suffix).setdefault("mgmt_ip", address)

    neighbors: list[LldpNeighbor] = []
    unmatched = 0
    for index, data in sorted(rows.items()):
        if len(index) < 2:
            continue
        port_num = index[1]
        chassis_subtype = data.get("chassis_subtype", 0)
        port_subtype = data.get("port_subtype", 0)
        cap_raw = data.get("cap_raw")
        neighbor = LldpNeighbor(
            local_ifindex=match_local_port(
                port_num, loc_id.get(port_num, ""), loc_desc.get(port_num, ""),
                port_names, if_indexes,
            ),
            local_port_num=port_num,
            chassis_id=normalize_id(
                chassis_subtype, data.get("chassis_raw", b""),
                CHASSIS_SUBTYPE_MAC,
            ),
            chassis_subtype=chassis_subtype,
            port_id=normalize_id(
                port_subtype, data.get("port_raw", b""), PORT_SUBTYPE_MAC
            ),
            port_subtype=port_subtype,
            port_desc=data.get("port_desc", ""),
            sys_name=data.get("sys_name", ""),
            sys_desc=data.get("sys_desc", ""),
            cap_enabled=parse_capabilities(cap_raw) if cap_raw else set(),
            cap_known=cap_raw is not None and any(cap_raw),
            mgmt_ip=data.get("mgmt_ip", ""),
        )
        if not neighbor.chassis_id:
            continue
        if neighbor.port_unmatched:
            unmatched += 1
        neighbors.append(neighbor)
    if unmatched:
        log.info(
            "%s LLDP: %d of %d neighbours sit on a local port that could "
            "not be matched to an interface — not used for links",
            host, unmatched, len(neighbors),
        )
    return neighbors


def forwarded_ports(neighbors: list[LldpNeighbor]) -> set[int]:
    """Local ports whose LLDP data cannot be trusted.

    With `LLDP Forward Message` enabled a switch re-transmits foreign
    LLDP frames, so a neighbour appears on a port it is not attached
    to. Two symptoms give it away and both are treated the same way —
    the port's neighbours are kept for display and dropped from link
    inference:

    1. more than one remote entry on a single local port;
    2. one chassis id seen on more than one local port of this switch.
    """
    by_port: dict[int, set[str]] = {}
    for n in neighbors:
        if n.local_ifindex is None:
            continue
        by_port.setdefault(n.local_ifindex, set()).add(n.chassis_id)
    suspect = {port for port, chassis in by_port.items() if len(chassis) > 1}
    ports_of_chassis: dict[str, set[int]] = {}
    for port, chassis_ids in by_port.items():
        if port in suspect:
            continue
        for chassis_id in chassis_ids:
            ports_of_chassis.setdefault(chassis_id, set()).add(port)
    for ports in ports_of_chassis.values():
        if len(ports) > 1:
            suspect |= ports
    return suspect
