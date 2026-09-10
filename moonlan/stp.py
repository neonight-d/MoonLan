"""Spanning tree state (BRIDGE-MIB, 1.3.6.1.2.1.17.2) — honestly read.

The trap this module exists to avoid: a switch with STP **disabled**
still answers every dot1dStp* object. It reports priority 0, root cost
0 and, most misleadingly, itself as the designated root. Read
uncritically, five such switches look like five root bridges of five
trees; that is exactly the false diagnosis this project started from.

So the root, the cost and the root port are used ONLY for a switch
that passes `is_operating`:

- at least one port has dot1dStpPortEnable = enabled(1), and
- at least one port is in a state other than disabled(1), and
- the tree has actually converged at some point: either
  dot1dStpTopChanges > 0, or dot1dStpTimeSinceTopologyChange is
  meaningfully younger than sysUpTime (a topology change happened
  after boot).

Everything else is reported as `stp_not_operating`, with the raw
values kept for `diag --stp` so the verdict can be checked.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .snmpval import as_octets

log = logging.getLogger(__name__)

OID_SYS_UPTIME = "1.3.6.1.2.1.1.3.0"

OID_STP_PROTOCOL_SPEC = "1.3.6.1.2.1.17.2.1.0"
OID_STP_PRIORITY = "1.3.6.1.2.1.17.2.2.0"
OID_STP_TIME_SINCE_CHANGE = "1.3.6.1.2.1.17.2.3.0"
OID_STP_TOP_CHANGES = "1.3.6.1.2.1.17.2.4.0"
OID_STP_DESIGNATED_ROOT = "1.3.6.1.2.1.17.2.5.0"
OID_STP_ROOT_COST = "1.3.6.1.2.1.17.2.6.0"
OID_STP_ROOT_PORT = "1.3.6.1.2.1.17.2.7.0"

OID_STP_PORT_STATE = "1.3.6.1.2.1.17.2.15.1.3"
OID_STP_PORT_ENABLE = "1.3.6.1.2.1.17.2.15.1.4"
OID_STP_PORT_PATH_COST = "1.3.6.1.2.1.17.2.15.1.5"
OID_STP_PORT_DESIGNATED_ROOT = "1.3.6.1.2.1.17.2.15.1.6"
OID_STP_PORT_DESIGNATED_COST = "1.3.6.1.2.1.17.2.15.1.7"
OID_STP_PORT_DESIGNATED_BRIDGE = "1.3.6.1.2.1.17.2.15.1.8"

# RSTP extension — optional, absent on plain 802.1D agents
OID_STP_VERSION = "1.3.6.1.2.1.17.2.16.0"
OID_STP_EXT_ADMIN_EDGE = "1.3.6.1.2.1.17.2.19.1.2"
OID_STP_EXT_OPER_EDGE = "1.3.6.1.2.1.17.2.19.1.3"

PROTOCOL_SPEC = {1: "unknown", 2: "decLb100", 3: "ieee8021d"}
PORT_STATES = {
    1: "disabled", 2: "blocking", 3: "listening",
    4: "learning", 5: "forwarding", 6: "broken",
}
STP_VERSIONS = {0: "stpCompatible", 2: "rstp", 3: "mstp"}

STATE_DISABLED = 1
STATE_BLOCKING = 2
STATE_FORWARDING = 5

# How much younger than sysUpTime dot1dStpTimeSinceTopologyChange has
# to be before it counts as evidence that the tree ever converged.
# TimeTicks are hundredths of a second; 60 s covers the clock skew
# between two GETs and a switch that changed topology right at boot.
CONVERGED_MARGIN_TICKS = 6000


@dataclass
class StpPort:
    """One row of dot1dStpPortTable, keyed by bridge-port."""

    bridge_port: int
    if_index: int | None = None
    name: str = ""
    state: int = 0
    enabled: bool = False
    path_cost: int = 0
    designated_root: str = ""
    designated_cost: int = 0
    designated_bridge: str = ""
    admin_edge: bool | None = None
    oper_edge: bool | None = None

    @property
    def state_name(self) -> str:
        # 2b0 answers dot1dStpPortState with 0, which is outside the
        # enum. Printing the raw number invites it to be read as a
        # state; it is the absence of one.
        return PORT_STATES.get(self.state, "n/a")

    @property
    def blocking(self) -> bool:
        return self.state == STATE_BLOCKING


@dataclass
class StpData:
    """dot1dStp* of one switch plus the verdict on whether it means anything."""

    supported: bool = False
    protocol_spec: int = 0
    priority: int = 0
    time_since_change: int = 0   # TimeTicks
    top_changes: int = 0
    designated_root: str = ""    # "32768/34:0a:33:bc:ca:f0"
    root_cost: int = 0
    root_port: int = 0
    version: int | None = None
    sys_uptime: int = 0
    ports: dict[int, StpPort] = field(default_factory=dict)
    operating: bool = False
    reason: str = ""             # why it is not operating, for diagnostics

    @property
    def protocol_name(self) -> str:
        return PROTOCOL_SPEC.get(self.protocol_spec, str(self.protocol_spec))

    @property
    def version_name(self) -> str:
        if self.version is None:
            return ""
        return STP_VERSIONS.get(self.version, str(self.version))

    @property
    def root_mac(self) -> str:
        return self.designated_root.partition("/")[2]

    def is_root(self, own_macs: set[str]) -> bool:
        """True when this switch itself is the designated root.

        Only meaningful for an operating switch: a disabled one always
        names itself.
        """
        return bool(self.operating and self.root_mac and self.root_mac in own_macs)

    def blocking_ports(self) -> list[StpPort]:
        return [p for p in self.ports.values() if p.blocking]


def format_bridge_id(raw: bytes) -> str:
    """dot1dStpDesignatedRoot (8 bytes: 2 priority + 6 MAC) as text."""
    if len(raw) != 8:
        return raw.hex() if raw else ""
    priority = (raw[0] << 8) | raw[1]
    mac = ":".join(f"{b:02x}" for b in raw[2:])
    return f"{priority}/{mac}"


def judge(data: StpData) -> StpData:
    """Fills in `operating` and `reason` — the whole point of the module."""
    if not data.supported:
        data.operating = False
        data.reason = "the switch does not answer dot1dStp* at all"
        return data
    enabled = [p for p in data.ports.values() if p.enabled]
    active = [p for p in data.ports.values() if p.state != STATE_DISABLED]
    if not enabled:
        data.operating = False
        data.reason = "no port has dot1dStpPortEnable = enabled"
        return data
    if not active:
        data.operating = False
        data.reason = "every port is in state disabled(1)"
        return data
    converged = data.top_changes > 0 or (
        data.sys_uptime > 0
        and data.sys_uptime - data.time_since_change > CONVERGED_MARGIN_TICKS
    )
    if not converged:
        data.operating = False
        data.reason = (
            f"the tree never converged: topology changes "
            f"{data.top_changes}, time since change "
            f"{data.time_since_change} ticks vs uptime {data.sys_uptime}"
        )
        return data
    data.operating = True
    data.reason = ""
    return data


async def collect_stp(collector, host: str, port_to_ifindex: dict[int, int]):
    """Walks BRIDGE-MIB dot1dStp* of one switch and judges the result.

    A switch without the objects (or with SNMP access to them denied)
    comes back with supported=False, which reads as "not operating"
    everywhere downstream.
    """
    data = StpData()

    uptime = await collector._get(host, OID_SYS_UPTIME)
    if uptime is not None:
        try:
            data.sys_uptime = int(uptime)
        except (TypeError, ValueError):
            data.sys_uptime = 0

    spec = await collector._get(host, OID_STP_PROTOCOL_SPEC)
    if spec is None:
        return judge(data)
    try:
        data.protocol_spec = int(spec)
    except (TypeError, ValueError):
        return judge(data)
    data.supported = True

    for oid, attr in (
        (OID_STP_PRIORITY, "priority"),
        (OID_STP_TIME_SINCE_CHANGE, "time_since_change"),
        (OID_STP_TOP_CHANGES, "top_changes"),
        (OID_STP_ROOT_COST, "root_cost"),
        (OID_STP_ROOT_PORT, "root_port"),
    ):
        value = await collector._get(host, oid)
        if value is not None:
            try:
                setattr(data, attr, int(value))
            except (TypeError, ValueError):
                pass
    root = await collector._get(host, OID_STP_DESIGNATED_ROOT)
    if root is not None:
        data.designated_root = format_bridge_id(as_octets(root))
    version = await collector._get(host, OID_STP_VERSION)
    if version is not None:
        try:
            data.version = int(version)
        except (TypeError, ValueError):
            data.version = None

    def port(bridge_port: int) -> StpPort:
        entry = data.ports.get(bridge_port)
        if entry is None:
            entry = StpPort(
                bridge_port=bridge_port,
                if_index=port_to_ifindex.get(bridge_port),
            )
            data.ports[bridge_port] = entry
        return entry

    async for suffix, value in collector._walk(host, OID_STP_PORT_STATE):
        port(suffix[0]).state = int(value)
    async for suffix, value in collector._walk(host, OID_STP_PORT_ENABLE):
        port(suffix[0]).enabled = int(value) == 1
    async for suffix, value in collector._walk(host, OID_STP_PORT_PATH_COST):
        port(suffix[0]).path_cost = int(value)
    async for suffix, value in collector._walk(host, OID_STP_PORT_DESIGNATED_ROOT):
        port(suffix[0]).designated_root = format_bridge_id(as_octets(value))
    async for suffix, value in collector._walk(host, OID_STP_PORT_DESIGNATED_COST):
        port(suffix[0]).designated_cost = int(value)
    async for suffix, value in collector._walk(host, OID_STP_PORT_DESIGNATED_BRIDGE):
        port(suffix[0]).designated_bridge = format_bridge_id(as_octets(value))
    # RSTP extension: present only on agents that implement it
    async for suffix, value in collector._walk(host, OID_STP_EXT_ADMIN_EDGE):
        port(suffix[0]).admin_edge = int(value) == 1
    async for suffix, value in collector._walk(host, OID_STP_EXT_OPER_EDGE):
        port(suffix[0]).oper_edge = int(value) == 1

    return judge(data)


def network_verdict(per_switch: dict[str, StpData]) -> dict:
    """The one-line answer for the whole network.

    verdict: "not_operating" — nobody is running a tree;
             "single" — every operating switch agrees on one root;
             "fragmented" — operating switches report different roots.
    """
    operating = {ip: s for ip, s in per_switch.items() if s.operating}
    if not operating:
        return {
            "verdict": "not_operating",
            "roots": {},
            "operating": [],
            "total": len(per_switch),
        }
    roots: dict[str, list[str]] = {}
    for ip, data in sorted(operating.items()):
        roots.setdefault(data.designated_root or "unknown", []).append(ip)
    return {
        "verdict": "single" if len(roots) == 1 else "fragmented",
        "roots": roots,
        "operating": sorted(operating),
        "total": len(per_switch),
    }
