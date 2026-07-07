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
"""

from __future__ import annotations

import random
import time

from .counters import Sample
from .db import Database
from .snmp_collector import PortInfo, SwitchData, infer_lag_groups

# The RNG is re-seeded inside demo_network so every scan rebuilds the
# same base network (stable MACs -> no phantom new_mac events)
_rng = random.Random(7)

VLAN_NAMES = {1: "default", 8: "office", 11: "ipmi"}
LAG_IFINDEX = 1000  # ifIndex of the logical port Po1

# Latecomer MACs sort after the base 02:4d:4c hosts so the positional
# IP/name assignment in enrich_db is not reshuffled by their arrival
LATECOMERS = {"fe:ee:00:00:00:01": 9, "fe:ee:00:00:00:02": 10}  # mac -> ray1 port


def _rand_mac(prefix: str = "02:4d:4c") -> str:
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

    # Latecomers appear from the second scan on -> new_mac alarms
    if _scan_count >= 2:
        for mac, port in LATECOMERS.items():
            ray1.ports[port].oper_up = True
            ray1.fdb[mac] = port
            ray1.port_pvid[port] = 8
            core.fdb[mac] = core_port_to_ray[ray1.ip]

    return switches


_journal_seeded = False
_recover_mac: str | None = None  # host that comes back -> host_down clear


def enrich_db(db: Database, hosts: list[dict]) -> None:
    """v0.3 data for the demo: IPs, names, ping state, journal events.

    Shows every UI state: green (replying), grey (not replying),
    blue (no IP — cannot ping), hosts with and without names. Ping
    state is only seeded once per host — after that ping_results()
    owns it, so the down/recover scenario is not reset by rescans.
    """
    global _journal_seeded, _recover_mac
    now = time.time()

    rows = db.hosts_by_mac()
    for i, host in enumerate(hosts):
        mac = host["mac"]
        if i % 5 == 4:
            continue  # some hosts never got an IP
        db.set_ips({mac: f"10.0.99.{10 + i}"})
        if i % 3 != 2:  # some hosts have no name — only an IP
            db.set_name(mac, f"pc-{i + 1:02d}.demo.lan")
        row = rows.get(mac, {})
        if row.get("ping_up") or row.get("last_ping_ok"):
            continue  # already seeded
        if not _journal_seeded and i in (1, 6):
            # a couple powered off: replied 15 minutes ago
            db.set_ping_state(mac, up=False, last_ok=now - 15 * 60)
            if i == 6:
                _recover_mac = mac
        else:
            db.set_ping_state(mac, up=True, last_ok=now)

    if not _journal_seeded and len(hosts) > 6:
        _journal_seeded = True
        db.add_event(now - 40 * 60, "host_down", hosts[6]["mac"], "10.0.99.16")
        db.add_event(now - 15 * 60, "host_down", hosts[1]["mac"], "pc-02.demo.lan")
        db.add_event(now - 5 * 60, "host_up", hosts[3]["mac"], "pc-04.demo.lan")


_ping_cycle = 0
RECOVER_AFTER = 8  # ping cycles until the recovering host answers again


def ping_results(db: Database) -> dict[str, bool]:
    """One demo ping cycle: mac -> replied.

    Two hosts are down (host_down raises after 3 cycles); one of them
    recovers at cycle RECOVER_AFTER, demonstrating the CLEARED path.
    """
    global _ping_cycle
    _ping_cycle += 1
    now = time.time()
    if _recover_mac and _ping_cycle >= RECOVER_AFTER:
        db.set_ping_state(_recover_mac, up=True, last_ok=now)
    db.touch_ping_ok(now)
    return {
        mac: bool(row["ping_up"])
        for mac, row in db.hosts_by_mac().items()
        if row["ip"]
    }


class DemoCounters:
    """Synthetic raw counter samples for the demo network.

    Every active physical port carries a smooth random traffic curve
    (a bounded random walk); one port accumulates errors fast enough
    to cross the default threshold -> port_errors alarm after ~2
    cycles. Real Sample objects are produced so the whole delta
    pipeline in CounterStore is exercised, not bypassed.
    """

    ERROR_SWITCH = "10.0.0.22"  # access-sw-2
    ERROR_PORT = 3              # Gi0/3

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
        out: dict[str, dict[int, Sample]] = {}
        for sw in switches:
            samples: dict[int, Sample] = {}
            for p in sw.ports.values():
                if not p.is_physical or not p.oper_up:
                    continue
                key = (sw.ip, p.if_index)
                rate = self._rates.setdefault(
                    key, [self._rng.uniform(1, 60), self._rng.uniform(1, 30)]
                )
                for i in (0, 1):
                    step = self._rng.gauss(0, rate[i] * 0.15 + 0.5)
                    rate[i] = min(max(rate[i] + step, 0.2), 900.0)
                tot = self._totals.setdefault(key, [0, 0, 0, 0, 0, 0])
                tot[0] += int(rate[0] * 1e6 / 8 * dt)  # in octets
                tot[1] += int(rate[1] * 1e6 / 8 * dt)  # out octets
                if (
                    sw.ip == self.ERROR_SWITCH
                    and p.if_index == self.ERROR_PORT
                    and self._cycle >= 2
                ):
                    # ~12 err/min + ~4 disc/min > default threshold of 10
                    tot[2] += max(1, int(12 * dt / 60))
                    tot[4] += max(1, int(4 * dt / 60))
                samples[p.if_index] = Sample(
                    ts=now,
                    in_octets=tot[0], out_octets=tot[1],
                    in_errors=tot[2], out_errors=tot[3],
                    in_discards=tot[4], out_discards=tot[5],
                )
            out[sw.ip] = samples
        return out
