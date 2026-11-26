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
- sysObjectID                              (SNMPv2-MIB; picks the
                                            loop-detection profile,
                                            see loopdetect.py)
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from . import lldp as lldp_mod
from . import pinger
from .routers import RouterIdentity, identity_from_tables
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

# Numeric OIDs so we do not depend on MIB file loading
OID_SYS_NAME = "1.3.6.1.2.1.1.5.0"
OID_SYS_DESCR = "1.3.6.1.2.1.1.1.0"
# sysObjectID: the vendor's model identifier, and the key the
# loop-detection profiles are chosen by
OID_SYS_OBJECT_ID = "1.3.6.1.2.1.1.2.0"
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
OID_IP_AD_ENT_IF_INDEX = "1.3.6.1.2.1.4.20.1.2"  # ipAdEntIfIndex.<IP> = ifIndex
# dot3adAggPortAttachedAggID.<member ifIndex> -> aggregate ifIndex (IEEE8023-LAG-MIB)
OID_LAG_ATTACHED_ID = "1.2.840.10006.300.43.1.2.1.1.13"
OID_PVID = "1.3.6.1.2.1.17.7.1.4.5.1.1"       # dot1qPvid.<bridge-port>
OID_VLAN_NAME = "1.3.6.1.2.1.17.7.1.4.3.1.1"  # dot1qVlanStaticName.<VLAN ID>

IF_TYPE_ETHERNET = 6  # ethernetCsmacd
# ifType values that mean a physical Ethernet port. Some switches (e.g.
# D-Link DES-3526 combo gigabit ports) report types other than 6:
# 62 = fastEther, 69 = fastEtherFX, 117 = gigabitEthernet.
PHYSICAL_IF_TYPES = {IF_TYPE_ETHERNET, 62, 69, 117}

# Seconds between giving up on a walk and picking it back up. An agent
# that stopped answering mid-table is busy, not broken; asking again
# immediately gets the same silence.
RESUME_PAUSE = 0.2


# What `last_walk_status` reports for a walk that was never sent. It
# has to read as "no answer", not as "an empty table": the difference
# between those two is the whole point of WalkStatus.
SKIPPED_ERROR = (
    "not asked: this OID has answered nothing but timeouts on this host"
)


def is_timeout(error: str) -> bool:
    """True for the one failure the dead-OID rule acts on.

    pysnmp reports it as "No SNMP response received before timeout".
    An agent error, a malformed OID, a table that ends early — none of
    those mean the agent will go on saying nothing, and none of them
    cost a full timeout budget to ask again.
    """
    return "timeout" in error.lower()


@dataclass
class DeadOid:
    """How one (host, OID) pair has been behaving."""

    strikes: int = 0     # walks in a row with no rows and a timeout
    skip_until: int = 0  # scan cycle at which it is tried again


# Objects every SNMP agent has to implement. Silence on one of these
# is never "the agent does not have it".
MANDATORY_OIDS = frozenset({
    OID_SYS_NAME, OID_SYS_DESCR, OID_SYS_OBJECT_ID,
})

# What silence on an OID turned out to mean
SILENCE_COMMUNITY = "community"    # answers ping, says nothing to SNMP
SILENCE_UNREACHABLE = "unreachable"  # says nothing to anything
SILENCE_OID = "oid"                # the agent is alive; this OID is not


async def diagnose_silence(
    collector, host: str, oid: str = ""
) -> tuple[str, str]:
    """Why an OID returned nothing: (verdict, a sentence for a human).

    SNMPv2c does not answer a wrong community string at all. From the
    outside that is indistinguishable from an agent that does not
    implement the object — and MoonLan used to print exactly that:
    "the subtree is empty, or the agent does not implement it". Both
    halves were wrong on a switch that pings in 3 ms, implements
    sysName and works perfectly; somebody had put `community: public`
    back into the config. The answer was not merely incomplete, it
    pointed away from the cause.

    So: ask for sysDescr, which every agent must implement, and ping
    the host. Silent SNMP on a host that answers ICMP is a community
    string or a disabled agent, not a missing OID. Asking about an
    object that is itself mandatory skips the redundant probe — its
    silence already carries the same weight.
    """
    if oid not in MANDATORY_OIDS:
        probe = await collector._get(host, OID_SYS_DESCR)
        if probe is not None:
            return SILENCE_OID, (
                f"the agent is alive and answers sysDescr "
                f"({str(probe)[:60]!r}), so this OID really is empty or "
                f"not implemented on it"
            )
    alive = await pinger.ping(host)
    if alive:
        return SILENCE_COMMUNITY, (
            "the host answers ping but says nothing to sysDescr either — "
            "most likely the community string does not match, or SNMP is "
            "switched off on it. An agent that does not implement an OID "
            "does not look like this: it answers, and says it has no such "
            "object"
        )
    return SILENCE_UNREACHABLE, (
        "the host answers neither SNMP nor ping — it is not reachable "
        "from here at all, which is a network question before it is an "
        "SNMP one"
    )


@dataclass
class WalkStatus:
    """How one walk of one OID subtree ended."""

    oid: str
    rows: int = 0
    error: str = ""          # what it stopped with, if it stopped
    truncated: bool = False  # rows arrived and then it stopped answering
    last_oid: str = ""       # the last OID that did arrive
    resumes: int = 0         # how many times the walk was picked back up
    # the agent broke the GETNEXT rules and the walk was ended for it
    # (v0.8.8): FAULT_REPEATED_OID or FAULT_NO_SUCH_RUN, else ""
    fault: str = ""
    # how many times an answer went back to an OID before the previous
    # one, not seen yet in the walk: the agent's order is not the OIDs'
    # order, and the table is read on to its end all the same
    out_of_order: int = 0

    @property
    def complete(self) -> bool:
        return not self.error and not self.truncated


