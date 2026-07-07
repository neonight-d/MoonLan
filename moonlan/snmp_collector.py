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
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

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


def _fmt_mac(raw: bytes) -> str:
    return ":".join(f"{b:02x}" for b in raw)


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
    def __init__(self, community: str, timeout: int = 2, retries: int = 1):
        self._community = CommunityData(community, mpModel=1)  # v2c
        self._timeout = timeout
        self._retries = retries
        self._engine = SnmpEngine()

    async def _target(self, host: str) -> UdpTransportTarget:
        return await UdpTransportTarget.create(
            (host, 161), timeout=self._timeout, retries=self._retries
        )

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
        # addresses, not with the bridge base MAC
        async for _suffix, value in self._walk(host, OID_IF_PHYS_ADDRESS):
            raw = bytes(value)
            if len(raw) == 6 and any(raw):
                data.own_macs.add(_fmt_mac(raw))

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
        for fdb_oid in (OID_FDB_PORT, OID_Q_FDB_PORT):
            async for suffix, value in self._walk(host, fdb_oid):
                bridge_port = int(value)
                if bridge_port == 0:  # 0 — the switch's own MAC / CPU
                    continue
                mac = ":".join(f"{octet:02x}" for octet in suffix[-6:])
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

        log.info(
            "%s (%s): %d ports, %d MAC addresses",
            data.sys_name, host, len(data.ports), len(data.fdb),
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
