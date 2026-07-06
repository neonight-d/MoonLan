"""Building the network topology from data collected from switches.

Algorithm (v0.4):

1. LACP: the ifIndex of every physical member port is mapped to the
   aggregate's ifIndex, so the aggregate takes part in all computations
   (links, uplinks, host binding) as a single logical port.
2. Direct links. A switch is recognized in neighbors' FDB tables by any
   MAC from its own_macs set (bridge base MAC, interface MACs,
   management-IP MAC), not only by dot1dBaseBridgeAddress: real frames
   are sent from interface MACs. For a pair (A, B) take the port pA where
   A sees B and the port pB where B sees A. Let S(A, p) be the set of
   *other polled switches* visible in A's FDB on port p. The link
   A(pA)—B(pB) is direct if and only if S(A, pA) ∩ S(B, pB) = ∅: two
   ports facing each other must not both see the same third switch.
   This cuts off false ray-to-ray links in a star, where every ray sees
   all the other rays through the core. When visibility is one-way
   (B does not see A anywhere), an exclusion rule applies: B is a direct
   neighbor of A on pA if no switch D in S(A, pA) has B "behind" it
   (D sees B on a port where it does not see A). Such a link is built
   with b_port = "?".
3. Uplink ports: a port where any other switch is visible, or a port
   with a suspiciously large number of MACs (> UPLINK_MAC_THRESHOLD) —
   there is almost certainly another switch behind it, even if unmanaged.
4. End devices. MACs on the remaining ports are hosts. The same MAC can
   be visible from several switches; the host is bound to the port with
   the fewest other MACs (that is the port of direct attachment).
5. Unmanaged switches. If more than unmanaged_threshold hosts are found
   on a non-uplink port, there is almost certainly a switch without SNMP
   or an access point behind it: the hosts are grouped under a pseudo node.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable

from .snmp_collector import SwitchData

log = logging.getLogger(__name__)

UPLINK_MAC_THRESHOLD = 8
UNMANAGED_THRESHOLD = 3  # hosts per port; more — draw a pseudo-switch


@dataclass
class TopologyState:
    """Thread-safe storage of the current topology."""

    switches: list[dict] = field(default_factory=list)
    links: list[dict] = field(default_factory=list)
    hosts: list[dict] = field(default_factory=list)
    pseudo_switches: list[dict] = field(default_factory=list)
    vlan_names: dict[int, str] = field(default_factory=dict)
    last_scan: float = 0.0
    scanning: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def update(
        self,
        switches: list[dict],
        links: list[dict],
        hosts: list[dict],
        pseudo_switches: list[dict],
        vlan_names: dict[int, str],
    ) -> None:
        with self._lock:
            self.switches = switches
            self.links = links
            self.hosts = hosts
            self.pseudo_switches = pseudo_switches
            self.vlan_names = vlan_names
            self.last_scan = time.time()

    def as_dict(self) -> dict:
        with self._lock:
            return {
                "switches": self.switches,
                "links": self.links,
                "hosts": self.hosts,
                "pseudo_switches": self.pseudo_switches,
                "vlan_names": self.vlan_names,
                "last_scan": self.last_scan,
                "scanning": self.scanning,
            }

    def search(self, query: str) -> list[dict]:
        q = query.strip().lower()
        if not q:
            return []
        found: list[dict] = []
        with self._lock:
            for sw in self.switches:
                haystack = f"{sw['name']} {sw['ip']} {sw.get('mac', '')}".lower()
                if q in haystack:
                    found.append({"type": "switch", **sw})
            for host in self.hosts:
                haystack = (
                    f"{host['mac']} {host.get('name', '')} {host.get('ip', '')}"
                ).lower()
                if q in haystack:
                    found.append({"type": "host", **host})
        return found


class FdbStability:
    """Smoothing of FDB aging between polls.

    A neighbor's MAC can age out of the FDB by the time of a poll, which
    makes the link set flicker. An entry from previous polls lives for
    ttl cycles until confirmed by fresh data; confirmation resets the
    countdown. The merged FDB is used only for computing links and
    uplink ports; host binding and last_seen use fresh data.
    """

    def __init__(self, ttl: int = 3):
        self.ttl = ttl
        # ip -> mac -> [if_index, polls remaining]
        self._cache: dict[str, dict[str, list[int]]] = {}

    def merge(self, sw_ip: str, fresh: dict[str, int]) -> dict[str, int]:
        cache = self._cache.setdefault(sw_ip, {})
        for mac in list(cache):
            cache[mac][1] -= 1
            if cache[mac][1] < 0:  # survived ttl unconfirmed polls
                del cache[mac]
        for mac, if_index in fresh.items():
            cache[mac] = [if_index, self.ttl]
        return {mac: entry[0] for mac, entry in cache.items()}


def _port_name(sw: SwitchData, if_index: int) -> str:
    port = sw.ports.get(if_index)
    return port.name if port and port.name else str(if_index)


def _lag_info(sw: SwitchData, agg_index: int) -> tuple[list[str], int]:
    """Names of the aggregate's physical member ports and their total speed."""
    members = sorted(m for m, agg in sw.lag_members.items() if agg == agg_index)
    names = [_port_name(sw, m) for m in members]
    speed = sum(sw.ports[m].speed_mbps for m in members if m in sw.ports)
    return names, speed


