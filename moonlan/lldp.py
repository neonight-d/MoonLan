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
    # How local_ifindex was arrived at: "loc_id" / "loc_desc" from the
    # local port table, "fdb" from this switch's own MAC table, "num"
    # from lldpRemLocalPortNum taken as an ifIndex. The last is a
    # convention that happens to hold on most agents, not a fact, so
    # links built on it must not override the MAC tables.
    port_matched_by: str = ""
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
    # Every management address the neighbour announced. A MikroTik with
    # a dozen VLAN interfaces sends one row per interface, each with its
    # own address; taking whichever came first can easily take the
    # wrong one, and tying a bridge to its IP is the point of reading
    # LLDP at all.
    mgmt_ips: list[str] = field(default_factory=list)
    # How many lldpRemTable rows were merged into this one entry
    rows: int = 1

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
    fdb_port: int | None = None,
) -> tuple[int | None, str]:
    """lldpRemLocalPortNum -> (ifIndex, how it was found).

    Order of preference, strongest evidence first:

    1. `lldpLocPortId` / `lldpLocPortDesc` matched against our own port
       table — the switch naming its own port;
    2. this switch's MAC table: the neighbour's chassis address is
       behind a port of ours, and a forwarding table is hard evidence;
    3. `lldpRemLocalPortNum` read as an ifIndex. On most agents the two
       coincide, but that is a convention, not a rule, which is why the
       result is labelled and treated as the weakest of the three.
    """
    for candidate, how in ((port_id, "loc_id"), (port_desc, "loc_desc")):
        key = (candidate or "").strip().lower()
        if not key:
            continue
        if key in port_names:
            return port_names[key], how
        if key.isdigit() and int(key) in if_indexes:
            return int(key), how
    if fdb_port is not None:
        return fdb_port, "fdb"
    if port_num in if_indexes:
        return port_num, "num"
    return None, ""


def build_port_names(ports, phys_addr: dict[int, str]) -> dict[str, int]:
    """Everything a switch may call its own port -> ifIndex.

    A value that leads to two different ports is thrown away rather
    than resolved to whichever came first. That single line is what
    put all seven of the HPE 1820's LLDP neighbours on port 1: the
    1820 answers `lldpLocPortId` with one system MAC on all 26 ports,
    so the first port holding that address won every lookup.
    """
    names: dict[str, int] = {}
    ambiguous: set[str] = set()
    for if_index, port in ports.items():
        if if_index < 0:
            continue  # synthetic bridge-port, not a real interface
        for key in (port.name, str(if_index), phys_addr.get(if_index, "")):
            key = (key or "").strip().lower()
            if not key:
                continue
            if names.setdefault(key, if_index) != if_index:
                ambiguous.add(key)
    for key in ambiguous:
        del names[key]
    return names


def usable_local_ids(loc_id: dict[int, str], subtype: dict[int, int]) -> bool:
    """False when lldpLocPortId carries no per-port information.

    The HPE 1820 reports subtype macAddress and the same system MAC for
    every port. Matching on a value that is identical everywhere places
    every neighbour on whichever port that value happens to resolve to.
    """
    values = {value for value in loc_id.values() if value}
    return not (len(loc_id) > 1 and len(values) <= 1)


