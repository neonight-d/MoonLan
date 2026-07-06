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
"""

from __future__ import annotations

import random
import time

from .db import Database
from .snmp_collector import PortInfo, SwitchData

random.seed(7)  # keep the demo network identical between runs

VLAN_NAMES = {1: "default", 8: "office", 11: "ipmi"}
LAG_IFINDEX = 1000  # ifIndex of the logical port Po1


def _rand_mac(prefix: str = "02:4d:4c") -> str:
    return prefix + ":" + ":".join(f"{random.randint(0, 255):02x}" for _ in range(3))


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


def _synthetic_port(sw: SwitchData, bridge_port: int) -> int:
    """A trunk bridge-port missing from dot1dBasePortIfIndex."""
    if_index = -bridge_port
    sw.ports[if_index] = PortInfo(
        if_index=if_index, name=f"bridge-port {bridge_port}", is_physical=False
    )
    return if_index


def demo_network() -> list[SwitchData]:
    """A star: core + four rays, LACP, a LAG trunk, a pseudo-switch, VLANs."""
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
    # LAG trunks without IEEE8023-LAG-MIB. The rays do not see the core.
    core_trunk_to_ray3 = _synthetic_port(core, 3)
    ray3_trunk = _synthetic_port(ray3, 24)
    core_port_to_ray = {
        ray1.ip: 1,               # LACP member -> normalized to Po1
        ray2.ip: 2,
        ray3.ip: core_trunk_to_ray3,
        ray4.ip: 4,
    }
    for ray in (ray2, ray4):
        core.ports[core_port_to_ray[ray.ip]].oper_up = True
        ray.ports[24].oper_up = True
    for ray in (ray1, ray2, ray3, ray4):
        core.fdb[ray.bridge_mac] = core_port_to_ray[ray.ip]

    # Stray MACs of other rays leak through the core onto the rays'
    # uplinks: they reveal each ray's uplink port, and the branch
    # invariant must keep them from becoming false ray-to-ray links.
    ray1.fdb[_iface_mac(ray3)] = 25          # LACP member -> Po1
    ray2.fdb[_iface_mac(ray4)] = 24
    ray3.fdb[_iface_mac(ray2)] = ray3_trunk  # synthetic uplink
    # ray4 sees nobody at all -> its link port stays "?"

    def connect_host(sw: SwitchData, port: int, vlan: int) -> str:
        mac = _rand_mac()
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

    return switches


_journal_seeded = False


def enrich_db(db: Database, hosts: list[dict]) -> None:
    """v0.3 data for the demo: IPs, names, ping state, journal events.

    Shows every UI state: green (replying), grey (not replying),
    blue (no IP — cannot ping), hosts with and without names.
    """
    global _journal_seeded
    now = time.time()

    for i, host in enumerate(hosts):
        mac = host["mac"]
        if i % 5 == 4:
            continue  # some hosts never got an IP
        db.set_ips({mac: f"10.0.99.{10 + i}"})
        if i % 3 != 2:  # some hosts have no name — only an IP
            db.set_name(mac, f"pc-{i + 1:02d}.demo.lan")
        if i in (1, 6):  # a couple powered off: replied 15 minutes ago
            db.set_ping_state(mac, up=False, last_ok=now - 15 * 60)
        else:
            db.set_ping_state(mac, up=True, last_ok=now)

    if not _journal_seeded and len(hosts) > 6:
        _journal_seeded = True
        db.add_event(now - 40 * 60, "host_down", hosts[6]["mac"], "10.0.99.16")
        db.add_event(now - 15 * 60, "host_down", hosts[1]["mac"], "pc-02.demo.lan")
        db.add_event(now - 5 * 60, "host_up", hosts[3]["mac"], "pc-04.demo.lan")