def _make_link(a: SwitchData, pa: int, b: SwitchData, pb: int | None) -> dict:
    """Link A(pA)—B(pB); pb=None — B's port is unknown (one-way visibility)."""
    link = {
        "a": a.ip,
        "b": b.ip,
        "a_port": _port_name(a, pa),
        "b_port": _port_name(b, pb) if pb is not None else "?",
        "speed_mbps": 0,
        "lag": None,
    }
    a_members, a_speed = _lag_info(a, pa)
    b_members, b_speed = _lag_info(b, pb) if pb is not None else ([], 0)
    if a_members or b_members:
        members = a_members or b_members
        link["lag"] = {
            "members": members,
            "count": len(members),
            "a_members": a_members,
            "b_members": b_members,
        }
        link["speed_mbps"] = a_speed or b_speed
    else:
        port = a.ports.get(pa) or (b.ports.get(pb) if pb is not None else None)
        link["speed_mbps"] = port.speed_mbps if port else 0
    return link


def _one_way_link(
    a: SwitchData,
    pa: int | None,
    b: SwitchData,
    pb: int | None,
    sees: dict[str, dict[str, int]],
    switches_on_port: dict[str, dict[int, set[str]]],
) -> dict | None:
    """Fallback algorithm: a link under one-way visibility.

    viewer sees target on port p, target does not see viewer anywhere.
    target is a direct neighbor if no other switch D in S(viewer, p) has
    target "behind" it: D sees target on a port where D does not see viewer.
    """
    viewer, port, target = (a, pa, b) if pb is None else (b, pb, a)
    candidates = switches_on_port[viewer.ip].get(port, set())

    def behind(d_ip: str) -> bool:
        viewer_port = sees[d_ip].get(viewer.ip)
        return any(
            target.ip in ips and p != viewer_port
            for p, ips in switches_on_port[d_ip].items()
        )

    direct = target.ip in candidates and not any(
        d_ip != target.ip and behind(d_ip) for d_ip in candidates
    )
    if not direct:
        log.warning(
            "Incomplete FDB: %s sees %s, but %s does not see %s, and the "
            "direct neighbor could not be determined — no link is drawn",
            viewer.ip, target.ip, target.ip, viewer.ip,
        )
        return None
    log.info(
        "Link %s—%s built from one-way visibility (%s does not see %s)",
        viewer.ip, target.ip, target.ip, viewer.ip,
    )
    return _make_link(viewer, port, target, None)


