"""Demo mode: a virtual network to explore MoonLan without switches.

A star of five switches that reproduces the real-world scenario the
tree algorithm was built for:

- the core sees every ray, each on its own port (one of them on a
  synthetic bridge-port — a D-Link-style LAG trunk missing from
  dot1dBasePortIfIndex);
- the rays do NOT see the core at all (permanent one-way visibility);
- rays see stray interface MACs of other rays through the core — the
  branch invariant must prevent false ray-to-ray links;
- a 2×1G LACP aggregate (IEEE8023-LAG-MIB) between the core and the
  first ray;
- an unmanaged switch with five hosts behind one port of the second ray;
- port PVIDs and VLAN names.

v0.5 scenarios (the demo doubles as the regression suite):
- smooth random traffic curves on every active port;
- one port with a growing error rate -> port_errors alarm after ~2
  counter cycles;
- one host that never answers ping and one that recovers after a few
  cycles -> host_down raise and clear;
- two latecomer hosts that appear from the second scan on -> new_mac
  alarms (the first scan is the initial inventory and stays silent).

v0.6 scenarios:
- the fourth ray is invisible in the core's MAC table and is placed by
  LLDP alone -> a link with source "lldp", which is the whole point of
  reading LLDP at all;
- a named MikroTik bridge behind the access port that used to carry an
  anonymous pseudo-switch -> unmanaged_bridge_detected, and the five
  devices behind it hang off the named node;
- spanning tree: the core is the root, one ray holds its uplink in
  blocking, and the fourth ray has STP switched off while still
  reporting priority 0 / cost 0 / itself as root — the trap the STP
  verdict exists to catch;
- one port that changes link state on every counters cycle ->
  port_flapping.

v0.5.8 scenarios:
- a port whose cable starts failing on the second scan: the switch
  learns five distorted copies of the address of the device behind it
  -> port_frame_corruption, and the copies never reach the map;
- a device visible only through the trunks, placed approximately.

v0.5.3 scenarios:
- hosts last seen hours ago: on the map, greyed out, no alarms — one
  alone on its port, two on another and six on a third (both gathered
  under "Offline · N" nodes), and two more sharing the pseudo-switch
  port with live hosts;
- two devices known only from ARP, in a subnet no switch port shows —
  the "not on map" inventory;
- one host whose IP no ARP table confirms, and one device with a
  randomized (locally administered) MAC;
- the mass-outage port carries five distinct IPs, so the alarm counts
  five devices rather than five host records.
"""

from __future__ import annotations

import logging
import random
import time

from .counters import ColumnStatus, Sample
from .db import Database
from .lldp import (
    LldpNeighbor,
    analyse_ports,
    merge_rows,
    useful_port_labels,
)
from .stp import StpData, StpPort
from .snmp_collector import (
    PortInfo,
    SwitchData,
    aggregate_port,
    infer_lag_groups,
    parse_fdb_entry,
)

# The RNG is re-seeded inside demo_network so every scan rebuilds the
# same base network (stable MACs -> no phantom new_mac events)
log = logging.getLogger(__name__)

_rng = random.Random(7)

VLAN_NAMES = {1: "default", 8: "office", 11: "ipmi"}
LAG_IFINDEX = 1000  # ifIndex of the logical port Po1

# Latecomer MACs sort after the base 00:4d:4c hosts so the positional
# IP/name assignment in enrich_db is not reshuffled by their arrival
# The second one is a phone with a randomized (locally administered)
# MAC — the kind that leaves a new "device" behind on every visit
LATECOMERS = {"00:ee:00:00:00:01": 9, "b2:1a:7c:44:55:66": 10}  # mac -> ray1 port

# A port with a failing cable. Two real cameras live behind it; from
# the second scan on, the switch also learns distorted copies of the
# first one's address out of the damaged frames — different ones every
# poll, which is exactly how it looks on real hardware. The second
# camera is 12 bits away from the first: far enough that the 8-bit rule
# leaves it alone, which is the point of having a threshold.
CORRUPT_PORT = 6            # Gi0/6 of access-sw-2
CORRUPT_REAL = "20:7b:d5:1a:31:8d"
CORRUPT_IP = "10.0.99.57"
CORRUPT_NEIGHBOR = "20:77:b5:7c:37:87"
CORRUPT_NEIGHBOR_IP = "10.0.99.58"
CORRUPT_PER_SCAN = 3        # invented addresses per poll

# The MikroTik bridge behind access-sw-2 Gi0/5 — the port that already
# carries five devices. Before v0.6 that port could only show an
# anonymous "switch without SNMP"; LLDP gives it a name, a model and a
# management address, and raises unmanaged_bridge_detected.
BRIDGE_PORT = 5
BRIDGE_CHASSIS = "18:fd:74:fd:b5:cf"
BRIDGE_NAME = "RouterOS-Sport"
BRIDGE_DESC = "RouterOS RB941-2nD 6.49.19"
BRIDGE_MGMT_IP = "10.3.5.6"

# The chain behind access-sw-1 Gi0/14: an unmanaged box on the cable
# with TWO MikroTik bridges behind it, plus four ordinary devices.
# This is mb1 port 28 (Garage -> Workshop -> 10.3.6.124) and the shape
# that crashed build_topology in v0.6.0: one port, one pseudo-switch
# and several bridges.
CHAIN_PORT = 14
CHAIN_HOSTS = 4
CHAIN_BRIDGES = (
    ("18:fd:74:fd:e3:7f", "RouterOS-Garage", "RouterOS RB941-2nD 6.49.19",
     "10.3.6.4"),
    ("18:fd:74:fd:b3:af", "RouterOS-Workshop", "RouterOS hAP lite 6.49.10",
     "10.3.6.2"),
)

# Ten IP cameras behind an unmanaged switch on access-sw-3 Gi0/9.
# They speak LLDP but send no optional TLVs — a bare chassis MAC, no
# name, no capabilities. This is mb2 1/6 and mb4 1/2 on the real
# network, where v0.6.0 would have drawn ten bridge nodes and fired ten
# alarms. They must produce neither: the LLDP data belongs on each
# camera's own host card.
CAMERA_PORT = 9
CAMERAS = [f"18:c0:4d:00:0a:{n:02x}" for n in range(1, 11)]
CAMERA_DESC = "IPC-B140 v2.800.0000000.16.R"