async def collect_lldp(
    collector,
    host: str,
    port_names: dict[str, int],
    if_indexes: set[int],
    fdb: dict[str, int] | None = None,
) -> tuple[list[LldpNeighbor], dict[int, str]]:
    """Walks lldpRemTable of one switch and resolves the local ports.

    Returns (neighbours, port labels): `lldpLocPortDesc` carries the
    administrative name an operator typed into the switch ("Library",
    "403 audit" on the 1820), which is worth showing next to the port.

    `fdb` is this switch's MAC table, used to place a neighbour whose
    local port the LLDP tables cannot identify. A switch without LLDP
    simply yields nothing — the walks return no rows.
    """
    fdb = fdb or {}
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
    if not usable_local_ids(loc_id, loc_subtype):
        log.info(
            "%s: lldpLocPortId is the same on all %d local ports — it "
            "says nothing about which port is which, so neighbours are "
            "placed by port number and by the MAC table instead",
            host, len(loc_id),
        )
        loc_id = {}

    # Administrative port names an operator typed into the switch.
    # They belong to lldpLocPortNum, which is an ifIndex on every agent
    # seen so far; a number that is not one of our ifIndexes is skipped
    # rather than guessed at.
    port_labels = {
        port_num: label
        for port_num, label in loc_desc.items()
        if label and port_num in if_indexes
    }

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
            addresses = row(suffix).setdefault("mgmt_ips", [])
            if address not in addresses:
                addresses.append(address)

    neighbors: list[LldpNeighbor] = []
    unmatched = 0
    for index, data in sorted(rows.items()):
        if len(index) < 2:
            continue
        port_num = index[1]
        chassis_subtype = data.get("chassis_subtype", 0)
        port_subtype = data.get("port_subtype", 0)
        cap_raw = data.get("cap_raw")
        chassis_id = normalize_id(
            chassis_subtype, data.get("chassis_raw", b""), CHASSIS_SUBTYPE_MAC
        )
        if_index, matched_by = match_local_port(
            port_num, loc_id.get(port_num, ""), loc_desc.get(port_num, ""),
            port_names, if_indexes, fdb.get(chassis_id),
        )
        neighbor = LldpNeighbor(
            local_ifindex=if_index,
            port_matched_by=matched_by,
            local_port_num=port_num,
            chassis_id=chassis_id,
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
            mgmt_ip=(data.get("mgmt_ips") or [""])[0],
            mgmt_ips=list(data.get("mgmt_ips") or []),
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
    rows_before = len(neighbors)
    neighbors = merge_rows(neighbors)
    if len(neighbors) < rows_before:
        log.debug(
            "%s LLDP: %d table rows describe %d devices — a router with "
            "many VLAN interfaces sends one row per interface",
            host, rows_before, len(neighbors),
        )
    return neighbors, port_labels


def merge_rows(neighbors: list[LldpNeighbor]) -> list[LldpNeighbor]:
    """One entry per device per port, however many rows it sent.

    The MikroTik x86 behind mb0 Slot0/21 fills lldpRemTable with one
    row per VLAN interface — 38 of them, each announcing its own
    management address. They are one device on one cable. Merging them
    here rather than in each consumer also means a bridge node carries
    every address the device announced instead of whichever row the
    walk returned first: tying a bridge to its IP is the reason LLDP is
    read at all, and the first address is as likely to be the wrong one
    as the right one.
    """
    merged: dict[tuple, LldpNeighbor] = {}
    for neighbor in neighbors:
        key = (neighbor.local_ifindex, neighbor.local_port_num,
               neighbor.chassis_id)
        first = merged.get(key)
        if first is None:
            merged[key] = neighbor
            continue
        first.rows += 1
        for address in neighbor.mgmt_ips:
            if address not in first.mgmt_ips:
                first.mgmt_ips.append(address)
        if not first.mgmt_ip and first.mgmt_ips:
            first.mgmt_ip = first.mgmt_ips[0]
        first.sys_name = first.sys_name or neighbor.sys_name
        first.sys_desc = first.sys_desc or neighbor.sys_desc
        first.port_id = first.port_id or neighbor.port_id
        first.port_desc = first.port_desc or neighbor.port_desc
        if neighbor.cap_known and not first.cap_known:
            first.cap_known = True
            first.cap_enabled = set(neighbor.cap_enabled)
        elif neighbor.cap_known:
            first.cap_enabled |= neighbor.cap_enabled
    return list(merged.values())


def analyse_ports(
    neighbors: list[LldpNeighbor],
    aggregate,
    fdb: dict[str, int],
) -> tuple[set[int], set[int]]:
    """Local ports whose LLDP needs care: (forwarded, crowded).

    **forwarded** — the frames did not come from the cable. With
    `LLDP Forward Message` enabled a switch re-transmits foreign LLDP
    frames, and the neighbour then appears on a port it is not attached
    to. Two symptoms give it away, and both compare LOGICAL ports, so
    the two members of a LACP aggregate seeing the same neighbour are
    one port and not evidence of anything:

    1. one chassis id on more than one logical port of this switch;
    2. the neighbour's MAC is in this switch's own forwarding table on
       a DIFFERENT logical port — the frame demonstrably belongs
       elsewhere.

    A chassis missing from the forwarding table altogether is not
    evidence either way: plenty of bridges never source a frame from
    their chassis MAC. v0.6.1 reads "absent from this port's FDB" that
    way on purpose; the looser reading would flag every one of them.

    **crowded** — several neighbours on one logical port with none of
    the above. That is not forwarding, it is an unmanaged switch on the
    cable with several talkers behind it, which is the normal shape of
    an access port here. Links cannot be inferred (which of them is on
    the cable?), but the devices are real and are shown as such.

    v0.6.0 flagged both cases as forwarding, which cost the network its
    LACP uplink source (mb0—mb1 fell back to `fdb`) and suppressed the
    very ports the version was written for: mb1 27 and 28 and 2b0 20,
    where the MikroTik bridges live.
    """
    by_logical: dict[int, set[str]] = {}
    physical_of: dict[int, set[int]] = {}
    for neighbor in neighbors:
        if neighbor.local_ifindex is None:
            continue
        logical = aggregate(neighbor.local_ifindex)
        by_logical.setdefault(logical, set()).add(neighbor.chassis_id)
        physical_of.setdefault(logical, set()).add(neighbor.local_ifindex)

    ports_of_chassis: dict[str, set[int]] = {}
    for logical, chassis_ids in by_logical.items():
        for chassis_id in chassis_ids:
            ports_of_chassis.setdefault(chassis_id, set()).add(logical)
    forwarded_logical = {
        logical
        for ports in ports_of_chassis.values() if len(ports) > 1
        for logical in ports
    }
    for neighbor in neighbors:
        if neighbor.local_ifindex is None:
            continue
        logical = aggregate(neighbor.local_ifindex)
        seen_on = fdb.get(neighbor.chassis_id)
        if seen_on is not None and aggregate(seen_on) != logical:
            forwarded_logical.add(logical)

    crowded_logical = {
        logical for logical, chassis_ids in by_logical.items()
        if len(chassis_ids) > 1
    } - forwarded_logical
    return (
        {p for lg in forwarded_logical for p in physical_of.get(lg, ())},
        {p for lg in crowded_logical for p in physical_of.get(lg, ())},
    )
