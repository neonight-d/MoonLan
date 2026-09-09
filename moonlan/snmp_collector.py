"""Polling a switch over SNMP v2c.

We collect the minimum needed to build the topology:
- sysName, sysDescr                        (SNMPv2-MIB)
- interface list, speed, status, type      (IF-MIB)
- bridge base MAC                          (BRIDGE-MIB, dot1dBaseBridgeAddress)
- bridge-port -> ifIndex mapping           (BRIDGE-MIB, dot1dBasePortIfIndex)
- MAC forwarding table -> bridge-port      (BRIDGE-MIB, dot1dTpFdbPort;
                                            Q-BRIDGE-MIB, dot1qTpFdbPort)
- LACP membership                          (IEEE8023-LAG-MIB)
- port PVIDs and VLAN names                (Q-BRIDGE-MIB)
- LLDP neighbours                          (LLDP-MIB, see lldp.py)
- spanning tree state                      (BRIDGE-MIB, see stp.py)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from . import lldp as lldp_mod
from . import stp as stp_mod

from pysnmp.hlapi.v3arch.asyncio import (
    CommunityData,
    ContextData,
    ObjectIdentity,
    ObjectType,
    SnmpEngine,
    UdpTransportTarget,
    get_cmd,
    walk_cmd,
)

log = logging.getLogger(__name__)

# Numeric OIDs so we do not depend on MIB file loading
OID_SYS_NAME = "1.3.6.1.2.1.1.5.0"
OID_SYS_DESCR = "1.3.6.1.2.1.1.1.0"
OID_IF_DESCR = "1.3.6.1.2.1.2.2.1.2"          # ifDescr.<ifIndex>
OID_IF_TYPE = "1.3.6.1.2.1.2.2.1.3"           # ifType.<ifIndex>
OID_IF_PHYS_ADDRESS = "1.3.6.1.2.1.2.2.1.6"   # ifPhysAddress.<ifIndex>
OID_IF_NAME = "1.3.6.1.2.1.31.1.1.1.1"        # ifName.<ifIndex>
OID_IF_OPER_STATUS = "1.3.6.1.2.1.2.2.1.8"    # 1=up, 2=down
OID_IF_LAST_CHANGE = "1.3.6.1.2.1.2.2.1.9"    # ifLastChange, TimeTicks
OID_IF_HIGH_SPEED = "1.3.6.1.2.1.31.1.1.1.15" # Mbit/s
OID_BRIDGE_ADDRESS = "1.3.6.1.2.1.17.1.1.0"   # dot1dBaseBridgeAddress
OID_PORT_IFINDEX = "1.3.6.1.2.1.17.1.4.1.2"   # dot1dBasePortIfIndex.<port>
OID_FDB_PORT = "1.3.6.1.2.1.17.4.3.1.2"       # dot1dTpFdbPort.<6-byte MAC>
OID_Q_FDB_PORT = "1.3.6.1.2.1.17.7.1.2.2.1.2" # dot1qTpFdbPort.<fdbId>.<6-byte MAC>
OID_ARP_PHYS = "1.3.6.1.2.1.4.22.1.2"         # ipNetToMediaPhysAddress.<ifIndex>.<IP>
# dot3adAggPortAttachedAggID.<member ifIndex> -> aggregate ifIndex (IEEE8023-LAG-MIB)
OID_LAG_ATTACHED_ID = "1.2.840.10006.300.43.1.2.1.1.13"
OID_PVID = "1.3.6.1.2.1.17.7.1.4.5.1.1"       # dot1qPvid.<bridge-port>
OID_VLAN_NAME = "1.3.6.1.2.1.17.7.1.4.3.1.1"  # dot1qVlanStaticName.<VLAN ID>

IF_TYPE_ETHERNET = 6  # ethernetCsmacd
# ifType values that mean a physical Ethernet port. Some switches (e.g.
# D-Link DES-3526 combo gigabit ports) report types other than 6:
# 62 = fastEther, 69 = fastEtherFX, 117 = gigabitEthernet.
PHYSICAL_IF_TYPES = {IF_TYPE_ETHERNET, 62, 69, 117}


@dataclass
class PortInfo:
    if_index: int
    name: str = ""
    oper_up: bool = False
    speed_mbps: int = 0
    is_physical: bool = True  # ifType 6; aggregates/CPU/VLAN interfaces — False
    mac: str = ""             # ifPhysAddress: some agents use it as an LLDP port id
    last_change: int = 0      # ifLastChange, TimeTicks since sysUpTime epoch


@dataclass
class SwitchData:
    """Everything collected from a single switch."""

    ip: str
    reachable: bool = False
    sys_name: str = ""
    sys_descr: str = ""
    bridge_mac: str = ""
    # All MACs the switch may be seen under in neighbors' FDB tables:
    # bridge base MAC, interface MACs (ifPhysAddress), management-IP MAC
    # from ARP (added in server.py). bridge_mac is kept for display.
    own_macs: set[str] = field(default_factory=set)
    ports: dict[int, PortInfo] = field(default_factory=dict)   # ifIndex -> port
    fdb: dict[str, int] = field(default_factory=dict)          # MAC -> ifIndex
    lag_members: dict[int, int] = field(default_factory=dict)  # member ifIndex -> aggregate ifIndex
    # LAG trunks inferred from bridge-ports missing from
    # dot1dBasePortIfIndex: synthetic bridge-port -> member ifIndexes
    lag_groups: dict[int, list[int]] = field(default_factory=dict)
    port_pvid: dict[int, int] = field(default_factory=dict)    # ifIndex -> PVID (untagged VLAN)
    vlan_names: dict[int, str] = field(default_factory=dict)   # VLAN ID -> name
    # LLDP neighbours, the local ports whose data is unusable because
    # foreign frames are forwarded onto them, and the ports that simply
    # have several devices behind an unmanaged switch
    lldp_neighbors: list = field(default_factory=list)
    lldp_forwarded: set[int] = field(default_factory=set)
    lldp_crowded: set[int] = field(default_factory=set)
    # lldpLocPortDesc: the administrative port name an operator typed
    # into the switch ("Library", "403 audit")
    port_labels: dict[int, str] = field(default_factory=dict)
    # Spanning tree as the switch reports it, verdict included
    stp: object | None = None
    sys_uptime: int = 0  # sysUpTime in TimeTicks, for the STP verdict


def _fmt_mac(raw: bytes) -> str:
    return ":".join(f"{b:02x}" for b in raw)


def aggregate_port(sw: SwitchData, if_index: int | None) -> int | None:
    """The logical port a physical one belongs to, if any.

    Two kinds of aggregate exist here: an IEEE8023-LAG-MIB one (a
    positive aggregate ifIndex) and a D-Link trunk inferred from the
    dot1dBasePortIfIndex gaps, whose FDB lives on a synthetic negative
    ifIndex. LLDP always reports the physical member, so both have to
    be resolved.
    """
    if if_index is None:
        return None
    aggregate = sw.lag_members.get(if_index)
    if aggregate is not None:
        return aggregate
    for bridge_port, members in sw.lag_groups.items():
        if if_index in members:
            return -bridge_port
    return if_index


def port_display(data: "SwitchData", if_index: int) -> str:
    """Port name as the operator knows it, ifIndex as the fallback."""
    port = data.ports.get(if_index)
    return port.name if port and port.name else str(if_index)


# A MAC forwarding entry is indexed by the 6 address bytes (BRIDGE-MIB)
# or by an fdbId plus those 6 bytes (Q-BRIDGE-MIB). Anything else is a
# row of another table the walk ran into, or an agent numbering its
# entries its own way — taking the last six components of such a
# suffix invents devices that do not exist.
FDB_SUFFIX_LEN = {6: "dot1dTpFdbPort", 7: "dot1qTpFdbPort"}
BROADCAST_MAC = "ff:ff:ff:ff:ff:ff"
ZERO_MAC = "00:00:00:00:00:00"


def is_random_mac(mac: str) -> bool:
    """True for a locally administered address — phones and laptops
    randomize those per network, so they pile up as one-off devices."""
    try:
        return bool(int(mac.split(":")[0], 16) & 0x02)
    except (ValueError, IndexError):
        return False


def is_valid_mac(mac: str) -> bool:
    """A unicast, non-reserved address of the right shape — the same
    test the FDB parser applies, for addresses already in the DB."""
    parts = mac.split(":")
    if len(parts) != 6:
        return False
    try:
        octets = [int(part, 16) for part in parts]
    except ValueError:
        return False
    if any(not 0 <= octet <= 255 for octet in octets):
        return False
    if octets[0] & 0x01:  # multicast, broadcast included
        return False
    return mac != ZERO_MAC


def parse_fdb_entry(
    suffix: tuple[int, ...], value, expected_len: int
) -> tuple[str, int, str]:
    """Turns one FDB row into (mac, bridge_port, reason).

    A non-empty reason means the row is rejected and says why.
    """
    if len(suffix) != expected_len:
        return "", 0, f"suffix has {len(suffix)} components, expected {expected_len}"
    octets = suffix[-6:]
    if any(not 0 <= octet <= 255 for octet in octets):
        return "", 0, "suffix component outside 0..255"
    mac = ":".join(f"{octet:02x}" for octet in octets)
    if mac in (ZERO_MAC, BROADCAST_MAC):
        return mac, 0, "reserved MAC"
    if octets[0] & 0x01:
        return mac, 0, "multicast MAC"
    try:
        bridge_port = int(value)
    except (TypeError, ValueError):
        return mac, 0, f"bridge-port is not a number: {value!r}"
    return mac, bridge_port, ""


def infer_lag_groups(
    physical: set[int], mapped: set[int], synthetic: set[int]
) -> dict[int, list[int]]:
    """LAG members inferred from bridge-ports missing from dot1dBasePortIfIndex.

    On D-Link models the bridge-port number matches the physical port
    ifIndex; ports joined into a LACP group vanish from the mapping,
    while the trunk's FDB lives on a synthetic bridge-port equal to one
    of the members (the master). So: members = physical ports whose
    bridge-ports are missing from the mapping, grouped around the
    synthetic FDB ports (each member goes to the nearest one).
    """
    if not mapped:
        return {}  # the whole mapping is absent — nothing can be inferred
    missing = sorted(p for p in physical if p not in mapped)
    trunks = sorted(s for s in synthetic if s in missing)
    if not trunks:
        return {}
    groups: dict[int, list[int]] = {}
    for member in missing:
        nearest = min(trunks, key=lambda s: (abs(member - s), s))
        groups.setdefault(nearest, []).append(member)
    return groups


class SnmpCollector:
    """SNMP client for all polling loops.

    Create ONE instance per process and reuse it: every SnmpEngine
    owns a transport dispatcher (sockets) and loaded MIB state, so a
    new engine per polling cycle leaks file descriptors and memory
    (OSError 24 in the pinger, MibNotFoundError from pysnmp, growing
    RSS). Transport targets are cached per host for the same reason.
    """

    def __init__(self, community: str, timeout: int = 2, retries: int = 1):
        self._community = CommunityData(community, mpModel=1)  # v2c
        self._timeout = timeout
        self._retries = retries
        self._engine = SnmpEngine()
        self._targets: dict[str, UdpTransportTarget] = {}

    async def _target(self, host: str) -> UdpTransportTarget:
        target = self._targets.get(host)
        if target is None:
            target = await UdpTransportTarget.create(
                (host, 161), timeout=self._timeout, retries=self._retries
            )
            self._targets[host] = target
        return target

    async def _get(self, host: str, oid: str):
        """GET of a single value; None on error."""
        error_ind, error_status, _, var_binds = await get_cmd(
            self._engine,
            self._community,
            await self._target(host),
            ContextData(),
            ObjectType(ObjectIdentity(oid)),
        )
        if error_ind or error_status:
            log.debug("%s GET %s: %s", host, oid, error_ind or error_status)
            return None
        return var_binds[0][1]

    async def _walk(self, host: str, oid: str):
        """WALK of a subtree; yields (OID suffix, value) pairs."""
        base = tuple(int(x) for x in oid.split("."))
        objects = walk_cmd(
            self._engine,
            self._community,
            await self._target(host),
            ContextData(),
            ObjectType(ObjectIdentity(oid)),
            lexicographicMode=False,
        )
        async for error_ind, error_status, _, var_binds in objects:
            if error_ind or error_status:
                log.debug("%s WALK %s: %s", host, oid, error_ind or error_status)
                return
            for name, value in var_binds:
                suffix = tuple(name)[len(base):]
                yield suffix, value

    async def collect(self, host: str) -> SwitchData:
        """Full poll of a single switch."""
        data = SwitchData(ip=host)

        sys_name = await self._get(host, OID_SYS_NAME)
        if sys_name is None:
            log.warning("Switch %s does not respond to SNMP", host)
            return data

        data.reachable = True
        data.sys_name = str(sys_name)
        sys_descr = await self._get(host, OID_SYS_DESCR)
        data.sys_descr = str(sys_descr) if sys_descr is not None else ""

        bridge_mac = await self._get(host, OID_BRIDGE_ADDRESS)
        if bridge_mac is not None:
            data.bridge_mac = _fmt_mac(bytes(bridge_mac))
            data.own_macs.add(data.bridge_mac)

        # Interface MACs: real frames leave the switch with these source
        # addresses, not with the bridge base MAC. They are also what
        # some agents put in lldpLocPortId, so they are kept per port.
        phys_addr: dict[int, str] = {}
        async for suffix, value in self._walk(host, OID_IF_PHYS_ADDRESS):
            raw = bytes(value)
            if len(raw) == 6 and any(raw):
                mac = _fmt_mac(raw)
                data.own_macs.add(mac)
                phys_addr[suffix[0]] = mac

        # Interfaces. Port name comes from ifName; ifDescr is only a
        # fallback: D-Link puts the whole model and firmware into ifDescr.
        async for suffix, value in self._walk(host, OID_IF_DESCR):
            if_index = suffix[0]
            data.ports[if_index] = PortInfo(if_index=if_index, name=str(value))
        async for suffix, value in self._walk(host, OID_IF_NAME):
            port = data.ports.get(suffix[0])
            name = str(value).strip()
            if port and name:
                port.name = name
        async for suffix, value in self._walk(host, OID_IF_TYPE):
            port = data.ports.get(suffix[0])
            if port:
                port.is_physical = int(value) in PHYSICAL_IF_TYPES
        async for suffix, value in self._walk(host, OID_IF_OPER_STATUS):
            port = data.ports.get(suffix[0])
            if port:
                port.oper_up = int(value) == 1
        async for suffix, value in self._walk(host, OID_IF_HIGH_SPEED):
            port = data.ports.get(suffix[0])
            if port:
                port.speed_mbps = int(value)
        async for suffix, value in self._walk(host, OID_IF_LAST_CHANGE):
            port = data.ports.get(suffix[0])
            if port:
                port.last_change = int(value)
        for if_index, mac in phys_addr.items():
            port = data.ports.get(if_index)
            if port:
                port.mac = mac

        # LACP: membership of physical ports in aggregates. If the switch
        # does not support IEEE8023-LAG-MIB, the walk simply yields nothing.
        async for suffix, value in self._walk(host, OID_LAG_ATTACHED_ID):
            member, aggregate = suffix[0], int(value)
            if aggregate and aggregate != member:
                data.lag_members[member] = aggregate

        # bridge-port -> ifIndex
        port_to_ifindex: dict[int, int] = {}
        async for suffix, value in self._walk(host, OID_PORT_IFINDEX):
            port_to_ifindex[suffix[0]] = int(value)

        # Spanning tree, read with the "disabled STP still answers"
        # trap in mind (see stp.py)
        data.stp = await stp_mod.collect_stp(self, host, port_to_ifindex)
        for entry in data.stp.ports.values():
            if entry.if_index is not None:
                entry.name = port_display(data, entry.if_index)
        data.sys_uptime = data.stp.sys_uptime

        # VLANs: port PVIDs (Q-BRIDGE-MIB, indexed by bridge-port) and names
        async for suffix, value in self._walk(host, OID_PVID):
            if_index = port_to_ifindex.get(suffix[0])
            if if_index is not None:
                data.port_pvid[if_index] = int(value)
        async for suffix, value in self._walk(host, OID_VLAN_NAME):
            data.vlan_names[suffix[-1]] = str(value).strip()

        # MAC table: BRIDGE-MIB and Q-BRIDGE-MIB (Q-BRIDGE has an fdbId in
        # the suffix before the MAC, so we take the last 6 bytes).
        # Entries on bridge-ports missing from dot1dBasePortIfIndex (this is
        # how LACP trunks look on some D-Link models) are not dropped:
        # they get a synthetic port with ifIndex = -bridge_port.
        unmapped: dict[int, int] = {}
        bad_suffix = bad_mac = 0
        for fdb_oid, expected_len in (
            (OID_FDB_PORT, 6), (OID_Q_FDB_PORT, 7)
        ):
            async for suffix, value in self._walk(host, fdb_oid):
                mac, bridge_port, reason = parse_fdb_entry(
                    suffix, value, expected_len
                )
                if reason:
                    if "suffix" in reason:
                        bad_suffix += 1
                    else:
                        bad_mac += 1
                    log.debug("%s: FDB entry rejected (%s): %s", host, reason, mac)
                    continue
                if bridge_port == 0:  # 0 — the switch's own MAC / CPU
                    continue
                if mac in data.fdb:
                    continue
                if_index = port_to_ifindex.get(bridge_port)
                if if_index is None:
                    if_index = -bridge_port
                    unmapped[bridge_port] = unmapped.get(bridge_port, 0) + 1
                    if if_index not in data.ports:
                        data.ports[if_index] = PortInfo(
                            if_index=if_index,
                            name=f"bridge-port {bridge_port}",
                            is_physical=False,
                        )
                data.fdb[mac] = if_index
        rejected = bad_suffix + bad_mac
        log.log(
            logging.INFO if rejected else logging.DEBUG,
            "%s FDB: %d entries rejected (bad suffix: %d, bad MAC: %d)",
            host, rejected, bad_suffix, bad_mac,
        )
        if unmapped:
            log.debug(
                "%s: FDB entries on unmapped bridge-ports: %s",
                host,
                "; ".join(
                    f"port {p}: {n} MACs" for p, n in sorted(unmapped.items())
                ),
            )

        # LAG composition: physical ports whose bridge-ports vanished
        # from dot1dBasePortIfIndex, grouped around the synthetic ports
        data.lag_groups = infer_lag_groups(
            {p.if_index for p in data.ports.values()
             if p.is_physical and p.if_index > 0},
            set(port_to_ifindex),
            set(unmapped),
        )
        if data.lag_groups:
            log.debug(
                "%s: inferred LAG groups: %s",
                host,
                "; ".join(
                    f"bridge-port {s}: members {', '.join(map(str, members))}"
                    for s, members in sorted(data.lag_groups.items())
                ),
            )

        # LLDP comes last on purpose: both halves of it need this
        # switch's own MAC table. Placing a neighbour whose local port
        # the LLDP tables cannot identify uses the table, and so does
        # deciding whether a frame arrived on the cable or was
        # forwarded onto it from somewhere else.
        data.lldp_neighbors, data.port_labels = await lldp_mod.collect_lldp(
            self, host,
            lldp_mod.build_port_names(data.ports, phys_addr),
            set(data.ports),
            data.fdb,
        )
        data.lldp_forwarded, data.lldp_crowded = lldp_mod.analyse_ports(
            data.lldp_neighbors,
            lambda if_index: aggregate_port(data, if_index),
            data.fdb,
        )
        if data.lldp_forwarded:
            log.warning(
                "%s: LLDP on port(s) %s did not come from the cable — a "
                "neighbour is on several ports, or its MAC is in the "
                "forwarding table behind another one. `LLDP Forward "
                "Message` is most likely enabled; these ports are "
                "excluded from link inference.",
                host,
                ", ".join(
                    port_display(data, i) for i in sorted(data.lldp_forwarded)
                ),
            )
        if data.lldp_crowded:
            log.debug(
                "%s: several LLDP devices behind port(s) %s — an "
                "unmanaged switch, most likely; no links are inferred "
                "from them",
                host,
                ", ".join(
                    port_display(data, i) for i in sorted(data.lldp_crowded)
                ),
            )

        log.info(
            "%s (%s): %d ports, %d MAC addresses, %d LLDP neighbour(s), "
            "STP %s",
            data.sys_name, host, len(data.ports), len(data.fdb),
            len(data.lldp_neighbors),
            "operating" if data.stp and data.stp.operating else "not operating",
        )
        return data

    async def collect_arp(self, host: str) -> dict[str, str]:
        """The device's (router's) ARP table: MAC -> IP.

        The ipNetToMediaPhysAddress index is <ifIndex>.<4 IP octets>,
        the value is a 6-byte MAC.
        """
        arp: dict[str, str] = {}
        async for suffix, value in self._walk(host, OID_ARP_PHYS):
            raw = bytes(value)
            if len(raw) != 6:
                continue
            ip = ".".join(str(octet) for octet in suffix[-4:])
            arp[_fmt_mac(raw)] = ip
        log.info("ARP from %s: %d entries", host, len(arp))
        return arp
