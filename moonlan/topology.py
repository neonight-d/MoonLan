"""Building the network topology from data collected from switches.

Algorithm (v0.4.4) — a tree grown from the root:

1. LACP: the ifIndex of every physical member port is mapped to the
   aggregate's ifIndex, so the aggregate takes part in all computations
   (links, uplinks, host binding) as a single logical port.
2. A switch is recognized in neighbors' FDB tables by any MAC from its
   own_macs set (bridge base MAC, interface MACs, management-IP MAC),
   not only by dot1dBaseBridgeAddress: real frames are sent from
   interface MACs.
3. Root selection: the switch that sees the largest number of other
   switches in its FDB; ties are broken by FDB size. In practice this
   is the core of the star — it sees everyone, while access switches
   often do not see the core at all (one-way visibility is a permanent
   property of some networks, not a glitch).
4. Branch split: every switch X != root belongs to the branch of the
   root port where the root sees X. A single-switch branch produces a
   direct link root—X. Inside a multi-switch branch the switch nearest
   to the root is the one that sees the other members on ports other
   than its own uplink; it becomes the sub-root for the rest. If the
   order cannot be determined, all members are linked to the root
   directly with a WARNING.
5. Links between different branches are FORBIDDEN — this invariant
   removes false ray-to-ray links (a ray can see stray MACs of another
   ray through the core) and the flicker they cause.
6. Uplink of X: the port where X sees own_macs of anyone OUTSIDE its
   own branch (the root and other branches are all visible only through
   the uplink). It becomes b_port of the link to X ("?" if X sees
   nobody). Uplink and trunk ports are excluded from host binding.
7. A switch the root does not see is attached through its own
   sightings (of the root or of a branch member); if it sees nobody,
   it is left unlinked with a WARNING.
8. End devices. MACs on non-trunk ports are hosts; a host is bound to
   the port with the fewest other MACs. More than unmanaged_threshold
   hosts on one port become a "switch without SNMP" pseudo node.
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


def _make_link(
    a: SwitchData, pa: int | None, b: SwitchData, pb: int | None
) -> dict:
    """Link A(pA)—B(pB); a None port means that side is unknown ("?")."""
    link = {
        "a": a.ip,
        "b": b.ip,
        "a_port": _port_name(a, pa) if pa is not None else "?",
        "b_port": _port_name(b, pb) if pb is not None else "?",
        "speed_mbps": 0,
        "lag": None,
    }
    a_members, a_speed = _lag_info(a, pa) if pa is not None else ([], 0)
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
        port = None
        if pa is not None:
            port = a.ports.get(pa)
        if port is None and pb is not None:
            port = b.ports.get(pb)
        link["speed_mbps"] = port.speed_mbps if port else 0
    # A synthetic bridge-port (negative ifIndex, absent from
    # dot1dBasePortIfIndex) on either side means a LAG trunk on switches
    # that do not expose IEEE8023-LAG-MIB (e.g. D-Link)
    if (pa is not None and pa < 0) or (pb is not None and pb < 0):
        if link["lag"] is None:
            link["lag"] = {"trunk": True}
        else:
            link["lag"]["trunk"] = True
    return link


def normalized_fdb(switches: list[SwitchData]) -> dict[str, dict[str, int]]:
    """FDB per switch with LACP member ports mapped to the aggregate."""
    return {
        sw.ip: {
            mac: sw.lag_members.get(if_index, if_index)
            for mac, if_index in sw.fdb.items()
        }
        for sw in switches
    }


def switch_sightings(
    switches: list[SwitchData], fdb: dict[str, dict[str, int]]
) -> tuple[dict[str, dict[int, set[str]]], dict[str, dict[str, int]]]:
    """Who sees whom and where.

    Returns (switches_on_port, sees):
    switches_on_port[A][p] — set of switch IPs visible on A's port p;
    sees[A][B] — the port where A sees B (the most frequent one if several).
    """
    # Any MAC from own_macs identifies the switch; bridge_mac is a
    # fallback in case own_macs is empty (e.g. old data)
    mac_to_switch: dict[str, SwitchData] = {}
    for sw in switches:
        for mac in sw.own_macs | ({sw.bridge_mac} if sw.bridge_mac else set()):
            mac_to_switch.setdefault(mac, sw)

    switches_on_port: dict[str, dict[int, set[str]]] = {}
    sees: dict[str, dict[str, int]] = {}
    for sw in switches:
        per_port: dict[int, set[str]] = {}
        sightings: dict[str, Counter] = {}
        for mac, if_index in fdb[sw.ip].items():
            neighbor = mac_to_switch.get(mac)
            if neighbor is None or neighbor.ip == sw.ip:
                continue
            per_port.setdefault(if_index, set()).add(neighbor.ip)
            sightings.setdefault(neighbor.ip, Counter())[if_index] += 1
        switches_on_port[sw.ip] = per_port
        sees[sw.ip] = {
            ip: counts.most_common(1)[0][0] for ip, counts in sightings.items()
        }
    return switches_on_port, sees


def infer_tree(
    switches: list[SwitchData],
    switches_on_port: dict[str, dict[int, set[str]]],
    sees: dict[str, dict[str, int]],
) -> tuple[list[dict], dict[str, int | None], dict]:
    """Root-based tree inference of switch-to-switch links.

    Returns (links, uplinks, info). uplinks[ip] is the computed uplink
    port of every non-root switch (None if unknown). info holds the
    intermediate picture for diagnostics: the root, the branch split by
    root port and the list of switches left unplaced.
    """
    if not switches:
        return [], {}, {"root": None, "branches": {}, "unplaced": []}

    by_ip = {sw.ip: sw for sw in switches}

    # Root: sees the most other switches; ties broken by FDB size
    root = max(
        switches,
        key=lambda sw: (len(sees[sw.ip]), len(sw.fdb), sw.ip),
    )
    log.info(
        "Topology root: %s (%s), sees %d of %d switches",
        root.sys_name or root.ip, root.ip,
        len(sees[root.ip]), len(switches) - 1,
    )

    # Branch split: everything the root sees on one port is one subtree
    branches: dict[int, list[SwitchData]] = {}
    unseen: list[SwitchData] = []
    for sw in switches:
        if sw is root:
            continue
        port = sees[root.ip].get(sw.ip)
        if port is None:
            unseen.append(sw)
        else:
            branches.setdefault(port, []).append(sw)
    branch_of = {
        member.ip: port
        for port, members in branches.items()
        for member in members
    }

    # Uplink of X: the port where X sees switches outside its own branch
    # (the root and other branches are only reachable through the uplink)
    def uplink_of(sw: SwitchData) -> int | None:
        inside = {
            m.ip for m in branches.get(branch_of.get(sw.ip), [])
        }
        counts: Counter = Counter()
        for other_ip, port in sees[sw.ip].items():
            if other_ip not in inside:
                counts[port] += 1
        return counts.most_common(1)[0][0] if counts else None

    uplinks: dict[str, int | None] = {
        sw.ip: uplink_of(sw) for sw in switches if sw is not root
    }

    links: list[dict] = []

    def child_port(parent: SwitchData, child: SwitchData) -> int | None:
        """The child's port toward the parent: direct sighting, else uplink."""
        direct = sees[child.ip].get(parent.ip)
        return direct if direct is not None else uplinks.get(child.ip)

    def attach(parent: SwitchData, port: int, members: list[SwitchData]) -> None:
        """Links members (one branch behind the parent's port) to the tree."""
        if len(members) == 1:
            child = members[0]
            links.append(_make_link(parent, port, child, child_port(parent, child)))
            return
        # The member nearest to the parent sees all the other members
        # on ports different from its own uplink
        def is_nearest(c: SwitchData) -> bool:
            up = uplinks.get(c.ip)
            return all(
                sees[c.ip].get(m.ip) is not None and sees[c.ip][m.ip] != up
                for m in members
                if m is not c
            )

        candidates = [c for c in members if is_nearest(c)]
        if len(candidates) > 1:
            # Symmetric sightings pass the test vacuously when uplinks are
            # unknown; trust only candidates with a known uplink
            strong = [c for c in candidates if uplinks.get(c.ip) is not None]
            candidates = strong
        if not candidates:
            log.warning(
                "Branch order behind %s port %s is undetermined (%s); "
                "connecting all members to %s directly",
                parent.ip, _port_name(parent, port),
                ", ".join(m.ip for m in members), parent.ip,
            )
            for child in members:
                links.append(
                    _make_link(parent, port, child, child_port(parent, child))
                )
            return
        nearest = max(candidates, key=lambda c: (len(sees[c.ip]), c.ip))
        links.append(_make_link(parent, port, nearest, child_port(parent, nearest)))
        rest = [m for m in members if m is not nearest]
        subgroups: dict[int, list[SwitchData]] = {}
        stranded: list[SwitchData] = []
        for m in rest:
            p = sees[nearest.ip].get(m.ip)
            if p is None:
                stranded.append(m)
            else:
                subgroups.setdefault(p, []).append(m)
        for p, group in sorted(subgroups.items()):
            attach(nearest, p, group)
        for m in stranded:
            log.warning(
                "%s belongs to the branch of %s but is not visible from it; "
                "connecting to %s directly",
                m.ip, nearest.ip, parent.ip,
            )
            links.append(_make_link(parent, port, m, child_port(parent, m)))

    for port, members in sorted(branches.items()):
        attach(root, port, members)

    # Switches the root does not see: attach through their own sightings
    for sw in unseen:
        port_to_root = sees[sw.ip].get(root.ip)
        if port_to_root is not None:
            links.append(_make_link(root, None, sw, port_to_root))
            continue
        anchors = [
            (ip, port) for ip, port in sorted(sees[sw.ip].items()) if ip in by_ip
        ]
        if anchors:
            anchor_ip, port = anchors[0]
            log.info(
                "%s is not visible from the root; attached via its own "
                "sighting of %s", sw.ip, anchor_ip,
            )
            links.append(_make_link(by_ip[anchor_ip], None, sw, port))
        else:
            log.warning(
                "Switch %s is not visible from the root and sees no other "
                "switches — left unlinked", sw.ip,
            )

    info = {
        "root": root.ip,
        "branches": {port: [m.ip for m in members] for port, members in branches.items()},
        "unplaced": [sw.ip for sw in unseen],
    }
    return links, uplinks, info