# The MikroTik router on core-sw Gi0/21. It fills lldpRemTable with
# one row per VLAN interface, each announcing its own management
# address — 38 rows for one device on one cable. The card must show one
# device and all of its addresses.
ROUTER_PORT = 21
ROUTER_CHASSIS = "00:0e:04:b7:79:ab"
ROUTER_IPS = ["10.0.0.1", "10.0.1.1"] + [f"10.3.{n}.1" for n in range(1, 9)]

# An edge router behind core-sw Gi0/22: it announces `router` and a
# name over LLDP, but no ARP table of ours gives it an address and
# reverse DNS has never heard of it. Before v0.6.2 that made it a pale
# dot labelled with its MAC, while LLDP had been calling it by name
# the whole time.
EDGE_ROUTER_PORT = 22
EDGE_ROUTER_CHASSIS = "00:0e:04:aa:bb:cc"
EDGE_ROUTER_IPS = ["10.9.0.1", "10.9.1.1"]

# The provider handover on core-sw Gi0/26. Their switch announces
# itself over LLDP and its MAC is in our table on the same port, so it
# must come out as ONE node; the addresses behind it are not ours to
# draw one by one, and it must raise no "someone plugged a switch in"
# alarm — it is the edge of the network.
UPLINK_PORT = 26
UPLINK_CHASSIS = "10:c1:72:bd:44:e1"
UPLINK_NAME = "CE6851-48S6Q-HI"
UPLINK_MGMT_IP = "172.16.0.3"
# Addresses past the handover, from the documentation range of
# RFC 5737. The point of the "External network" node is that what is
# behind it is somebody else's, and until v0.6.3 the demo filled it
# with pc-NN.demo.lan on 10.0.99.x — demonstrating the opposite.
UPLINK_HOSTS = 4
UPLINK_HOST_MACS = [f"00:1b:21:e0:00:{n:02x}" for n in range(1, UPLINK_HOSTS + 1)]
UPLINK_HOST_IPS = [f"203.0.113.{10 + n}" for n in range(UPLINK_HOSTS)]
UPLINK_PORTS = {("10.0.0.10", f"Gi0/{UPLINK_PORT}")}

# What a `routers:` entry would resolve to through ARP: the address an
# operator actually reaches the box at, out of the many it announces
ROUTER_ADDRESSES = {ROUTER_CHASSIS: ROUTER_IPS[0]}

# What a `routers:` section would hold for this network. The port to
# this device must never be read as a way OUT of the network — it is
# the middle of it.
ROUTERS = [ROUTER_IPS[0]]

# access-sw-4 stands in for the HPE 1820: its agent fills
# lldpRemSysCapEnabled for nobody at all. A neighbour there that
# announces a name and a management address is taken for a bridge on
# that evidence alone — worth recording, not worth waking anyone, so
# its alarm is info rather than warning.
ASSUMED_PORT = 7
ASSUMED_CHASSIS = "18:fd:74:fd:b5:c5"
ASSUMED_NAME = "RouterOS-Old_building"
ASSUMED_MGMT_IP = "10.3.7.15"

# A port whose link goes up and down on every counters cycle ->
# port_flapping. Modelled on 2b0 port 21, which managed four cycles in
# thirty seconds.
FLAP_SWITCH = "10.0.0.22"
FLAP_PORT = 7

# A device behind an unmanaged switch on the core—access-sw-3 trunk: no
# switch has it on a host port, both ends of the trunk see it. Before
# v0.5.8 it fell off the map into "not on map" although it answers ping.
TRUNK_ONLY_MAC = "74:56:3c:9a:97:9f"
TRUNK_ONLY_IP = "10.0.99.51"

# The same situation, except that this one HAS been seen on a real port
# before and the inventory remembers it. It has aged out of its own
# switch's table while the core still carries it on the trunk — which
# is what happens to a quiet device — and it must stay on the port it
# lives on instead of migrating to the core's trunk and taking the
# offline group with it.
REMEMBERED_MAC = "74:56:3c:9a:97:a0"
REMEMBERED_IP = "10.0.99.52"
REMEMBERED_SWITCH = "10.0.0.23"   # access-sw-3
REMEMBERED_PORT = "Gi0/11"