def build_topology(
    collected: Iterable[SwitchData],
    unmanaged_threshold: int = UNMANAGED_THRESHOLD,
    fdb_stability: FdbStability | None = None,
) -> tuple[list[dict], list[dict], list[dict], list[dict], dict[int, str]]:
    """Turns poll data into nodes and links for the map."""
    switches = [sw for sw in collected if sw.reachable]
    # Any MAC from own_macs identifies the switch; bridge_mac is a
    # fallback in case own_macs is empty (e.g. old data)
    mac_to_switch: dict[str, SwitchData] = {}
    for sw in switches:
        for mac in sw.own_macs | ({sw.bridge_mac} if sw.bridge_mac else set()):
            mac_to_switch.setdefault(mac, sw)
    # FDB with LACP member ports mapped to the logical aggregate port
    fdb = {
        sw.ip: {
            mac: sw.lag_members.get(if_index, if_index)
            for mac, if_index in sw.fdb.items()
        }
        for sw in switches
    }

    # For links and uplinks — FDB merged with previous polls (protection
    # against aging); host binding below uses the fresh fdb
    if fdb_stability is not None:
        link_fdb = {sw.ip: fdb_stability.merge(sw.ip, fdb[sw.ip]) for sw in switches}
    else:
        link_fdb = fdb

    # S(A, p): which switches are visible on each of A's ports;
    # sees[A][B] — the port where A sees B (if several — the most frequent)
    switches_on_port: dict[str, dict[int, set[str]]] = {}
    sees: dict[str, dict[str, int]] = {}
    for sw in switches:
        per_port: dict[int, set[str]] = {}
        sightings: dict[str, Counter] = {}
        for mac, if_index in link_fdb[sw.ip].items():
            neighbor = mac_to_switch.get(mac)
            if neighbor is None or neighbor.ip == sw.ip:
                continue
            per_port.setdefault(if_index, set()).add(neighbor.ip)
            sightings.setdefault(neighbor.ip, Counter())[if_index] += 1
        switches_on_port[sw.ip] = per_port
        sees[sw.ip] = {
            ip: counts.most_common(1)[0][0] for ip, counts in sightings.items()
        }

    # 1. Direct links: the intersection of visible-switch sets must be empty
    links: list[dict] = []
    for i, a in enumerate(switches):
        for b in switches[i + 1:]:
            pa = sees[a.ip].get(b.ip)
            pb = sees[b.ip].get(a.ip)
            if pa is None and pb is None:
                continue
            if pa is None or pb is None:
                link = _one_way_link(a, pa, b, pb, sees, switches_on_port)
                if link:
                    links.append(link)
                continue
            if switches_on_port[a.ip][pa] & switches_on_port[b.ip][pb]:
                continue  # a third switch is visible through these ports — not direct
            links.append(_make_link(a, pa, b, pb))

    # 2. Uplink ports: another switch is visible, or too many MACs
    uplink_ports: dict[str, set[int]] = {}
    for sw in switches:
        uplink_ports[sw.ip] = set(switches_on_port[sw.ip])
        for if_index, count in Counter(link_fdb[sw.ip].values()).items():
            if count > UPLINK_MAC_THRESHOLD:
                uplink_ports[sw.ip].add(if_index)

    # 3. End devices: pick the port with the fewest MAC "neighbors"
    best_location: dict[str, tuple[int, str, int]] = {}  # mac -> (macs_on_port, sw_ip, ifIndex)
    switch_macs = set(mac_to_switch)
    for sw in switches:
        macs_per_port = Counter(fdb[sw.ip].values())
        for mac, if_index in fdb[sw.ip].items():
            if mac in switch_macs or if_index in uplink_ports[sw.ip]:
                continue
            candidate = (macs_per_port[if_index], sw.ip, if_index)
            if mac not in best_location or candidate < best_location[mac]:
                best_location[mac] = candidate

    switch_by_ip = {sw.ip: sw for sw in switches}
    hosts = []
    hosts_per_port: dict[tuple[str, int], list[dict]] = {}
    for mac, (_, sw_ip, if_index) in sorted(best_location.items()):
        sw = switch_by_ip[sw_ip]
        host = {
            "mac": mac,
            "switch": sw_ip,
            "port": _port_name(sw, if_index),
            "vlan": sw.port_pvid.get(if_index, 0),
            "name": "",  # names and IPs are added from the DB (ARP/DNS)
        }
        hosts.append(host)
        hosts_per_port.setdefault((sw_ip, if_index), []).append(host)

    # 4. Many hosts on a non-uplink port — an unmanaged switch behind it
    pseudo_switches: list[dict] = []
    if unmanaged_threshold > 0:
        for (sw_ip, if_index), port_hosts in sorted(hosts_per_port.items()):
            if len(port_hosts) <= unmanaged_threshold:
                continue
            pseudo_id = f"pseudo:{sw_ip}:{if_index}"
            for host in port_hosts:
                host["via"] = pseudo_id
            pseudo_switches.append({
                "id": pseudo_id,
                "switch": sw_ip,
                "port": _port_name(switch_by_ip[sw_ip], if_index),
                "host_count": len(port_hosts),
            })

    switch_dicts = [{
        "ip": sw.ip,
        "name": sw.sys_name or sw.ip,
        "mac": sw.bridge_mac,
        "descr": sw.sys_descr,
        # Physical ports only (ifType 6): aggregates, CPU and VLAN
        # interfaces must not inflate the counters
        "ports_total": sum(1 for p in sw.ports.values() if p.is_physical),
        "ports_up": sum(
            1 for p in sw.ports.values() if p.is_physical and p.oper_up
        ),
    } for sw in switches]

    # VLAN names from all switches (first switch wins on ID conflicts)
    vlan_names: dict[int, str] = {}
    for sw in switches:
        for vlan_id, name in sw.vlan_names.items():
            if name:
                vlan_names.setdefault(vlan_id, name)

    return switch_dicts, links, hosts, pseudo_switches, vlan_names