# A walk the agent itself sends round in circles (v0.8.8). RFC 3416
# §4.2.2: the answer to GETNEXT always carries an OID strictly past the
# one asked for, and never noSuchObject / noSuchInstance — the end of
# the MIB is endOfMibView. An agent of RouterOS 6.x answered GETNEXT on
# lldpRemManAddrIfSubtype with that very OID and noSuchObject, every
# time. pysnmp's walk_cmd stops only on its own Null or endOfMibView,
# and does not check what comes back; _walk skipped no-such rows
# without counting them. So the same request went out hundreds of
# times a second until the poll budget took the whole poll down — and
# slowed every other poll sharing the event loop meanwhile.
#
# What makes the loop is a repeat: the OID asked for coming back, or a
# row this walk has already had (an agent that goes back to the start
# of its table at the end). An answer that only goes back — to an OID
# before the previous one, never seen in this walk — is the agent's own
# order, not a loop: an access switch handed out its Q-BRIDGE table
# VLAN by VLAN, not in numeric order, and the rows after the step back
# exist nowhere else. Such a table is read on to its end. (net-snmp's
# snmpwalk stops on "OID not increasing" unless told -Cc; ending there
# lost those rows on every scan.)
FAULT_REPEATED_OID = "repeated_oid"
# OIDs that do grow, each answered noSuchObject / noSuchInstance: one
# such row in a table is skipped — a damaged row should not cost the
# table — but this many in a row is an agent walking a table it does
# not have, one request per row, and the walk ends there.
FAULT_NO_SUCH_RUN = "no_such_run"
WALK_MAX_NO_SUCH = 100


@dataclass
class WalkFault:
    """A (host, OID) whose walk the agent broke, while it keeps doing so."""

    fault: str
    rows: int
    last_oid: str
    error: str
    first_at: float
    last_at: float
    warned: bool = False   # this episode got the WARNING


@dataclass
class WalkTiming:
    """How long one walk of a poll took (v0.8.8), by the monotonic clock.

    `seconds` stays None while the walk runs. A walk the poll budget
    cancels keeps it None: that record is where the budget went, and
    the server reads it after the cancellation.
    """

    status: WalkStatus
    started: float
    seconds: float | None = None

    @property
    def running(self) -> bool:
        return self.seconds is None

    def took(self) -> float:
        if self.seconds is not None:
            return self.seconds
        return time.monotonic() - self.started

    def report(self, resumes: bool = False) -> dict:
        entry = {
            "oid": self.status.oid, "name": oid_name(self.status.oid),
            "rows": self.status.rows, "seconds": round(self.took(), 1),
        }
        if resumes:
            entry["resumes"] = self.status.resumes
        return entry


# The modules whose OID_* constants name the OIDs MoonLan walks, in the
# order a name is looked for. No MIB files: the constant is the name
# the code itself knows the OID by.
OID_NAME_MODULES = (
    "moonlan.snmp_collector", "moonlan.lldp", "moonlan.stp",
    "moonlan.counters", "moonlan.loopdetect",
)
_OID_NAMES: dict[str, str] | None = None


def oid_name(oid: str) -> str:
    """LLDP_REM_MAN_ADDR_IF_SUBTYPE for its OID, "" for one without a name."""
    global _OID_NAMES
    if _OID_NAMES is None:
        import importlib

        names: dict[str, str] = {}
        for module_name in OID_NAME_MODULES:
            module = importlib.import_module(module_name)
            for attr, value in vars(module).items():
                if (
                    attr.startswith("OID_") and isinstance(value, str)
                    and value.replace(".", "").isdigit()
                ):
                    names.setdefault(value, attr[len("OID_"):])
        _OID_NAMES = names
    return _OID_NAMES.get(oid, "")


