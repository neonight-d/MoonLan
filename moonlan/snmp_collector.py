"""SNMP v2c switch collector.

Collects the minimum data required to build and maintain network topology:
- sysName, sysDescr, sysObjectID
- interface information
- bridge MAC / forwarding database
- bridge-port -> ifIndex mapping
- LACP membership
- VLAN/PVID information
- LLDP neighbours
- spanning-tree state
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from . import lldp as lldp_mod
from . import stp as stp_mod
from .snmpval import as_octets, is_no_such

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


# ---------------------------------------------------------------------------
# OIDs
# ---------------------------------------------------------------------------

OID_SYS_NAME = "1.3.6.1.2.1.1.5.0"
OID_SYS_DESCR = "1.3.6.1.2.1.1.1.0"
OID_SYS_OBJECT_ID = "1.3.6.1.2.1.1.2.0"

OID_IF_DESCR = "1.3.6.1.2.1.2.2.1.2"
OID_IF_TYPE = "1.3.6.1.2.1.2.2.1.3"
OID_IF_PHYS_ADDRESS = "1.3.6.1.2.1.2.2.1.6"
OID_IF_NAME = "1.3.6.1.2.1.31.1.1.1.1"
OID_IF_OPER_STATUS = "1.3.6.1.2.1.2.2.1.8"
OID_IF_LAST_CHANGE = "1.3.6.1.2.1.2.2.1.9"
OID_IF_HIGH_SPEED = "1.3.6.1.2.1.31.1.1.1.15"

OID_BRIDGE_ADDRESS = "1.3.6.1.2.1.17.1.1.0"
OID_PORT_IFINDEX = "1.3.6.1.2.1.17.1.4.1.2"

OID_FDB_PORT = "1.3.6.1.2.1.17.4.3.1.2"
OID_Q_FDB_PORT = "1.3.6.1.2.1.17.7.1.2.2.1.2"

OID_ARP_PHYS = "1.3.6.1.2.1.4.22.1.2"

OID_LAG_ATTACHED_ID = "1.2.840.10006.300.43.1.2.1.1.13"

OID_PVID = "1.3.6.1.2.1.17.7.1.4.5.1.1"
OID_VLAN_NAME = "1.3.6.1.2.1.17.7.1.4.3.1.1"


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

IF_TYPE_ETHERNET = 6

# Some switches use non-standard values for physical Ethernet ports.
PHYSICAL_IF_TYPES = frozenset({
    IF_TYPE_ETHERNET,
    62,   # fastEther
    69,   # fastEtherFX
    117,  # gigabitEthernet
})

RESUME_PAUSE = 0.2

FDB_SUFFIX_LEN = {
    6: "dot1dTpFdbPort",
    7: "dot1qTpFdbPort",
}

FDB_TABLES = (
    (OID_FDB_PORT, 6),
    (OID_Q_FDB_PORT, 7),
)

BROADCAST_MAC = "ff:ff:ff:ff:ff:ff"
ZERO_MAC = "00:00:00:00:00:00"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class WalkStatus:
    """How one walk of one OID subtree ended."""

    oid: str
    rows: int = 0
    error: str = ""
    truncated: bool = False
    last_oid: str = ""
    resumes: int = 0

    @property
    def complete(self) -> bool:
        return not self.error and not self.truncated


@dataclass
class PortInfo:
    if_index: int
    name: str = ""
    oper_up: bool = False
    speed_mbps: int = 0
    is_physical: bool = True
    mac: str = ""
    last_change: int = 0


@dataclass
class SwitchData:
    """Everything collected from a single switch."""

    ip: str
    reachable: bool = False

    sys_name: str = ""
    sys_descr: str = ""
    sys_object_id: str = ""

    bridge_mac: str = ""
    own_macs: set[str] = field(default_factory=set)

    ports: dict[int, PortInfo] = field(default_factory=dict)
    fdb: dict[str, int] = field(default_factory=dict)

    lag_members: dict[int, int] = field(default_factory=dict)
    lag_groups: dict[int, list[int]] = field(default_factory=dict)

    port_pvid: dict[int, int] = field(default_factory=dict)
    vlan_names: dict[int, str] = field(default_factory=dict)

    lldp_neighbors: list = field(default_factory=list)
    lldp_forwarded: set[int] = field(default_factory=set)
    lldp_crowded: set[int] = field(default_factory=set)

    port_labels: dict[int, str] = field(default_factory=dict)

    stp: object | None = None
    sys_uptime: int = 0

    loop_detection: object | None = None


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def _fmt_mac(raw: bytes) -> str:
    return ":".join(f"{byte:02x}" for byte in raw)


def port_display(data: "SwitchData", if_index: int) -> str:
    """Return the operator-facing port name, falling back to ifIndex."""
    port = data.ports.get(if_index)
    return port.name if port and port.name else str(if_index)


def aggregate_port(
    sw: SwitchData,
    if_index: int | None,
) -> int | None:
    """Resolve a physical port to its logical aggregate port."""

    if if_index is None:
        return None

    aggregate = sw.lag_members.get(if_index)
    if aggregate is not None:
        return aggregate

    for bridge_port, members in sw.lag_groups.items():
        if if_index in members:
            return -bridge_port

    return if_index


def _name_stp_ports(data: "SwitchData") -> None:
    """Resolve STP bridge-port entries to useful operator-facing names."""

    if data.stp is None:
        return

    member_to_bridge = {
        member: bridge_port
        for bridge_port, group in data.lag_groups.items()
        for member in group
    }

    for entry in data.stp.ports.values():
        if entry.if_index is not None:
            entry.name = port_display(data, entry.if_index)
            continue

        bridge_port = entry.bridge_port

        # Synthetic trunk.
        if bridge_port in data.lag_groups:
            trunk = data.ports.get(-bridge_port)
            if trunk is not None:
                entry.if_index = -bridge_port
                entry.name = trunk.name
                continue

        # Physical member of an inferred aggregate.
        aggregate = member_to_bridge.get(bridge_port)
        port = data.ports.get(bridge_port)

        if aggregate is None or port is None or not port.is_physical:
            continue

        entry.if_index = bridge_port
        entry.name = port.name or str(bridge_port)

        trunk = data.ports.get(-aggregate)
        if trunk is not None:
            entry.name += f" (in {trunk.name})"


def is_random_mac(mac: str) -> bool:
    """Return True for locally administered MAC addresses."""
    try:
        return bool(int(mac.split(":", 1)[0], 16) & 0x02)
    except (ValueError, IndexError):
        return False


def is_valid_mac(mac: str) -> bool:
    """Return True for a valid unicast, non-zero MAC address."""

    parts = mac.split(":")
    if len(parts) != 6:
        return False

    try:
        octets = [int(part, 16) for part in parts]
    except ValueError:
        return False

    if any(not 0 <= octet <= 255 for octet in octets):
        return False

    if octets[0] & 0x01:
        return False

    return mac != ZERO_MAC


def parse_fdb_entry(
    suffix: tuple[int, ...],
    value,
    expected_len: int,
) -> tuple[str, int, str]:
    """Parse one FDB row into (mac, bridge_port, reason)."""

    if len(suffix) != expected_len:
        return (
            "",
            0,
            f"suffix has {len(suffix)} components, expected {expected_len}",
        )

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


def sane_lag_members(
    data: "SwitchData",
    claimed: dict[int, int],
    host: str = "",
) -> dict[int, int]:
    """Filter LACP membership against interfaces actually present."""

    if not claimed:
        return {}

    physical = {
        if_index
        for if_index, port in data.ports.items()
        if port.is_physical and if_index > 0
    }

    members: dict[int, int] = {}
    unknown: dict[int, int] = {}

    for member, aggregate in claimed.items():
        if aggregate > 0 and aggregate in data.ports:
            members[member] = aggregate
        else:
            unknown[member] = aggregate

    if unknown:
        values = sorted(set(unknown.values()))
        log.warning(
            "%s: dot3adAggPortAttachedAggID names %d port(s) as members "
            "of unknown aggregate(s) %s; dropping those rows",
            host or data.ip,
            len(unknown),
            ", ".join(str(value) for value in values[:5]),
        )

    by_aggregate: dict[int, set[int]] = {}

    for member, aggregate in members.items():
        by_aggregate.setdefault(aggregate, set()).add(member)

    for aggregate, group in by_aggregate.items():
        if physical and group >= physical:
            log.warning(
                "%s: aggregate %s claims all %d physical port(s); "
                "dropping the aggregate",
                host or data.ip,
                aggregate,
                len(physical),
            )
            for member in group:
                members.pop(member, None)

    return members


def infer_lag_groups(
    physical: set[int],
    mapped: set[int],
    synthetic: set[int],
) -> dict[int, list[int]]:
    """Infer LAG groups from missing bridge-port mappings."""

    if not mapped:
        return {}

    missing = sorted(physical - mapped)
    trunks = sorted(synthetic & set(missing))

    if not trunks:
        return {}

    groups: dict[int, list[int]] = {}

    for member in missing:
        nearest = min(
            trunks,
            key=lambda trunk: (abs(member - trunk), trunk),
        )
        groups.setdefault(nearest, []).append(member)

    return groups


# ---------------------------------------------------------------------------
# SNMP collector
# ---------------------------------------------------------------------------

class SnmpCollector:
    """Reusable asynchronous SNMP v2c client."""

    def __init__(
        self,
        community: str,
        timeout: int = 2,
        retries: int = 1,
        retries_on_break: int = 2,
    ):
        self._community = CommunityData(community, mpModel=1)
        self._timeout = timeout
        self._retries = retries
        self._retries_on_break = retries_on_break

        # One engine per collector.
        self._engine = SnmpEngine()

        # Reuse UDP targets instead of creating one every request.
        self._targets: dict[str, UdpTransportTarget] = {}

        # Keep the latest state of each walk.
        self._walk_status: dict[tuple[str, str], WalkStatus] = {}

    async def _target(self, host: str) -> UdpTransportTarget:
        target = self._targets.get(host)

        if target is None:
            target = await UdpTransportTarget.create(
                (host, 161),
                timeout=self._timeout,
                retries=self._retries,
            )
            self._targets[host] = target

        return target

    async def _get(self, host: str, oid: str):
        """GET one OID. Returns None on any failure."""

        try:
            error_ind, error_status, _, var_binds = await get_cmd(
                self._engine,
                self._community,
                await self._target(host),
                ContextData(),
                ObjectType(ObjectIdentity(oid)),
            )
        except Exception as exc:
            log.warning(
                "%s: GET of %s failed (%s)",
                host,
                oid,
                exc,
            )
            return None

        if error_ind or error_status:
            log.debug(
                "%s GET %s: %s",
                host,
                oid,
                error_ind or error_status,
            )
            return None

        if not var_binds:
            log.debug("%s GET %s returned no varBinds", host, oid)
            return None

        value = var_binds[0][1]

        if is_no_such(value):
            log.debug(
                "%s GET %s: %s",
                host,
                oid,
                type(value).__name__,
            )
            return None

        return value

    async def _open_walk(self, host: str, start: str):
        """Open a walk starting at the supplied OID."""

        return walk_cmd(
            self._engine,
            self._community,
            await self._target(host),
            ContextData(),
            ObjectType(ObjectIdentity(start)),
            lexicographicMode=True,
        )

    async def _walk(self, host: str, oid: str):
        """Walk an OID subtree and transparently resume broken walks."""

        base = tuple(int(part) for part in oid.split("."))

        status = WalkStatus(oid=oid)
        self._walk_status[(host, oid)] = status

        start = oid
        last_oid: tuple[int, ...] | None = None

        while True:
            broke = ""

            try:
                objects = await self._open_walk(host, start)
            except Exception as exc:
                status.error = str(exc)

                log.warning(
                    "%s: walk of %s could not be started (%s)",
                    host,
                    start,
                    exc,
                )
                return

            left_subtree = False

            try:
                async for error_ind, error_status, _, var_binds in objects:
                    if error_ind or error_status:
                        broke = str(error_ind or error_status)
                        break

                    for name, value in var_binds:
                        full = tuple(name)

                        # The walk has passed the requested subtree.
                        if full[: len(base)] != base:
                            left_subtree = True
                            break

                        last_oid = full

                        if is_no_such(value):
                            continue

                        status.rows += 1
                        yield full[len(base):], value

                    if left_subtree:
                        break

            except GeneratorExit:
                raise

            except Exception as exc:
                broke = str(exc)
                log.warning(
                    "%s: walk of %s raised (%s)",
                    host,
                    oid,
                    exc,
                )

            finally:
                await objects.aclose()

            # Successfully reached the end of the subtree.
            if left_subtree or not broke:
                status.error = ""
                status.truncated = False
                status.last_oid = (
                    ".".join(map(str, last_oid))
                    if last_oid
                    else ""
                )
                return

            status.error = broke

            # No more recovery possible.
            if (
                status.resumes >= self._retries_on_break
                or last_oid is None
            ):
                status.truncated = status.rows > 0
                status.last_oid = (
                    ".".join(map(str, last_oid))
                    if last_oid
                    else ""
                )

                log.warning(
                    "%s: walk of %s stopped after %d row(s) (%s)%s",
                    host,
                    oid,
                    status.rows,
                    broke,
                    (
                        f", last OID {status.last_oid}"
                        if status.last_oid
                        else ""
                    ),
                )
                return

            status.resumes += 1

            await asyncio.sleep(RESUME_PAUSE)

            # Resume from the last successfully received OID.
            start = ".".join(map(str, last_oid))

    def last_walk_status(
        self,
        host: str,
        oid: str,
    ) -> "WalkStatus":
        """Return the latest status for a walk."""

        return self._walk_status.get(
            (host, oid)
        ) or WalkStatus(oid=oid)

    def last_walk_error(self, host: str, oid: str) -> str:
        """Return the latest walk error, if any."""

        return self.last_walk_status(host, oid).error

    async def collect(self, host: str) -> SwitchData:
        """Collect all topology-relevant data for one switch."""

        data = SwitchData(ip=host)

        # ------------------------------------------------------------------
        # Basic system information
        # ------------------------------------------------------------------

        sys_name = await self._get(host, OID_SYS_NAME)

        if sys_name is None:
            log.warning(
                "Switch %s does not respond to SNMP",
                host,
            )
            return data

        data.reachable = True
        data.sys_name = str(sys_name)

        # These three requests are independent, so do them concurrently.
        sys_descr, sys_object_id, bridge_mac = await asyncio.gather(
            self._get(host, OID_SYS_DESCR),
            self._get(host, OID_SYS_OBJECT_ID),
            self._get(host, OID_BRIDGE_ADDRESS),
        )

        data.sys_descr = str(sys_descr) if sys_descr is not None else ""
        data.sys_object_id = (
            str(sys_object_id)
            if sys_object_id is not None
            else ""
        )

        if bridge_mac is not None:
            raw = as_octets(bridge_mac)

            if len(raw) == 6 and any(raw):
                data.bridge_mac = _fmt_mac(raw)
                data.own_macs.add(data.bridge_mac)

        # ------------------------------------------------------------------
        # Interface MACs
        # ------------------------------------------------------------------

        phys_addr: dict[int, str] = {}

        async for suffix, value in self._walk(
            host,
            OID_IF_PHYS_ADDRESS,
        ):
            if not suffix:
                continue

            raw = as_octets(value)

            if len(raw) != 6 or not any(raw):
                continue

            mac = _fmt_mac(raw)
            if_index = suffix[0]

            data.own_macs.add(mac)
            phys_addr[if_index] = mac

        # ------------------------------------------------------------------
        # Interface table
        # ------------------------------------------------------------------

        if_descr: dict[int, str] = {}

        async for suffix, value in self._walk(
            host,
            OID_IF_DESCR,
        ):
            if not suffix:
                continue

            if_index = suffix[0]
            description = str(value)

            if_descr[if_index] = description
            data.ports[if_index] = PortInfo(
                if_index=if_index,
                name=description,
            )

        async for suffix, value in self._walk(
            host,
            OID_IF_NAME,
        ):
            if not suffix:
                continue

            port = data.ports.get(suffix[0])

            if port is None:
                continue

            name = str(value).strip()

            if name:
                port.name = name

        async for suffix, value in self._walk(
            host,
            OID_IF_TYPE,
        ):
            if not suffix:
                continue

            port = data.ports.get(suffix[0])

            if port is None:
                continue

            try:
                port.is_physical = int(value) in PHYSICAL_IF_TYPES
            except (TypeError, ValueError):
                log.debug(
                    "%s: invalid ifType for ifIndex %s: %r",
                    host,
                    suffix[0],
                    value,
                )

        async for suffix, value in self._walk(
            host,
            OID_IF_OPER_STATUS,
        ):
            if not suffix:
                continue

            port = data.ports.get(suffix[0])

            if port is None:
                continue

            try:
                port.oper_up = int(value) == 1
            except (TypeError, ValueError):
                continue

        async for suffix, value in self._walk(
            host,
            OID_IF_HIGH_SPEED,
        ):
            if not suffix:
                continue

            port = data.ports.get(suffix[0])

            if port is None:
                continue

            try:
                port.speed_mbps = int(value)
            except (TypeError, ValueError):
                continue

        async for suffix, value in self._walk(
            host,
            OID_IF_LAST_CHANGE,
        ):
            if not suffix:
                continue

            port = data.ports.get(suffix[0])

            if port is None:
                continue

            try:
                port.last_change = int(value)
            except (TypeError, ValueError):
                continue

        for if_index, mac in phys_addr.items():
            port = data.ports.get(if_index)

            if port is not None:
                port.mac = mac

        # ------------------------------------------------------------------
        # LACP
        # ------------------------------------------------------------------

        claimed: dict[int, int] = {}

        async for suffix, value in self._walk(
            host,
            OID_LAG_ATTACHED_ID,
        ):
            if not suffix:
                continue

            try:
                member = suffix[0]
                aggregate = int(value)
            except (TypeError, ValueError):
                continue

            if aggregate and aggregate != member:
                claimed[member] = aggregate

        data.lag_members = sane_lag_members(
            data,
            claimed,
            host,
        )

        # ------------------------------------------------------------------
        # Bridge-port -> ifIndex
        # ------------------------------------------------------------------

        port_to_ifindex: dict[int, int] = {}

        async for suffix, value in self._walk(
            host,
            OID_PORT_IFINDEX,
        ):
            if not suffix:
                continue

            try:
                port_to_ifindex[suffix[0]] = int(value)
            except (TypeError, ValueError):
                continue

        # ------------------------------------------------------------------
        # STP
        # ------------------------------------------------------------------

        data.stp = await stp_mod.collect_stp(
            self,
            host,
            port_to_ifindex,
            data.own_macs,
            {
                if_index: port.oper_up
                for if_index, port in data.ports.items()
            },
        )

        if data.stp is not None:
            data.sys_uptime = data.stp.sys_uptime

        # ------------------------------------------------------------------
        # VLAN / PVID
        # ------------------------------------------------------------------

        async for suffix, value in self._walk(
            host,
            OID_PVID,
        ):
            if not suffix:
                continue

            if_index = port_to_ifindex.get(suffix[0])

            if if_index is None:
                continue

            try:
                data.port_pvid[if_index] = int(value)
            except (TypeError, ValueError):
                continue

        async for suffix, value in self._walk(
            host,
            OID_VLAN_NAME,
        ):
            if not suffix:
                continue

            try:
                vlan_id = suffix[-1]
                data.vlan_names[vlan_id] = str(value).strip()
            except (TypeError, ValueError):
                continue

        # ------------------------------------------------------------------
        # FDB / MAC forwarding table
        # ------------------------------------------------------------------

        unmapped: dict[int, int] = {}
        bad_suffix = 0
        bad_mac = 0

        for fdb_oid, expected_len in FDB_TABLES:
            async for suffix, value in self._walk(
                host,
                fdb_oid,
            ):
                mac, bridge_port, reason = parse_fdb_entry(
                    suffix,
                    value,
                    expected_len,
                )

                if reason:
                    if "suffix" in reason:
                        bad_suffix += 1
                    else:
                        bad_mac += 1

                    log.debug(
                        "%s: FDB entry rejected (%s): %s",
                        host,
                        reason,
                        mac,
                    )
                    continue

                # Bridge-port zero is normally the switch itself / CPU.
                if bridge_port == 0:
                    continue

                # Ignore duplicate rows between BRIDGE-MIB and Q-BRIDGE-MIB.
                if mac in data.fdb:
                    continue

                if_index = port_to_ifindex.get(bridge_port)

                if if_index is None:
                    # Preserve the synthetic negative-ifIndex behavior.
                    if_index = -bridge_port

                    unmapped[bridge_port] = (
                        unmapped.get(bridge_port, 0) + 1
                    )

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
            "%s FDB: %d entries rejected "
            "(bad suffix: %d, bad MAC: %d)",
            host,
            rejected,
            bad_suffix,
            bad_mac,
        )

        if unmapped:
            log.debug(
                "%s: FDB entries on unmapped bridge-ports: %s",
                host,
                "; ".join(
                    f"port {port}: {count} MACs"
                    for port, count in sorted(unmapped.items())
                ),
            )

        # ------------------------------------------------------------------
        # Infer vendor-specific LAG groups
        # ------------------------------------------------------------------

        physical_ports = {
            port.if_index
            for port in data.ports.values()
            if port.is_physical and port.if_index > 0
        }

        data.lag_groups = infer_lag_groups(
            physical_ports,
            set(port_to_ifindex),
            set(unmapped),
        )

        if data.lag_groups:
            log.debug(
                "%s: inferred LAG groups: %s",
                host,
                "; ".join(
                    f"bridge-port {bridge_port}: "
                    f"members {', '.join(map(str, members))}"
                    for bridge_port, members
                    in sorted(data.lag_groups.items())
                ),
            )

        _name_stp_ports(data)

        # ------------------------------------------------------------------
        # LLDP
        # ------------------------------------------------------------------

        (
            data.lldp_neighbors,
            data.port_labels,
        ) = await lldp_mod.collect_lldp(
            self,
            host,
            lldp_mod.build_port_names(
                data.ports,
                phys_addr,
            ),
            set(data.ports),
            data.fdb,
        )

        data.port_labels, dropped = lldp_mod.useful_port_labels(
            data.port_labels,
            {
                if_index: port.name
                for if_index, port in data.ports.items()
            },
            if_descr,
        )

        if dropped:
            log.debug(
                "%s: %d LLDP port label(s) dropped",
                host,
                dropped,
            )

        data.lldp_forwarded, data.lldp_crowded = (
            lldp_mod.analyse_ports(
                data.lldp_neighbors,
                lambda if_index: aggregate_port(data, if_index),
                data.fdb,
            )
        )

        if data.lldp_forwarded:
            log.warning(
                "%s: LLDP on port(s) %s did not come from the cable; "
                "excluding them from link inference",
                host,
                ", ".join(
                    port_display(data, if_index)
                    for if_index in sorted(data.lldp_forwarded)
                ),
            )

        if data.lldp_crowded:
            log.debug(
                "%s: several LLDP devices behind port(s) %s; "
                "no links inferred from them",
                host,
                ", ".join(
                    port_display(data, if_index)
                    for if_index in sorted(data.lldp_crowded)
                ),
            )

        # ------------------------------------------------------------------
        # Summary
        # ------------------------------------------------------------------

        log.info(
            "%s (%s): %d ports, %d MAC addresses, "
            "%d LLDP neighbour(s), STP %s",
            data.sys_name,
            host,
            len(data.ports),
            len(data.fdb),
            len(data.lldp_neighbors),
            (
                "operating"
                if data.stp and data.stp.operating
                else "not operating"
            ),
        )

        return data

    async def collect_arp(
        self,
        host: str,
    ) -> dict[str, str]:
        """Collect the ARP table as MAC -> IP."""

        arp: dict[str, str] = {}

        async for suffix, value in self._walk(
            host,
            OID_ARP_PHYS,
        ):
            if len(suffix) < 4:
                continue

            raw = as_octets(value)

            if len(raw) != 6:
                continue

            ip = ".".join(
                str(octet)
                for octet in suffix[-4:]
            )

            arp[_fmt_mac(raw)] = ip

        log.info(
            "ARP from %s: %d entries",
            host,
            len(arp),
        )

        return arp