def _corrupt_copies(scan: int) -> list[str]:
    """The distorted copies this poll's damaged frames produced."""
    octets = [int(part, 16) for part in CORRUPT_REAL.split(":")]
    copies = []
    for n in range(CORRUPT_PER_SCAN):
        bit = (scan * CORRUPT_PER_SCAN + n) % 32  # never the first octet
        flipped = list(octets)
        flipped[2 + bit // 8] ^= 1 << (bit % 8)
        copies.append(":".join(f"{o:02x}" for o in flipped))
    return copies


def _rand_mac(prefix: str = "00:4d:4c") -> str:
    return prefix + ":" + ":".join(f"{_rng.randint(0, 255):02x}" for _ in range(3))


def _switch(ip: str, name: str, mac_octet: int) -> SwitchData:
    sw = SwitchData(
        ip=ip, reachable=True, sys_name=name,
        sys_descr="MoonLan demo switch, 26 ports",
        bridge_mac=f"02:4d:4c:00:00:{mac_octet:02x}",
    )
    # Like real switches: besides the bridge MAC there is an interface
    # MAC, and that is what neighbors see in their FDB tables
    sw.own_macs = {sw.bridge_mac, f"02:4d:4c:00:01:{mac_octet:02x}"}
    for i in range(1, 27):
        sw.ports[i] = PortInfo(if_index=i, name=f"Gi0/{i}", oper_up=False, speed_mbps=1000)
    sw.vlan_names = dict(VLAN_NAMES)
    return sw


def _iface_mac(sw: SwitchData) -> str:
    return next(m for m in sw.own_macs if m != sw.bridge_mac)


def _synthetic_trunk(sw: SwitchData, bridge_port: int, members: tuple[int, ...]) -> int:
    """A D-Link-style trunk: the member bridge-ports are missing from
    dot1dBasePortIfIndex and the FDB lives on a synthetic bridge-port.
    The members are derived with the same inference the collector uses.
    """
    if_index = -bridge_port
    sw.ports[if_index] = PortInfo(
        if_index=if_index, name=f"bridge-port {bridge_port}", is_physical=False
    )
    physical = {p.if_index for p in sw.ports.values() if p.is_physical}
    sw.lag_groups.update(
        infer_lag_groups(physical, physical - set(members), {bridge_port})
    )
    for m in members:
        sw.ports[m].oper_up = True
    return if_index


_scan_count = 0


def demo_network() -> list[SwitchData]:
    """A star: core + four rays, LACP, a LAG trunk, a pseudo-switch, VLANs."""
    global _scan_count
    _scan_count += 1
    _rng.seed(7)  # identical base network on every scan
    core = _switch("10.0.0.10", "core-sw", 1)
    ray1 = _switch("10.0.0.21", "access-sw-1", 2)
    ray2 = _switch("10.0.0.22", "access-sw-2", 3)
    ray3 = _switch("10.0.0.23", "access-sw-3", 4)
    ray4 = _switch("10.0.0.24", "access-sw-4", 5)
    switches = [core, ray1, ray2, ray3, ray4]

    # A 2×1G LACP aggregate (IEEE8023-LAG-MIB) between the core
    # (ports 1 and 25) and the first ray (ports 25 and 26)
    for sw, members in ((core, (1, 25)), (ray1, (25, 26))):
        sw.ports[LAG_IFINDEX] = PortInfo(
            if_index=LAG_IFINDEX, name="Po1", oper_up=True, speed_mbps=2000,
            is_physical=False,
        )
        sw.lag_members = {m: LAG_IFINDEX for m in members}
        for m in members:
            sw.ports[m].oper_up = True

    # The core sees every ray on its own port. The trunk to ray3 lives
    # on a synthetic bridge-port on BOTH sides — the way D-Link exposes
    # LAG trunks without IEEE8023-LAG-MIB: member ports 3+4 (core) and
    # 23+24 (ray3) are missing from dot1dBasePortIfIndex, so their
    # composition and 2×1G speed are inferred. The rays do not see the core.
    core_trunk_to_ray3 = _synthetic_trunk(core, 3, (3, 4))
    ray3_trunk = _synthetic_trunk(ray3, 24, (23, 24))
    core_port_to_ray = {
        ray1.ip: 1,               # LACP member -> normalized to Po1
        ray2.ip: 2,
        ray3.ip: core_trunk_to_ray3,
        ray4.ip: 5,
    }
    for ray in (ray2, ray4):
        core.ports[core_port_to_ray[ray.ip]].oper_up = True
        ray.ports[24].oper_up = True
    for ray in (ray1, ray2, ray3):
        core.fdb[ray.bridge_mac] = core_port_to_ray[ray.ip]
    # ray4's own MAC has aged out of the core's table and ray4 sees
    # nobody, so the MAC tables cannot place it at all. LLDP can, and
    # that link comes out with source "lldp" — the case the whole
    # feature exists for.

    # Stray MACs of other rays leak through the core onto the rays'
    # uplinks: they reveal each ray's uplink port, and the branch
    # invariant must keep them from becoming false ray-to-ray links.
    ray1.fdb[_iface_mac(ray3)] = 25          # LACP member -> Po1
    ray2.fdb[_iface_mac(ray4)] = 24
    ray3.fdb[_iface_mac(ray2)] = ray3_trunk  # synthetic uplink
    # ray4 sees nobody at all -> its link port stays "?"

    def connect_host(
        sw: SwitchData, port: int, vlan: int, mac: str = ""
    ) -> str:
        mac = mac or _rand_mac()
        sw.ports[port].oper_up = True
        sw.fdb[mac] = port
        sw.port_pvid[port] = vlan
        if sw is not core:  # the core sees ray hosts through its trunks
            core.fdb[mac] = core_port_to_ray[sw.ip]
        return mac

    # Hosts: the first ray is an office, the second is an office plus a
    # pseudo-switch, the third is mixed, the core hosts servers in VLAN 11
    for port in range(1, 9):
        connect_host(ray1, port, 1 if port <= 4 else 8)
    for port in range(1, 5):
        connect_host(ray2, port, 8)
    for _ in range(5):  # 5 hosts on one port — an unmanaged switch
        connect_host(ray2, 5, 8)
    for port in range(1, 7):
        connect_host(ray3, port, 1 if port % 2 else 8)
    for port in range(1, 4):
        connect_host(ray4, port, 1)
    for port in (12, 13, 14):
        connect_host(core, port, 11)

    # A real device on access-sw-2 Gi0/6 whose cable starts failing from
    # the second scan on: the switch then learns copies of its address
    # out of the damaged frames. On a real port the copies differ every
    # time; fixed ones make the demo a repeatable regression case.
    ray2.ports[CORRUPT_PORT].oper_up = True
    ray2.port_pvid[CORRUPT_PORT] = 8
    macs = [CORRUPT_REAL, CORRUPT_NEIGHBOR]
    if _scan_count >= 2:
        macs += _corrupt_copies(_scan_count)
    for mac in macs:
        ray2.fdb[mac] = CORRUPT_PORT
        core.fdb[mac] = core_port_to_ray[ray2.ip]

    # Seen on the core's trunk to ray3 and on ray3's own uplink, on no
    # host port anywhere: it is placed on the trunk with the better
    # claim — the core's downlink — and marked approximate
    core.fdb[TRUNK_ONLY_MAC] = core_trunk_to_ray3
    ray3.fdb[TRUNK_ONLY_MAC] = ray3_trunk
    # …and one the inventory can place properly
    core.fdb[REMEMBERED_MAC] = core_trunk_to_ray3
    ray3.fdb[REMEMBERED_MAC] = ray3_trunk

    # Four devices behind the unmanaged box on access-sw-1 Gi0/14, with
    # two bridges of their own further down the chain
    for _ in range(CHAIN_HOSTS):
        connect_host(ray1, CHAIN_PORT, 8)
    # Ten cameras behind an unmanaged switch, each of them talking LLDP
    for mac in CAMERAS:
        connect_host(ray3, CAMERA_PORT, 8, mac)
    # The router forwards frames, so its chassis MAC is in the core's
    # table on the very port LLDP found it on. It is one device and
    # must be one node — v0.6.1 drew it twice, once named and once as
    # a bare MAC beside itself.
    connect_host(core, ROUTER_PORT, 1, ROUTER_CHASSIS)
    # …and an edge router that gets no address from us at all
    connect_host(core, EDGE_ROUTER_PORT, 1, EDGE_ROUTER_CHASSIS)
    # The provider handover: their switch plus a handful of addresses
    # that live past it
    connect_host(core, UPLINK_PORT, 1, UPLINK_CHASSIS)
    for mac in UPLINK_HOST_MACS:
        connect_host(core, UPLINK_PORT, 1, mac)

    _add_lldp(core, ray1, ray2, ray3, ray4, core_port_to_ray)
    _add_stp(core, ray1, ray2, ray3, ray4)

    if _scan_count == 1:
        _report_rejected_fdb(ray4)

    # Latecomers appear from the second scan on -> new_mac alarms
    if _scan_count >= 2:
        for mac, port in LATECOMERS.items():
            ray1.ports[port].oper_up = True
            ray1.fdb[mac] = port
            ray1.port_pvid[port] = 8
            core.fdb[mac] = core_port_to_ray[ray1.ip]

    return switches


def _neighbor(
    if_index: int, chassis_id: str, port_id: str, *,
    sys_name: str = "", sys_desc: str = "", caps: set[str] | None = None,
    mgmt_ip: str = "", mgmt_ips: list[str] | None = None,
    cap_known: bool = True, matched_by: str = "loc_id",
) -> LldpNeighbor:
    addresses = mgmt_ips if mgmt_ips is not None else (
        [mgmt_ip] if mgmt_ip else []
    )
    return LldpNeighbor(
        local_ifindex=if_index, local_port_num=if_index,
        port_matched_by=matched_by,
        chassis_id=chassis_id, chassis_subtype=4,
        port_id=port_id, port_subtype=5,
        sys_name=sys_name, sys_desc=sys_desc,
        cap_enabled=caps or set(), cap_known=cap_known,
        mgmt_ip=addresses[0] if addresses else "",
        mgmt_ips=list(addresses),
    )


def _add_lldp(core, ray1, ray2, ray3, ray4, core_port_to_ray) -> None:
    """LLDP as the four rays would report it.

    Four situations at once, all taken from the real network:
    the core and ray2 name each other (a link the MAC tables also
    infer -> source "both"); the core and ray4 name each other where
    the MAC tables cannot help at all (-> source "lldp"); ray2 has an
    unmanaged MikroTik bridge behind an access port; ray1 forwards
    foreign LLDP frames, so two neighbours appear on one port and
    nothing may be inferred from it; and ray3 has a neighbour that
    sends no optional TLVs at all.
    """
    core.lldp_neighbors = [
        # Both members of the LACP bundle to ray1 see the neighbour.
        # That is one logical port, not a neighbour on two ports, and
        # v0.6.0 read it as forwarded LLDP — which cost the real
        # network its mb0—mb1 uplink source.
        *(
            _neighbor(
                member, ray1.bridge_mac, remote, sys_name=ray1.sys_name,
                sys_desc=ray1.sys_descr, caps={"bridge"},
            )
            for member, remote in ((1, "Gi0/25"), (25, "Gi0/26"))
        ),
        # One device sending one row per VLAN interface, each with its
        # own management address — the shape mb0 Slot0/21 really has.
        # merge_rows below turns them back into one device.
        *(
            _neighbor(
                ROUTER_PORT, ROUTER_CHASSIS, f"vlan{n}",
                sys_name="MikroTik-core" if n == 0 else "",
                sys_desc="RouterOS x86 6.46.2" if n == 0 else "",
                caps={"bridge", "router"}, mgmt_ips=[address],
            )
            for n, address in enumerate(ROUTER_IPS)
        ),
        # `router` and nothing else: not a bridge, so no node of its
        # own — but the host it already is gets its name from here
        _neighbor(
            EDGE_ROUTER_PORT, EDGE_ROUTER_CHASSIS, "ether1",
            sys_name="MikroTik-edge", sys_desc="RouterOS CCR1009 7.11",
            caps={"router"}, mgmt_ips=EDGE_ROUTER_IPS,
        ),
        # the provider's switch: a bridge, but the boundary of the
        # network rather than a stray one
        _neighbor(
            UPLINK_PORT, UPLINK_CHASSIS, "10GE1/0/22", sys_name=UPLINK_NAME,
            sys_desc="Huawei CE6851-48S6Q-HI V200R005C10",
            caps={"bridge", "router"}, mgmt_ip=UPLINK_MGMT_IP,
        ),
        _neighbor(
            core_port_to_ray[ray2.ip], ray2.bridge_mac, "Gi0/24",
            sys_name=ray2.sys_name, sys_desc=ray2.sys_descr,
            caps={"bridge"},
        ),
        _neighbor(
            core_port_to_ray[ray4.ip], ray4.bridge_mac, "Gi0/24",
            sys_name=ray4.sys_name, sys_desc=ray4.sys_descr,
            caps={"bridge"},
        ),
    ]
    ray2.lldp_neighbors = [
        _neighbor(
            24, core.bridge_mac, "Gi0/2",
            sys_name=core.sys_name, sys_desc=core.sys_descr,
            caps={"bridge"},
        ),
        _neighbor(
            BRIDGE_PORT, BRIDGE_CHASSIS, "ether1",
            sys_name=BRIDGE_NAME, sys_desc=BRIDGE_DESC,
            caps={"bridge", "router"}, mgmt_ip=BRIDGE_MGMT_IP,
        ),
    ]
    # This agent reports capabilities for nobody — the HPE 1820 does
    # exactly that — so the empty column says nothing about any one
    # neighbour, and a named, addressed device is taken for a bridge
    ray4.lldp_neighbors = [
        _neighbor(24, core.bridge_mac, "Gi0/5", sys_name=core.sys_name,
                  cap_known=False),
        _neighbor(
            ASSUMED_PORT, ASSUMED_CHASSIS, "bridge/ether1",
            sys_name=ASSUMED_NAME, sys_desc="RouterOS hAP lite 6.49.10",
            mgmt_ip=ASSUMED_MGMT_IP, cap_known=False,
        ),
    ]
    ray1.lldp_neighbors = [
        # The other end of the LACP bundle, again on both members
        *(
            _neighbor(
                member, core.bridge_mac, remote, sys_name=core.sys_name,
                sys_desc=core.sys_descr, caps={"bridge"},
            )
            for member, remote in ((25, "Gi0/1"), (26, "Gi0/25"))
        ),
        # Two bridges behind one access port, with four ordinary devices
        # on the same port: the pseudo-switch stays (the box on the
        # cable is real), and both bridges are drawn behind it
        *(
            _neighbor(
                CHAIN_PORT, chassis, "ether1", sys_name=name,
                sys_desc=desc, caps={"bridge", "router"}, mgmt_ip=ip,
            )
            for chassis, name, desc, ip in CHAIN_BRIDGES
        ),
        # `LLDP Forward Message`: ray3's frame comes out of an access
        # port, while ray3's MAC sits in ray1's forwarding table behind
        # the uplink. The table is what gives the forwarding away —
        # several neighbours on one port never did.
        _neighbor(12, _iface_mac(ray3), "Gi0/1", sys_name=ray3.sys_name,
                  caps={"bridge"}),
    ]
    # A neighbour with the optional TLVs switched off, the way a
    # DES-3526 ships: a bare MAC with no name and no capabilities. The
    # absence of the bridge flag proves nothing, so it is drawn as an
    # unidentified LLDP device and raises no alarm.
    ray3.lldp_neighbors = [
        _neighbor(8, "00:1e:58:a9:00:63", "8", cap_known=False),
        # Ten cameras on one port, all of them bare MACs. Neither a
        # node nor an alarm: their LLDP data goes on their host cards.
        *(
            _neighbor(
                CAMERA_PORT, mac, mac, sys_desc=CAMERA_DESC, cap_known=False
            )
            for mac in CAMERAS
        ),
    ]
    # lldpLocPortDesc, the two ways it comes back on real hardware: an
    # operator's own port names on one switch, and one firmware
    # template with the port number substituted on another. Only the
    # first kind survives useful_port_labels.
    core.port_labels = {
        i: f"{core.sys_descr} Port {i}" for i in core.ports if i > 0
    }
    ray2.port_labels = {5: "Library", 13: "403 audit", 17: "209 audit"}
    for sw in (core, ray1, ray2, ray3, ray4):
        sw.port_labels, _dropped = useful_port_labels(
            sw.port_labels,
            {i: p.name for i, p in sw.ports.items()},
            {i: p.name for i, p in sw.ports.items()},
        )
        # a real collect_lldp does this before anyone sees the rows
        sw.lldp_neighbors = merge_rows(sw.lldp_neighbors)
        sw.lldp_forwarded, sw.lldp_crowded = analyse_ports(
            sw.lldp_neighbors,
            lambda if_index, sw=sw: aggregate_port(sw, if_index),
            sw.fdb,
        )


def _stp_ports(
    sw: SwitchData, states: dict[int, int], enabled: bool, root_id: str
) -> dict[int, StpPort]:
    ports: dict[int, StpPort] = {}
    for if_index, state in states.items():
        ports[if_index] = StpPort(
            bridge_port=if_index,
            if_index=if_index,
            name=sw.ports[if_index].name if if_index in sw.ports else str(if_index),
            state=state,
            enabled=enabled,
            path_cost=20000,
            designated_root=root_id,
            designated_bridge=root_id,
        )
    return ports


def _add_stp(core, ray1, ray2, ray3, ray4) -> None:
    """A spanning tree that is running — except on one switch.

    The core is the root at priority 4096. ray2 holds its uplink in
    blocking, which the map draws as a dashed red edge. ray4 has STP
    switched off and still reports priority 0, cost 0 and itself as the
    root: the exact trap the verdict in stp.py exists to catch, and the
    reason a demo needs it.
    """
    from .stp import judge  # local import: demo data, real verdict

    root_id = f"4096/{core.bridge_mac}"
    uptime = 4_000_000  # ~11 hours in TimeTicks
    core.ports[ROUTER_PORT].oper_up = True
    core.ports[EDGE_ROUTER_PORT].oper_up = True
    core.ports[UPLINK_PORT].oper_up = True
    ray4.ports[ASSUMED_PORT].oper_up = True
    core.stp = judge(StpData(
        supported=True, protocol_spec=3, priority=4096,
        time_since_change=120_000, top_changes=7,
        designated_root=root_id, root_cost=0, root_port=0, version=2,
        sys_uptime=uptime,
        ports=_stp_ports(core, {1: 5, 2: 5, 5: 5, 25: 5}, True, root_id),
    ))
    for ray, root_port, blocking in (
        (ray1, 25, None), (ray2, 24, 24), (ray3, 23, None),
    ):
        states = {root_port: 5}
        if blocking is not None:
            states[blocking] = 2  # blocking(2): the link carries nothing
        ray.stp = judge(StpData(
            supported=True, protocol_spec=3, priority=32768,
            time_since_change=118_000 + _scan_count * 100,
            top_changes=7 + _scan_count // 4,
            designated_root=root_id, root_cost=20000, root_port=root_port,
            version=2, sys_uptime=uptime,
            ports=_stp_ports(ray, states, True, root_id),
        ))
    # STP disabled: every field still answers, and every field lies
    ray4.stp = judge(StpData(
        supported=True, protocol_spec=3, priority=0,
        time_since_change=uptime, top_changes=0,
        designated_root=f"0/{ray4.bridge_mac}", root_cost=0, root_port=0,
        version=2, sys_uptime=uptime,
        ports=_stp_ports(ray4, {i: 1 for i in range(1, 5)}, False, ""),
    ))
    for sw in (core, ray1, ray2, ray3, ray4):
        sw.sys_uptime = uptime


# Rows shaped like the ones that invented 32 phantom devices on a real
# switch: an over-long suffix (the walk left the table) and a multicast
# address. The demo network is built in memory, so they are fed to the
# real validator to show what it now rejects.
BOGUS_FDB_ROWS = [
    ((0x00, 0x0e, 0x04, 0xb7, 0x79, 0xab, 0x20, 0x7b), 3, 7),
    ((0x04, 0xb7, 0x79, 0xab, 0x20, 0x7b, 0x31), 3, 6),
    ((0x01, 0x00, 0x5e, 0x00, 0x00, 0xfb), 3, 6),
]


def _report_rejected_fdb(sw: SwitchData) -> None:
    """Runs the demo's bogus rows through the collector's validator."""
    bad_suffix = bad_mac = 0
    for suffix, value, expected_len in BOGUS_FDB_ROWS:
        _mac, _port, reason = parse_fdb_entry(suffix, value, expected_len)
        if not reason:
            continue
        if "suffix" in reason:
            bad_suffix += 1
        else:
            bad_mac += 1
    log.info(
        "%s FDB: %d entries rejected (bad suffix: %d, bad MAC: %d)",
        sw.ip, bad_suffix + bad_mac, bad_suffix, bad_mac,
    )


_journal_seeded = False
_recover_mac: str | None = None  # host that comes back -> host_down clear
_flap_mac: str | None = None     # host bouncing every few cycles -> FLAPPING


def enrich_db(db: Database, hosts: list[dict]) -> None:
    """v0.3 data for the demo: IPs, names, ping state, journal events.

    Shows every UI state: green (replying), grey (not replying),
    blue (no IP — cannot ping), hosts with and without names. Ping
    state is only seeded once per host — after that ping_results()
    owns it, so the down/recover scenario is not reset by rescans.
    """
    global _journal_seeded, _recover_mac, _flap_mac
    now = time.time()

    rows = db.hosts_by_mac()
    # An address ARP knows is confirmed on the spot, so the demo must
    # not hand one to a MAC whose whole point is to stay unconfirmed
    hosts = [h for h in hosts if not h.get("unconfirmed")]
    # The device whose frames arrive damaged is an ordinary one: it has
    # an IP, answers ping and stays on the map — only the copies of its
    # address do not. Its address is set aside from the positional
    # assignment below so it never drifts.
    # EDGE_ROUTER_CHASSIS is here on purpose and gets no IP below: its
    # whole point is a device the inventory cannot name
    fixed = (
        CORRUPT_REAL, CORRUPT_NEIGHBOR, TRUNK_ONLY_MAC, REMEMBERED_MAC,
        ROUTER_CHASSIS,
        EDGE_ROUTER_CHASSIS, UPLINK_CHASSIS, *UPLINK_HOST_MACS, *CAMERAS,
    )
    hosts = [h for h in hosts if h["mac"] not in fixed]
    for mac, ip, name in (
        (CORRUPT_REAL, CORRUPT_IP, "cam-4floor.demo.lan"),
        (CORRUPT_NEIGHBOR, CORRUPT_NEIGHBOR_IP, "cam-4floor-2.demo.lan"),
        (TRUNK_ONLY_MAC, TRUNK_ONLY_IP, "nvr-3floor.demo.lan"),
        (REMEMBERED_MAC, REMEMBERED_IP, "nvr-2floor.demo.lan"),
        (ROUTER_CHASSIS, ROUTER_IPS[0], "gw.demo.lan"),
        (UPLINK_CHASSIS, UPLINK_MGMT_IP, ""),
        # no *.demo.lan names: these are not ours to name
        *zip(UPLINK_HOST_MACS, UPLINK_HOST_IPS, [""] * UPLINK_HOSTS),
        *(
            (mac, f"10.0.98.{n}", f"cam-hall-{n:02d}.demo.lan")
            for n, mac in enumerate(CAMERAS, start=1)
        ),
    ):
        db.set_ips({mac: ip})
        db.set_name(mac, name)
        if not rows.get(mac, {}).get("last_ping_ok"):
            db.set_ping_state(mac, up=True, last_ok=now)
    for i, host in enumerate(hosts):
        mac = host["mac"]
        if i % 5 == 4:
            continue  # some hosts never got an IP
        db.set_ips({mac: f"10.0.99.{10 + i}"})
        if i == 3:
            # one host whose address no ARP table confirms: its card
            # says so, and if its MAC ever leaves the switch tables the
            # address is released instead of faking the host alive
            db.set_ip_confirmed(mac, 0)
        if i % 3 != 2:  # some hosts have no name — only an IP
            db.set_name(mac, f"pc-{i + 1:02d}.demo.lan")
        row = rows.get(mac, {})
        if row.get("ping_up") or row.get("last_ping_ok"):
            continue  # already seeded
        if not _journal_seeded and i in (1, 6):
            # a couple powered off: replied 15 minutes ago; marked
            # monitored so the demo still shows host_down raise/clear
            db.set_ping_state(mac, up=False, last_ok=now - 15 * 60)
            db.set_monitored(mac, True)
            if i == 6:
                _recover_mac = mac
        else:
            db.set_ping_state(mac, up=True, last_ok=now)
            if not _journal_seeded and i in (0, 3):
                db.set_monitored(mac, True)  # stars in the UI
            if not _journal_seeded and i == 8:
                # bounces every few ping cycles -> flap damping demo
                db.set_monitored(mac, True)
                _flap_mac = mac

    if not _journal_seeded and len(hosts) > 6:
        _journal_seeded = True
        _seed_offmap_hosts(db, now)
        db.add_event(now - 40 * 60, "host_down", hosts[6]["mac"], "10.0.99.16")
        db.add_event(now - 15 * 60, "host_down", hosts[1]["mac"], "pc-02.demo.lan")
        db.add_event(now - 5 * 60, "host_up", hosts[3]["mac"], "pc-04.demo.lan")


# Devices whose MAC is no longer in any FDB: still drawn at the port
# where they were seen last, greyed out, until the grace window ends.
# (mac, ip, name, switch, port, hours since it was last seen)
ACCESS_4 = "10.0.0.24"  # access-sw-4
ACCESS_2 = "10.0.0.22"  # access-sw-2, the one with the pseudo-switch
STALE_HOSTS = [
    # Alone on its port: still drawn as a single grey dot
    ("00:ee:00:00:0a:01", "10.0.99.71", "nb-sales.demo.lan", ACCESS_4, "Gi0/5", 3.0),
    # Two on one port: a small offline group
    ("00:ee:00:00:0a:02", "10.0.99.72", "printer-2f.demo.lan", ACCESS_4, "Gi0/6", 9.5),
    ("00:ee:00:00:0a:03", "10.0.99.73", "scanner-2f.demo.lan", ACCESS_4, "Gi0/6", 9.0),
    # Six on one port: a big offline group
    ("00:ee:00:00:0a:11", "10.0.99.81", "desk-a.demo.lan", ACCESS_4, "Gi0/7", 5.0),
    ("00:ee:00:00:0a:12", "10.0.99.82", "desk-b.demo.lan", ACCESS_4, "Gi0/7", 5.5),
    ("00:ee:00:00:0a:13", "10.0.99.83", "desk-c.demo.lan", ACCESS_4, "Gi0/7", 6.0),
    ("00:ee:00:00:0a:14", "10.0.99.84", "desk-d.demo.lan", ACCESS_4, "Gi0/7", 6.5),
    ("00:ee:00:00:0a:15", "10.0.99.85", "desk-e.demo.lan", ACCESS_4, "Gi0/7", 7.0),
    ("00:ee:00:00:0a:16", "10.0.99.86", "desk-f.demo.lan", ACCESS_4, "Gi0/7", 7.5),
    # The mixed case: two offline devices on the port that also carries
    # five live ones, so they hang off the pseudo-switch there
    ("00:ee:00:00:0a:21", "10.0.99.91", "tv-lobby.demo.lan", ACCESS_2, "Gi0/5", 4.0),
    ("00:ee:00:00:0a:22", "10.0.99.92", "ap-lobby.demo.lan", ACCESS_2, "Gi0/5", 8.0),
]

# Devices ARP knows but no switch port ever showed — a subnet behind a
# router, exactly what hides real segments from an L2 map
UNLOCATED_HOSTS = [
    ("00:ee:00:00:0b:01", "10.4.23.14", "audit-220.demo.lan"),
    ("00:ee:00:00:0b:02", "10.4.23.51", "cam-hall.demo.lan"),
]


def _seed_offmap_hosts(db: Database, now: float) -> None:
    """Hosts last seen hours ago (grace window) and ARP-only devices."""
    # The inventory has seen this one on a real access port. From the
    # next scan on it must be drawn there and not on the core's trunk,
    # however long it stays quiet.
    db.upsert_hosts([{
        "mac": REMEMBERED_MAC, "switch": REMEMBERED_SWITCH,
        "port": REMEMBERED_PORT, "vlan": 8,
    }])
    db.upsert_hosts([
        {"mac": mac, "switch": switch, "port": port, "vlan": 8}
        for mac, _ip, _name, switch, port, _hours in STALE_HOSTS
    ])
    for mac, ip, name, _switch, _port, hours in STALE_HOSTS:
        db.set_ips({mac: ip})
        db.set_name(mac, name)
        db.set_last_seen(mac, now - hours * 3600)
        db.set_ping_state(mac, up=False, last_ok=now - hours * 3600)
    for mac, ip, name in UNLOCATED_HOSTS:
        db.set_ips({mac: ip}, create_missing=True)
        db.set_name(mac, name)
        db.set_ping_state(mac, up=True, last_ok=now)


_ping_cycle = 0
RECOVER_AFTER = 8    # ping cycles until the recovering host answers again
MASS_DOWN_AT = 12    # all hosts behind the pseudo-switch port go silent
MASS_RECOVER_AT = 20 # most of them answer again -> port_hosts_down clears
MASS_PORT = ("10.0.0.22", "Gi0/5")  # the port with the unmanaged switch


def ping_results(db: Database) -> dict[str, bool]:
    """One demo ping cycle: mac -> replied.

    Two hosts are down (host_down raises after 3 cycles); one of them
    recovers at cycle RECOVER_AFTER, demonstrating the CLEARED path.
    At MASS_DOWN_AT every host behind the pseudo-switch port stops
    answering at once -> a single port_hosts_down alarm; at
    MASS_RECOVER_AT most of them come back -> the alarm clears.
    """
    global _ping_cycle
    _ping_cycle += 1
    now = time.time()
    if _recover_mac and _ping_cycle >= RECOVER_AFTER:
        db.set_ping_state(_recover_mac, up=True, last_ok=now)
    if _flap_mac:
        # 3 cycles silent, 1 cycle up: host_down raises and clears over
        # and over -> flap damping mutes it after the 3rd raise
        flap_up = _ping_cycle % 4 == 0
        row = db.hosts_by_mac().get(_flap_mac, {})
        db.set_ping_state(
            _flap_mac, up=flap_up,
            last_ok=now if flap_up else row.get("last_ping_ok", 0),
        )

    rows = db.hosts_by_mac()
    mass_macs = sorted(
        mac for mac, row in rows.items()
        if (row["switch_ip"], row["port"]) == MASS_PORT and row["ip"]
    )
    if _ping_cycle == MASS_DOWN_AT:
        for mac in mass_macs:
            db.set_ping_state(mac, up=False, last_ok=now)
    elif _ping_cycle == MASS_RECOVER_AT:
        for mac in mass_macs[: max(1, len(mass_macs) * 2 // 3)]:
            db.set_ping_state(mac, up=True, last_ok=now)

    db.touch_ping_ok(now)
    return {
        mac: bool(row["ping_up"])
        for mac, row in db.hosts_by_mac().items()
        if row["ip"]
    }


class DemoCounters:
    """Synthetic raw counter samples for the demo network.

    Every active physical port carries a smooth random traffic curve
    (a bounded random walk); one port accumulates damaged frames
    (-> port_errors) and another one accumulates discards without a
    single error (-> port_discards, info, syslog only), so the split
    between the two alarms is visible side by side.

    One switch answers for the inbound octets and not for the outbound
    ones, the way a DGS-1210 Rev.F1 does. Its "Out, Mbit/s" column must
    read "\u2014" and raise nothing — before v0.6.4 it read 0.0 on a
    trunk carrying traffic in both directions. Packet counters
    are derived from the traffic, which gives the error-ratio rule
    something real to work with. Real Sample objects are produced so
    the whole delta pipeline in CounterStore is exercised, not
    bypassed.
    """

    # The agent that does not implement ifHCOutOctets and answers
    # nothing on the 32-bit fallback either: an unknown column, not a
    # zero one
    NO_OUT_OCTETS_SWITCH = "10.0.0.23"  # access-sw-3
    ERROR_SWITCH = "10.0.0.22"  # access-sw-2
    ERROR_PORT = 3              # Gi0/3: damaged frames
    DISCARD_PORT = 4            # Gi0/4: filtering, no errors at all
    # Gi0/6 is the failing cable: the same fault the invented MAC
    # addresses come from, so the corruption alarm goes critical
    CORRUPT_ERROR_PORT = CORRUPT_PORT
    AVG_FRAME_BYTES = 800       # to turn octets into packet counters
    # One member of the core—ray1 LACP (the same physical cable seen
    # from both ends) flaps on a timer -> lag_degraded raise and clear,
    # edge label drops to "LACP 1×1 Gbit/s (1/2)"
    LAG_FLAP = {"10.0.0.10": 25, "10.0.0.21": 26}
    FLAP_PERIOD = 6  # counter cycles down, then the same up
    # A port whose link changes state on EVERY cycle: within a few
    # minutes it passes thresholds.flaps_per_window -> port_flapping
    LINK_FLAP = (FLAP_SWITCH, FLAP_PORT)

    def __init__(self):
        self._rng = random.Random(11)
        self._rates: dict[tuple[str, int], list[float]] = {}   # [in, out] Mbit/s
        self._totals: dict[tuple[str, int], list[int]] = {}    # running counters
        self._last_ts: float | None = None
        self._cycle = 0

    def sample(self, switches: list[SwitchData]) -> dict[str, dict[int, Sample]]:
        now = time.time()
        dt = now - self._last_ts if self._last_ts else 60.0
        self._last_ts = now
        self._cycle += 1
        member_down = (self._cycle // self.FLAP_PERIOD) % 2 == 1
        out: dict[str, dict[int, Sample]] = {}
        for sw in switches:
            flap_port = self.LAG_FLAP.get(sw.ip)
            if flap_port in sw.ports:
                sw.ports[flap_port].oper_up = not member_down
            link_flap = (
                self.LINK_FLAP[1] if sw.ip == self.LINK_FLAP[0] else None
            )
            if link_flap in sw.ports:
                sw.ports[link_flap].oper_up = self._cycle % 2 == 0
                # ifLastChange moves with every transition, which is
                # what lets the tracker count a bounce it never saw
                sw.ports[link_flap].last_change = self._cycle * 6000
            samples: dict[int, Sample] = {}
            for p in sw.ports.values():
                # the flapping port keeps reporting while it is down:
                # a real agent does the same, and ifLastChange is the
                # whole point of polling it
                if not p.is_physical or (
                    not p.oper_up and p.if_index != link_flap
                ):
                    continue
                key = (sw.ip, p.if_index)
                rate = self._rates.setdefault(
                    key, [self._rng.uniform(1, 60), self._rng.uniform(1, 30)]
                )
                for i in (0, 1):
                    step = self._rng.gauss(0, rate[i] * 0.15 + 0.5)
                    rate[i] = min(max(rate[i] + step, 0.2), 900.0)
                # kept as floats and truncated only when a sample is
                # emitted, so the rates come out right at any interval
                tot = self._totals.setdefault(key, [0.0] * 8)
                d_in = rate[0] * 1e6 / 8 * dt   # octets
                d_out = rate[1] * 1e6 / 8 * dt
                tot[0] += d_in
                tot[1] += d_out
                tot[6] += d_in / self.AVG_FRAME_BYTES   # in packets
                tot[7] += d_out / self.AVG_FRAME_BYTES  # out packets
                if sw.ip == self.ERROR_SWITCH and self._cycle >= 2:
                    if p.if_index == self.ERROR_PORT:
                        # 120 damaged frames a minute, split in/out:
                        # over the 5/min threshold and ~0.05% of this
                        # port's frames, so both error rules agree
                        tot[2] += 90 * dt / 60
                        tot[3] += 30 * dt / 60
                    elif p.if_index == self.CORRUPT_ERROR_PORT:
                        tot[2] += 40 * dt / 60
                        tot[3] += 20 * dt / 60
                    elif p.if_index == self.DISCARD_PORT:
                        # a port that filters a lot and breaks nothing
                        tot[4] += 600 * dt / 60
                silent_out = sw.ip == self.NO_OUT_OCTETS_SWITCH
                samples[p.if_index] = Sample(
                    ts=now,
                    in_octets=int(tot[0]),
                    out_octets=None if silent_out else int(tot[1]),
                    in_errors=int(tot[2]), out_errors=int(tot[3]),
                    in_discards=int(tot[4]), out_discards=int(tot[5]),
                    in_pkts=int(tot[6]),
                    out_pkts=None if silent_out else int(tot[7]),
                    last_change=p.last_change,
                )
            out[sw.ip] = samples
        return out

    def columns(self, ip: str) -> dict[str, ColumnStatus]:
        """What a real poll of this switch would report per column."""
        report = {
            name: ColumnStatus(oid=oid, rows=26)
            for name, oid in (
                ("in_octets", "1.3.6.1.2.1.31.1.1.1.6"),
                ("out_octets", "1.3.6.1.2.1.31.1.1.1.10"),
                ("in_errors", "1.3.6.1.2.1.2.2.1.14"),
                ("out_errors", "1.3.6.1.2.1.2.2.1.20"),
                ("in_discards", "1.3.6.1.2.1.2.2.1.13"),
                ("out_discards", "1.3.6.1.2.1.2.2.1.19"),
            )
        }
        if ip == self.NO_OUT_OCTETS_SWITCH:
            report["out_octets"] = ColumnStatus(
                oid="1.3.6.1.2.1.2.2.1.16", rows=0
            )
        return report