@dataclass
class PortInfo:
    if_index: int
    name: str = ""
    oper_up: bool = False
    speed_mbps: int = 0
    is_physical: bool = True  # ifType 6; aggregates/CPU/VLAN interfaces — False
    mac: str = ""             # ifPhysAddress: some agents use it as an LLDP port id
    last_change: int = 0      # ifLastChange, TimeTicks since sysUpTime epoch

    def to_dict(self) -> dict:
        return {
            "if_index": self.if_index, "name": self.name,
            "oper_up": self.oper_up, "speed_mbps": self.speed_mbps,
            "is_physical": self.is_physical, "mac": self.mac,
            "last_change": self.last_change,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "PortInfo":
        return cls(
            if_index=int(raw["if_index"]), name=str(raw.get("name", "")),
            oper_up=bool(raw.get("oper_up", False)),
            speed_mbps=int(raw.get("speed_mbps", 0)),
            is_physical=bool(raw.get("is_physical", True)),
            mac=str(raw.get("mac", "")),
            last_change=int(raw.get("last_change", 0)),
        )


# Where the data of a switch on the map comes from (v0.8.7). One of
# five, and the map, the card and /api/polling say which:
#   fresh        a full poll in this scan
#   over_budget  answered, did not finish inside its budget: the last
#                full reading, dated
#   unreachable  did not answer in this scan: the last full reading
#   restored     not polled since the start yet: the reading saved in
#                the database before the restart
#   never        no full reading anywhere: a node with nothing on it
STATE_FRESH = "fresh"
STATE_OVER_BUDGET = "over_budget"
STATE_UNREACHABLE = "unreachable"
STATE_RESTORED = "restored"
STATE_NEVER = "never"
# A reading somebody took earlier, not this scan's observation: drawn,
# never recorded as a sighting (v0.6.15)
READING_STATES = frozenset({STATE_OVER_BUDGET, STATE_UNREACHABLE, STATE_RESTORED})

# The shape of a reading saved in switch_readings. A reading in another
# format is ignored at the start rather than read wrong: one more
# reading after the next scan costs less than a map built from fields
# that meant something else.
READING_FORMAT = 1


class OverBudget:
    """A poll that ran past its budget, and whether the switch answered
    before it did (v0.8.7): True when its first answer came in time,
    None when the budget ran out before anything came back. An agent
    that does not answer at all is cut off inside collect() long before
    any budget — but "long before" is a matter of settings, so it is
    asked, not assumed."""

    def __init__(
        self, answered: bool | None = None, cut: dict | None = None,
        walks: list | None = None,
    ):
        self.answered = answered
        # where the budget ran out (v0.8.8): SnmpCollector.poll_cut()
        # and the slowest walks of the poll, a cut one included
        self.cut = cut
        self.walks = list(walks or [])


def _int_keys(raw: dict, value=lambda v: v) -> dict:
    """JSON keeps object keys as strings; the reading's are ints"""
    return {int(k): value(v) for k, v in (raw or {}).items()}


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
    # sysObjectID: the vendor's own model identifier. It picks the
    # loop-detection profile (see loopdetect.py) and is worth showing
    # in the switch card regardless.
    sys_object_id: str = ""
    # Loop detection from the vendor's private MIB, refreshed by the
    # counters loop rather than by the scan: a loop is an incident,
    # and ten minutes is too long to hear about one.
    loop_detection: object | None = None
    # When this reading was taken, and whether it is a reading at all.
    # A switch that did not finish inside its budget keeps the last
    # data it did produce, and both fields say so — "the map is from
    # 15:58" is a different statement from "the map is from now".
    polled_at: float = 0.0
    over_budget: bool = False
    # How long the last complete poll took, and how many scans in a row
    # have ended without one. A switch that answers and never finishes
    # is a third state next to "answering" and "down", and it needs its
    # own numbers to be said out loud.
    poll_seconds: float = 0.0
    over_budget_scans: int = 0
    # Where the data comes from (STATE_*), set by the scan. Empty on a
    # SwitchData nobody has judged yet — a poll's own result, a test's
    # network: `map_state` reads it from the other fields then.
    state: str = ""
    # Did it answer in this scan: True, False, or None when that is not
    # known — restored before the first scan, or a budget that ran out
    # before the first answer came. The alarms go by this, never by
    # what is drawn: a switch drawn from a reading may be silent now.
    answered: bool | None = None
    # When it stopped answering: "not answering since 14:20"
    down_since: float = 0.0

    @property
    def map_state(self) -> str:
        if self.state:
            return self.state
        if self.over_budget:
            return STATE_OVER_BUDGET
        return STATE_FRESH if self.reachable else STATE_NEVER

    @property
    def from_reading(self) -> bool:
        """Drawn from an earlier reading, not from this scan"""
        return self.map_state in READING_STATES

    def to_dict(self) -> dict:
        """The reading, for switch_readings (v0.8.7).

        Everything the map is built from. Not loop detection — the
        counters cycle refreshes it — and nothing that describes the
        current scan (state, answered, over_budget and its streak):
        those are facts about now, and a reading is a fact about then.
        """
        return {
            "format": READING_FORMAT,
            "ip": self.ip,
            "sys_name": self.sys_name,
            "sys_descr": self.sys_descr,
            "bridge_mac": self.bridge_mac,
            "own_macs": sorted(self.own_macs),
            "ports": [p.to_dict() for _, p in sorted(self.ports.items())],
            "fdb": dict(sorted(self.fdb.items())),
            "lag_members": {str(k): v for k, v in self.lag_members.items()},
            "lag_groups": {
                str(k): list(v) for k, v in self.lag_groups.items()
            },
            "port_pvid": {str(k): v for k, v in self.port_pvid.items()},
            "vlan_names": {str(k): v for k, v in self.vlan_names.items()},
            "lldp_neighbors": [n.to_dict() for n in self.lldp_neighbors],
            "lldp_forwarded": sorted(self.lldp_forwarded),
            "lldp_crowded": sorted(self.lldp_crowded),
            "port_labels": {str(k): v for k, v in self.port_labels.items()},
            "stp": self.stp.to_dict() if self.stp is not None else None,
            "sys_uptime": self.sys_uptime,
            "sys_object_id": self.sys_object_id,
            "polled_at": self.polled_at,
            "poll_seconds": self.poll_seconds,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "SwitchData":
        """A reading back from switch_readings; the caller has checked
        its format. It is a reading of a switch that answered in full,
        so `reachable` is True — what it says about now is `state`."""
        if raw.get("format") != READING_FORMAT:
            raise ValueError(f"reading format {raw.get('format')!r}")
        ports = (PortInfo.from_dict(p) for p in raw.get("ports") or [])
        stp = raw.get("stp")
        return cls(
            ip=str(raw["ip"]),
            reachable=True,
            sys_name=str(raw.get("sys_name", "")),
            sys_descr=str(raw.get("sys_descr", "")),
            bridge_mac=str(raw.get("bridge_mac", "")),
            own_macs=set(raw.get("own_macs") or ()),
            ports={p.if_index: p for p in ports},
            fdb={str(k): int(v) for k, v in (raw.get("fdb") or {}).items()},
            lag_members=_int_keys(raw.get("lag_members"), int),
            lag_groups=_int_keys(
                raw.get("lag_groups"), lambda v: [int(i) for i in v]
            ),
            port_pvid=_int_keys(raw.get("port_pvid"), int),
            vlan_names=_int_keys(raw.get("vlan_names"), str),
            lldp_neighbors=[
                lldp_mod.LldpNeighbor.from_dict(n)
                for n in raw.get("lldp_neighbors") or ()
            ],
            lldp_forwarded={int(i) for i in raw.get("lldp_forwarded") or ()},
            lldp_crowded={int(i) for i in raw.get("lldp_crowded") or ()},
            port_labels=_int_keys(raw.get("port_labels"), str),
            stp=stp_mod.StpData.from_dict(stp) if stp is not None else None,
            sys_uptime=int(raw.get("sys_uptime", 0)),
            sys_object_id=str(raw.get("sys_object_id", "")),
            polled_at=float(raw.get("polled_at", 0.0)),
            poll_seconds=float(raw.get("poll_seconds", 0.0)),
        )


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


def _name_stp_ports(data: "SwitchData") -> None:
    """Gives every dot1dStpPortTable row the port name an operator knows.

    A bridge-port missing from dot1dBasePortIfIndex used to print as
    "(unmapped)". On D-Link those are exactly the LACP members and the
    synthetic trunk port the aggregate's FDB lives on — the map already
    names both, and so should the STP table.
    """
    if data.stp is None:
        return
    members = {
        member: bridge_port
        for bridge_port, group in data.lag_groups.items()
        for member in group
    }
    for entry in data.stp.ports.values():
        if entry.if_index is not None:
            entry.name = port_display(data, entry.if_index)
            continue
        trunk = data.ports.get(-entry.bridge_port)
        if entry.bridge_port in data.lag_groups and trunk is not None:
            entry.if_index = -entry.bridge_port
            entry.name = trunk.name
            continue
        # a member of an aggregate: on these models the bridge-port
        # number is the member's ifIndex
        aggregate = members.get(entry.bridge_port)
        port = data.ports.get(entry.bridge_port)
        if aggregate is not None and port is not None and port.is_physical:
            entry.if_index = entry.bridge_port
            trunk = data.ports.get(-aggregate)
            entry.name = port.name or str(entry.bridge_port)
            if trunk is not None:
                entry.name += f" (in {trunk.name})"


def port_display(data: "SwitchData", if_index: int) -> str:
    """Port name as the operator knows it, ifIndex as the fallback."""
    port = data.ports.get(if_index)
    return port.name if port and port.name else str(if_index)


# What an inferred aggregate's port is called (v0.8.6): its members.
# The synthetic bridge-port number it used to be named by is what
# D-Link renumbers when the members flap (v0.5.1); the members are
# what the operator cabled, and what the other end calls it too.
LAG_PREFIX = "LAG "


def lag_port_name(data: "SwitchData", members) -> str:
    """'LAG 1/1+1/2' — the members by ascending ifIndex."""
    return LAG_PREFIX + "+".join(
        port_display(data, m) for m in sorted(members)
    )


def unknown_bridge_port_reason(bridge_port: int) -> str:
    """Why a MAC-table row on an unmapped bridge-port is dropped — the
    same words in the service's log and in diag --fdb."""
    return (
        f"bridge-port {bridge_port} is not in dot1dBasePortIfIndex, and it "
        f"is not an aggregate"
    )


# (switch, bridge-port) pairs already reported as unknown: one WARNING
# per pair for the life of the process, not one per poll
_warned_bridge_ports: set[tuple[str, int]] = set()


def settle_synthetic_ports(
    data: "SwitchData", unmapped: dict[int, list[str]], host: str = ""
) -> dict[int, list[str]]:
    """Keeps a synthetic port only where it is an aggregate (v0.8.6).

    Runs right after infer_lag_groups. A bridge-port missing from
    dot1dBasePortIfIndex used to become a port the moment a MAC-table
    row named it, aggregate or not — and a real access switch, whose
    mapping has no gaps at all, once answered with bridge-port
    402792706. The port it got stayed in the database as
    "-402792706", and three devices sat on it as "Offline · 3".

    The v0.6.8 rule for phantom aggregates, applied to the MAC table:
    without the signs of an aggregate, there is no synthetic port. Its
    rows are dropped and returned (bridge-port -> MACs) for the count
    and the log. The aggregates that remain are named by their members.
    """
    dropped: dict[int, list[str]] = {}
    for bridge_port, macs in unmapped.items():
        if bridge_port in data.lag_groups:
            continue
        data.ports.pop(-bridge_port, None)
        dropped[bridge_port] = sorted(macs)
    if dropped:
        data.fdb = {
            mac: if_index for mac, if_index in data.fdb.items()
            if not (if_index < 0 and -if_index in dropped)
        }
    for bridge_port, members in data.lag_groups.items():
        port = data.ports.get(-bridge_port)
        if port is not None:
            port.name = lag_port_name(data, members)
    for bridge_port, macs in sorted(dropped.items()):
        key = (host or data.ip, bridge_port)
        if key in _warned_bridge_ports:
            continue
        _warned_bridge_ports.add(key)
        log.warning(
            "%s: %d MAC-table row(s) on bridge-port %d dropped — %s "
            "(e.g. %s). Nothing is placed on a port the switch does not "
            "map; `python -m moonlan.diag --fdb %s` lists the rows",
            key[0], len(macs), bridge_port,
            unknown_bridge_port_reason(bridge_port),
            ", ".join(macs[:3]), key[0],
        )
    return dropped


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


def sane_lag_members(
    data: "SwitchData", claimed: dict[int, int], host: str = ""
) -> dict[int, int]:
    """dot3adAggPortAttachedAggID, filtered by what the switch actually has.

    An Edge-Core ES3528M answers this column with -402792706 on every
    one of its 28 ports. The old test — non-zero and not the port
    itself — passes, so all 28 ports became members of one aggregate
    with that number for an ifIndex. The ports panel showed 28 rows
    with the same ifIndex, the map grew a trunk that does not exist,
    and a lag_degraded alarm sat on it for 66 hours.

    Same rule as the counters learned in v0.6.6: a number that is not
    in the interface table is not an interface. Plus one more, because
    a plausible-looking ifIndex can be wrong too — an aggregate that
    swallowed every physical port of the switch is not an aggregate,
    it is the column being read the wrong way.
    """
    if not claimed:
        return {}
    physical = {
        i for i, port in data.ports.items() if port.is_physical and i > 0
    }
    members: dict[int, int] = {}
    unknown: dict[int, int] = {}
    for member, aggregate in claimed.items():
        if aggregate > 0 and aggregate in data.ports:
            members[member] = aggregate
        else:
            unknown[member] = aggregate
    if unknown:
        values = sorted({a for a in unknown.values()})
        log.warning(
            "%s: dot3adAggPortAttachedAggID names %d port(s) as members of "
            "aggregate(s) %s, which are not in this switch's interface "
            "table — the rows are dropped rather than drawn as a trunk "
            "nobody has",
            host or data.ip, len(unknown),
            ", ".join(str(v) for v in values[:5]),
        )
    # an aggregate that contains every physical port of the switch
    by_aggregate: dict[int, set[int]] = {}
    for member, aggregate in members.items():
        by_aggregate.setdefault(aggregate, set()).add(member)
    for aggregate, group in by_aggregate.items():
        if physical and group >= physical:
            log.warning(
                "%s: aggregate %s claims all %d physical port(s) of the "
                "switch — that is not an aggregate, and the rows are "
                "dropped", host or data.ip, aggregate, len(physical),
            )
            for member in group:
                members.pop(member, None)
    return members


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

    # State that tests building a collector without __init__ need not
    # know about: None here, a dict of the instance's own on first use
    _walk_faults: dict | None = None
    _walk_fault_warned: set | None = None
    _walk_order_noted: set | None = None
    _walk_now: dict | None = None
    _walks_done: dict | None = None
    _polling: set | None = None

    def __init__(
        self,
        community: str,
        timeout: int = 2,
        retries: int = 1,
        retries_on_break: int = 2,
        per_host: dict | None = None,
        dead_oid_strikes: int = 0,
        dead_oid_cooldown_scans: int = 30,
    ):
        self._community = CommunityData(community, mpModel=1)  # v2c
        self._timeout = timeout
        self._retries = retries
        self._engine = SnmpEngine()
        self._targets: dict[str, UdpTransportTarget] = {}
        # (host, oid) -> how the last walk of it ended. A walk that
        # fails is otherwise indistinguishable from a column of zeros,
        # which is how "no answer" reached the ports panel as a
        # confident 0.0.
        self._walk_status: dict[tuple[str, str], WalkStatus] = {}
        self._retries_on_break = retries_on_break
        # host -> config.HostSnmp for the devices that were given
        # settings of their own. Everything else uses the arguments
        # above, which are the global `snmp:` section.
        self._per_host = dict(per_host or {})
        self._communities: dict[str, CommunityData] = {}
        # (host, oid) -> the dead-OID record for it, and the scan
        # cycle counter the cooldown is measured in
        self._dead_oids: dict[tuple[str, str], DeadOid] = {}
        self._cycle = 0
        # 0 disables the rule: every OID is asked for every time
        self._dead_oid_strikes = dead_oid_strikes
        self._dead_oid_cooldown = dead_oid_cooldown_scans
        # host -> when it last answered sysName, the first request of a
        # full poll (v0.8.7). A poll that then runs out of budget has
        # had an answer, and "over budget" is not "not answering".
        self.answered_at: dict[str, float] = {}
        # (host, oid) -> a walk the agent breaks (v0.8.8), and the pairs
        # that have had their one WARNING in the life of this process
        self._walk_faults = {}
        self._walk_fault_warned = set()
        # host -> the walk running now, and the walks this poll has
        # finished (v0.8.8): where a poll's budget went
        self._walk_now = {}
        self._walks_done = {}
        # hosts inside collect() right now: only a full poll's walks
        # are timed — a counters cycle that takes the host the moment a
        # cancelled poll lets go of it must not overwrite the record
        self._polling = set()

    def _timings(self) -> tuple[dict, dict]:
        if self._walk_now is None:
            self._walk_now = {}
        if self._walks_done is None:
            self._walks_done = {}
        if self._polling is None:
            self._polling = set()
        return self._walk_now, self._walks_done

    def _poll_begins(self, host: str) -> None:
        now, done = self._timings()
        now.pop(host, None)
        done[host] = []
        self._polling.add(host)

    def poll_cut(self, host: str) -> dict:
        """Where a poll stopped, read after the budget cancelled it.

        `in_walk` true: the walk that was running — OID, rows, seconds,
        resumes. False: the cancellation fell between walks, and the
        fields are the last walk that finished (oid "" if none had).
        """
        now, done = self._timings()
        running = now.get(host)
        if running is not None and running.running:
            return {**running.report(resumes=True), "in_walk": True}
        finished = done.get(host) or []
        if finished:
            return {**finished[-1].report(resumes=True), "in_walk": False}
        return {"oid": "", "name": "", "rows": 0, "seconds": 0.0,
                "resumes": 0, "in_walk": False}

    def slowest_walks(self, host: str, limit: int = 3) -> list[dict]:
        """The longest walks of the last poll, a cut one included."""
        now, done = self._timings()
        walks = list(done.get(host) or [])
        running = now.get(host)
        if running is not None and running.running:
            walks.append(running)
        walks.sort(key=lambda w: w.took(), reverse=True)
        return [w.report() for w in walks[:limit]]

    def _faults(self) -> dict[tuple[str, str], WalkFault]:
        if self._walk_faults is None:
            self._walk_faults = {}
        return self._walk_faults

    def walk_faults(self) -> list[dict]:
        """Walks the agents break right now (v0.8.8), for /api/skipped-oids."""
        return [
            {
                "host": host, "oid": oid, "fault": record.fault,
                "rows": record.rows, "last_oid": record.last_oid,
                "error": record.error, "first_at": record.first_at,
                "last_at": record.last_at,
            }
            for (host, oid), record in sorted(self._faults().items())
        ]

    def _note_walk_fault(
        self, host: str, oid: str, status: "WalkStatus", answer: str,
        kind: str, which: str = "",
    ) -> None:
        """One WARNING per (host, OID) in the life of the process.

        The agent will answer the same next time, every scan, so the
        repeats go to DEBUG: the first one says all there is to say.
        """
        key = (host, oid)
        now = time.time()
        faults = self._faults()
        record = faults.get(key)
        if record is None:
            record = faults[key] = WalkFault(
                fault=status.fault, rows=status.rows,
                last_oid=status.last_oid, error=status.error,
                first_at=now, last_at=now,
            )
        else:
            record.fault, record.rows = status.fault, status.rows
            record.last_oid, record.error = status.last_oid, status.error
            record.last_at = now
        if self._walk_fault_warned is None:
            self._walk_fault_warned = set()
        if key in self._walk_fault_warned:
            log.debug("%s: walk of %s stopped again: %s", host, oid, status.error)
            return
        self._walk_fault_warned.add(key)
        record.warned = True
        kept = (
            "this table is left out" if not status.rows
            else f"the {status.rows} row(s) before it are kept, the tail "
                 "of the table is left out"
        )
        if status.fault == FAULT_REPEATED_OID:
            what = f"the agent answered GETNEXT with {answer} ({kind}), {which}"
        else:
            what = (
                f"{WALK_MAX_NO_SUCH} answers in a row carried {kind} "
                f"instead of a value, the last for {answer}"
            )
        log.warning(
            "%s: walk of %s stopped — %s. An agent bug (RFC 3416 §4.2.2): "
            "%s; the rest of the poll goes on",
            host, oid, what, kept,
        )

    def _note_out_of_order(
        self, host: str, oid: str, previous: tuple, answer: tuple
    ) -> None:
        """One INFO per (host, OID) in the life of the process: the agent
        hands this table out in an order of its own, and it is read on."""
        if self._walk_order_noted is None:
            self._walk_order_noted = set()
        key = (host, oid)
        if key in self._walk_order_noted:
            return
        self._walk_order_noted.add(key)
        log.info(
            "%s: the agent returns %s out of order (%s then %s); the table "
            "is read to its end anyway",
            host, oid, ".".join(str(part) for part in previous),
            ".".join(str(part) for part in answer),
        )

    def _note_walk_normal(self, host: str, oid: str) -> None:
        """A walk that used to be broken read to its end."""
        record = self._faults().pop((host, oid), None)
        if record is None:
            return
        if record.warned:
            log.info(
                "%s: walk of %s walks normally again (%d row(s))",
                host, oid, self._walk_status[(host, oid)].rows,
            )
        else:
            log.debug("%s: walk of %s walks normally again", host, oid)

    def begin_scan_cycle(self) -> None:
        """One more scan has started; the cooldown is counted in these."""
        self._cycle += 1

    def paused_oids(self) -> list[dict]:
        """(host, OID) pairs nobody is asking for right now."""
        return [
            {
                "host": host,
                "oid": oid,
                "strikes": record.strikes,
                "cycles_left": record.skip_until - self._cycle,
            }
            for (host, oid), record in sorted(self._dead_oids.items())
            if record.skip_until > self._cycle
        ]

    def _skipping(self, host: str, oid: str) -> bool:
        """Is this OID on pause — and, if the pause is over, say so.

        The resumption is logged as loudly as the pause. An operator
        looking at a gap in the data has to be able to tell "the device
        does not report this" from "MoonLan stopped asking", and that
        means both edges are in the log.
        """
        record = self._dead_oids.get((host, oid))
        if record is None or not record.skip_until:
            return False
        if record.skip_until > self._cycle:
            return True
        log.info(
            "%s: asking for %s again after %d scan(s) of silence — the "
            "firmware may have learned to answer it",
            host, oid, self._dead_oid_cooldown,
        )
        record.skip_until = 0
        record.strikes = 0
        return False

    def _note_walk_outcome(
        self, host: str, oid: str, status: "WalkStatus"
    ) -> None:
        """Count the one outcome worth giving up on.

        No rows AND a timeout: the agent does not implement the table
        and cannot say so, so every cycle spends the whole retry budget
        to learn nothing. Anything else resets the count.
        """
        key = (host, oid)
        if not (status.rows == 0 and is_timeout(status.error)):
            self._dead_oids.pop(key, None)
            return
        if not self._dead_oid_strikes:
            return
        record = self._dead_oids.setdefault(key, DeadOid())
        record.strikes += 1
        if record.strikes < self._dead_oid_strikes or record.skip_until:
            return
        record.skip_until = self._cycle + self._dead_oid_cooldown
        log.warning(
            "%s: walk of %s has returned no rows and timed out %d time(s) "
            "in a row — not asking for it again for %d scan(s). The data "
            "is missing because MoonLan stopped asking, not because the "
            "device denies having it.",
            host, oid, record.strikes, self._dead_oid_cooldown,
        )

    def _community_for(self, host: str) -> CommunityData:
        """The community string this host answers to.

        A parc assembled over years is not one community string. The
        object is cached because pysnmp derives keys from it.
        """
        settings = self._per_host.get(host)
        if settings is None or settings.community == str(
            self._community.communityName
        ):
            return self._community
        cached = self._communities.get(host)
        if cached is None:
            cached = CommunityData(settings.community, mpModel=1)
            self._communities[host] = cached
        return cached

    def _breaks_for(self, host: str) -> int:
        settings = self._per_host.get(host)
        return (
            settings.retries_on_break if settings is not None
            else self._retries_on_break
        )

    async def _target(self, host: str) -> UdpTransportTarget:
        target = self._targets.get(host)
        if target is None:
            settings = self._per_host.get(host)
            timeout = settings.timeout if settings else self._timeout
            retries = settings.retries if settings else self._retries
            target = await UdpTransportTarget.create(
                (host, 161), timeout=timeout, retries=retries
            )
            self._targets[host] = target
        return target

    async def _get(self, host: str, oid: str):
        """GET of a single value; None on any failure.

        Including a failure to BUILD the request. pyasn1 rejects a
        malformed OID while the request is being assembled, before
        anything is sent, so `error_ind` never gets the chance to
        report it — the exception simply leaves through the caller. A
        single bad OID once took the whole counters cycle down with it,
        for every switch in the network. Whatever the reason, one
        unreadable value is a thing to log, not a thing to stop for.
        """
        try:
            error_ind, error_status, _, var_binds = await get_cmd(
                self._engine,
                self._community_for(host),
                await self._target(host),
                ContextData(),
                ObjectType(ObjectIdentity(oid)),
            )
        except Exception as exc:  # pyasn1 included: a bad OID is data
            log.warning("%s: GET of %s failed (%s)", host, oid, exc)
            return None
        if error_ind or error_status:
            log.debug("%s GET %s: %s", host, oid, error_ind or error_status)
            return None
        value = var_binds[0][1]
        if is_no_such(value):
            # An SNMPv2c agent answers "I do not implement that" with a
            # perfectly successful PDU carrying noSuchObject /
            # noSuchInstance / endOfMibView. Nothing above catches it,
            # and all three derive from OctetString, so the caller gets
            # what looks like an empty string. "Not implemented" has to
            # come back the same way "no reply" does, or every probe
            # for a private branch succeeds on every device alive.
            log.debug("%s GET %s: %s", host, oid, type(value).__name__)
            return None
        return value

    async def _open_walk(self, host: str, start: str):
        """The pysnmp walk generator, starting just after `start`.

        A seam of its own because `_walk` opens one of these per
        resume, and because a test needs somewhere to stand.
        """
        return walk_cmd(
            self._engine,
            self._community_for(host),
            await self._target(host),
            ContextData(),
            ObjectType(ObjectIdentity(start)),
            # Resuming means starting at a leaf OID, and a walk bounded
            # to that leaf's own subtree would stop at once. The
            # subtree boundary is checked in _walk instead.
            lexicographicMode=True,
        )

    async def _walk(self, host: str, oid: str):
        """WALK of a subtree; yields (OID suffix, value) pairs.

        A walk that stops answering halfway is picked back up from the
        last OID that did arrive, up to `retries_on_break` times. This
        is not the same as the SNMP retry count: a retry re-sends one
        request, while this re-opens the walk after the agent has
        already given up mid-table.

        It matters because the loss is not random. The agent answers
        the first rows and runs out of breath, so the ports that
        disappear are the ones at the end of ifTable — on sw1 that was
        1/25…1/28, the gigabit uplinks, in three polls running. Raising
        the timeout cured sw0 and sw2 and did not cure sw1: a switch
        with 36 interfaces needs the table read in more than one bite.

        Resuming forces lexicographicMode=True — a walk restarted at a
        leaf OID would otherwise decide it had left its own subtree
        immediately — so the subtree boundary is checked here instead.

        An answer that repeats — the OID asked for, or an OID this walk
        has already had, from any pass and the no-such rows included —
        ends the walk, and so does a run of WALK_MAX_NO_SUCH no-such
        rows (v0.8.8, see FAULT_REPEATED_OID). Such a walk is not
        resumed: the agent would answer the same again. An answer that
        goes back to an OID not seen yet is the agent's order, counted
        in `out_of_order` and read on. The repeat is checked first, the
        subtree boundary after it.
        """
        base = tuple(int(x) for x in oid.split("."))
        status = WalkStatus(oid=oid)
        self._walk_status[(host, oid)] = status
        if self._skipping(host, oid):
            # Not sent, and not reported as an empty table either: the
            # caller sees the same "no answer" it would see from a walk
            # that failed, which is exactly what this is.
            status.error = SKIPPED_ERROR
            return
        walk_now, walks_done = self._timings()
        timed = host in self._polling
        timing = WalkTiming(status=status, started=time.monotonic())
        if timed:
            walk_now[host] = timing
        cut = False
        rows = self._walk_rows(host, oid, base, status)
        try:
            async for row in rows:
                yield row
        except asyncio.CancelledError:
            # the poll budget ran out here: the record stays "running",
            # and that is what the server reads to say where it went
            cut = True
            raise
        finally:
            # a consumer that stops reading closes the pysnmp walk now,
            # as it did before the body moved into _walk_rows
            await rows.aclose()
            if timed and not cut:
                timing.seconds = time.monotonic() - timing.started
                walks_done.setdefault(host, []).append(timing)

    async def _walk_rows(self, host: str, oid: str, base: tuple, status):
        """The body of _walk, apart so that its clock wraps every exit."""
        start = oid
        last_oid: tuple[int, ...] | None = None
        no_such_run = 0
        # every OID this walk has had, across its passes: a repeat is a
        # loop. One tuple per row; for a MAC table of tens of thousands
        # of rows that is acceptable.
        seen: set[tuple[int, ...]] = set()

        while True:
            broke = ""
            fault = ""
            answer: tuple[int, ...] = ()
            kind = ""
            which = ""
            asked = tuple(int(x) for x in start.split("."))
            try:
                objects = await self._open_walk(host, start)
            except Exception as exc:
                # the request could not even be built — a malformed OID
                # is a diagnosis, not a reason to stop collecting
                status.error = str(exc)
                log.warning(
                    "%s: walk of %s could not be started (%s)",
                    host, start, exc,
                )
                self._note_walk_outcome(host, oid, status)
                return
            left_subtree = False
            try:
                async for error_ind, error_status, _, var_binds in objects:
                    if error_ind or error_status:
                        broke = str(error_ind or error_status)
                        break
                    for name, value in var_binds:
                        full = tuple(name)
                        if full == asked or full in seen:
                            # a repeat: from here the agent would hand
                            # out the same answers again, forever
                            fault, answer = FAULT_REPEATED_OID, full
                            kind = type(value).__name__
                            which = (
                                "the OID asked for" if full == asked
                                else "which it had already returned in "
                                     "this walk"
                            )
                            break
                        if full[:len(base)] != base:
                            left_subtree = True  # the table is finished
                            break
                        if full < asked:
                            # back to an OID not seen yet: its order,
                            # not a loop — read on
                            status.out_of_order += 1
                            self._note_out_of_order(host, oid, asked, full)
                        seen.add(full)
                        asked = last_oid = full
                        if is_no_such(value):
                            # same sentinel as in _get: a row that says
                            # "nothing here" is not a row
                            no_such_run += 1
                            if no_such_run >= WALK_MAX_NO_SUCH:
                                fault, answer = FAULT_NO_SUCH_RUN, full
                                kind = type(value).__name__
                                break
                            continue
                        no_such_run = 0
                        status.rows += 1
                        yield full[len(base):], value
                    if left_subtree or fault:
                        break
            except GeneratorExit:
                raise  # the consumer stopped reading; not our business
            except Exception as exc:
                broke = str(exc)
                log.warning("%s: walk of %s raised (%s)", host, oid, exc)
            finally:
                await objects.aclose()
            if fault:
                answer_text = ".".join(str(part) for part in answer)
                status.fault = fault
                status.truncated = status.rows > 0
                status.last_oid = (
                    ".".join(str(part) for part in last_oid) if last_oid else ""
                )
                status.error = (
                    f"the agent answered GETNEXT with {answer_text} ({kind}), "
                    f"{which}"
                    if fault == FAULT_REPEATED_OID else
                    f"{WALK_MAX_NO_SUCH} answers in a row carried {kind}, "
                    f"the last for {answer_text}"
                )
                self._note_walk_fault(
                    host, oid, status, answer_text, kind, which
                )
                # not a dead-OID strike: the loop now costs one request
                self._note_walk_outcome(host, oid, status)
                return
            if left_subtree or not broke:
                self._note_walk_outcome(host, oid, status)
                self._note_walk_normal(host, oid)
                return  # read to the end
            status.error = broke
            if status.resumes >= self._breaks_for(host) or last_oid is None:
                status.truncated = status.rows > 0
                status.last_oid = (
                    ".".join(str(part) for part in last_oid) if last_oid else ""
                )
                log.warning(
                    "%s: walk of %s stopped after %d row(s) (%s)%s — what "
                    "it would have returned past that point is unknown, "
                    "not zero",
                    host, oid, status.rows, broke,
                    f", last OID {status.last_oid}" if status.last_oid else "",
                )
                self._note_walk_outcome(host, oid, status)
                return
            status.resumes += 1
            # an agent that has run out of breath needs a moment before
            # it can answer again
            await asyncio.sleep(RESUME_PAUSE)
            start = ".".join(str(part) for part in last_oid)

    def last_walk_status(self, host: str, oid: str) -> "WalkStatus":
        """How the most recent walk of this OID ended.

        Three outcomes the caller has to tell apart: read to the end,
        stopped partway (rows arrived, the tail did not), and no answer
        at all. Before v0.6.5 the last two were the same thing, so a
        column that returned 24 of 28 ports was reported as "NO ANSWER"
        — hiding the one fact worth having.
        """
        return self._walk_status.get((host, oid)) or WalkStatus(oid=oid)

    def last_walk_error(self, host: str, oid: str) -> str:
        """The error the most recent walk of this OID ended with, if any."""
        return self.last_walk_status(host, oid).error

    async def collect(self, host: str) -> SwitchData:
        """Full poll of a single switch, its walks timed (v0.8.8)."""
        self._poll_begins(host)
        try:
            return await self._collect(host)
        finally:
            self._polling.discard(host)

    async def _collect(self, host: str) -> SwitchData:
        data = SwitchData(ip=host, polled_at=time.time())

        sys_name = await self._get(host, OID_SYS_NAME)
        if sys_name is None:
            # "Does not respond to SNMP" was the whole message, and it
            # suggested nothing. The two causes need different people
            # doing different things, and one probe tells them apart.
            _verdict, why = await diagnose_silence(
                self, host, OID_SYS_NAME
            )
            log.warning(
                "Switch %s does not respond to SNMP: %s", host, why
            )
            return data

        data.reachable = True
        self.answered_at[host] = time.time()
        data.sys_name = str(sys_name)
        sys_descr = await self._get(host, OID_SYS_DESCR)
        data.sys_descr = str(sys_descr) if sys_descr is not None else ""
        sys_object_id = await self._get(host, OID_SYS_OBJECT_ID)
        data.sys_object_id = (
            str(sys_object_id) if sys_object_id is not None else ""
        )

        bridge_mac = await self._get(host, OID_BRIDGE_ADDRESS)
        if bridge_mac is not None:
            data.bridge_mac = _fmt_mac(as_octets(bridge_mac))
            data.own_macs.add(data.bridge_mac)

        # Interface MACs: real frames leave the switch with these source
        # addresses, not with the bridge base MAC. They are also what
        # some agents put in lldpLocPortId, so they are kept per port.
        phys_addr: dict[int, str] = {}
        async for suffix, value in self._walk(host, OID_IF_PHYS_ADDRESS):
            raw = as_octets(value)
            if len(raw) == 6 and any(raw):
                mac = _fmt_mac(raw)
                data.own_macs.add(mac)
                phys_addr[suffix[0]] = mac

        # Interfaces. Port name comes from ifName; ifDescr is only a
        # fallback: D-Link puts the whole model and firmware into ifDescr.
        if_descr: dict[int, str] = {}
        async for suffix, value in self._walk(host, OID_IF_DESCR):
            if_index = suffix[0]
            if_descr[if_index] = str(value)
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
        claimed: dict[int, int] = {}
        async for suffix, value in self._walk(host, OID_LAG_ATTACHED_ID):
            try:
                member, aggregate = suffix[0], int(value)
            except (TypeError, ValueError):
                continue
            if aggregate and aggregate != member:
                claimed[member] = aggregate
        data.lag_members = sane_lag_members(data, claimed, host)

        # bridge-port -> ifIndex
        port_to_ifindex: dict[int, int] = {}
        async for suffix, value in self._walk(host, OID_PORT_IFINDEX):
            port_to_ifindex[suffix[0]] = int(value)

        # Spanning tree, read with the "disabled STP still answers"
        # trap in mind (see stp.py)
        # own_macs goes in: "did this bridge accept somebody else's
        # root" is a comparison against every address it answers to
        data.stp = await stp_mod.collect_stp(
            self, host, port_to_ifindex, data.own_macs,
            {i: p.oper_up for i, p in data.ports.items()},
        )
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
        # how LACP trunks look on some D-Link models) get a synthetic port
        # with ifIndex = -bridge_port for now; the ones that do not turn
        # out to be an aggregate are dropped below (v0.8.6).
        unmapped: dict[int, list[str]] = {}
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
                    unmapped.setdefault(bridge_port, []).append(mac)
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
                    f"port {p}: {len(macs)} MACs"
                    for p, macs in sorted(unmapped.items())
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
        # …and a synthetic port that is not one of them is no port
        unknown = sum(
            len(macs)
            for macs in settle_synthetic_ports(data, unmapped, host).values()
        )
        rejected = bad_suffix + bad_mac + unknown
        log.log(
            logging.INFO if rejected else logging.DEBUG,
            "%s FDB: %d entries rejected (bad suffix: %d, bad MAC: %d, "
            "unknown bridge-port: %d)",
            host, rejected, bad_suffix, bad_mac, unknown,
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

        _name_stp_ports(data)

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
            port_to_ifindex,
        )
        # lldpLocPortDesc is an administrative port name on some agents
        # and a copy of ifDescr on others; only the former is worth
        # showing, and the latter is wide enough to break the layout
        data.port_labels, dropped = lldp_mod.useful_port_labels(
            data.port_labels,
            {i: p.name for i, p in data.ports.items()},
            if_descr,
        )
        if dropped:
            log.debug(
                "%s: %d LLDP port label(s) dropped — they repeat ifName / "
                "ifDescr or are one firmware template with the port "
                "number substituted", host, dropped,
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
            raw = as_octets(value)
            if len(raw) != 6:
                continue
            ip = ".".join(str(octet) for octet in suffix[-4:])
            arp[_fmt_mac(raw)] = ip
        log.info("ARP from %s: %d entries", host, len(arp))
        return arp

    async def collect_router_identity(self, host: str) -> RouterIdentity:
        """What a router of `routers:` says about itself (v0.8.11).

        Its MACs (ifPhysAddress), what to call them (ifName; ifDescr for
        an interface whose ifName is empty), the kind of each interface
        (ifType) and the interface each of its addresses is on
        (ipAdEntIfIndex: the index is the IP, the value its ifIndex).
        Every table through _walk, with all it guards against. No MACs
        — an empty identity, and the next three walks are not sent.
        """
        phys: dict[int, str] = {}
        async for suffix, value in self._walk(host, OID_IF_PHYS_ADDRESS):
            raw = as_octets(value)
            if len(raw) == 6 and any(raw):
                phys[suffix[0]] = _fmt_mac(raw)
        if not phys:
            return RouterIdentity(address=host)
        names: dict[int, str] = {}
        async for suffix, value in self._walk(host, OID_IF_NAME):
            name = str(value).strip()
            if name:
                names[suffix[0]] = name
        if any(if_index not in names for if_index in phys):
            async for suffix, value in self._walk(host, OID_IF_DESCR):
                name = str(value).strip()
                if name and suffix[0] in phys and suffix[0] not in names:
                    names[suffix[0]] = name
        types: dict[int, int] = {}
        async for suffix, value in self._walk(host, OID_IF_TYPE):
            try:
                types[suffix[0]] = int(value)
            except (TypeError, ValueError):
                continue
        ip_index: dict[str, int] = {}
        async for suffix, value in self._walk(host, OID_IP_AD_ENT_IF_INDEX):
            if len(suffix) != 4:
                continue
            try:
                ip_index[".".join(str(octet) for octet in suffix)] = int(value)
            except (TypeError, ValueError):
                continue
        return identity_from_tables(host, phys, names, types, ip_index)