def build_topology(
    collected: Iterable[SwitchData],
    unmanaged_threshold: int = UNMANAGED_THRESHOLD,
    fdb_stability: FdbStability | None = None,
) -> tuple[list[dict], list[dict], list[dict], list[dict], dict[int, str]]:
    """Turns poll data into nodes and links for the map."""
    switches = [sw for sw in collected if sw.reachable]
    fdb = normalized_fdb(switches)

    # For links and uplinks — FDB merged with previous polls (protection
    # against aging); host binding below uses the fresh fdb
    if fdb_stability is not None:
        link_fdb = {sw.ip: fdb_stability.merge(sw.ip, fdb[sw.ip]) for sw in switches}
    else:
        link_fdb = fdb

    switches_on_port, sees = switch_sightings(switches, link_fdb)

    # 1. Switch-to-switch links: a tree grown from the root
    links, _uplinks, _info = infer_tree(switches, switches_on_port, sees)

    # 2. Trunk ports: any port with another switch behind it, or with
    # too many MACs; they are excluded from host binding
    uplink_ports: dict[str, set[int]] = {}
    for sw in switches:
        uplink_ports[sw.ip] = set(switches_on_port[sw.ip])
        for if_index, count in Counter(link_fdb[sw.ip].values()).items():
            if count > UPLINK_MAC_THRESHOLD:
                uplink_ports[sw.ip].add(if_index)

    # 3. End devices: pick the port with the fewest MAC "neighbors"
    mac_to_switch: dict[str, SwitchData] = {}
    for sw in switches:
        for mac in sw.own_macs | ({sw.bridge_mac} if sw.bridge_mac else set()):
            mac_to_switch.setdefault(mac, sw)
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

    # 4. Many hosts on a non-trunk port — an unmanaged switch behind it
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
        # Physical ports only (ifType 6/62/69/117): aggregates, CPU and
        # VLAN interfaces must not inflate the counters
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
