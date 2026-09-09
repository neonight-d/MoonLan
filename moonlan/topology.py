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
9. A MAC seen ONLY on trunk ports sits behind equipment nobody polls.
   It is placed on the trunk of the switch that has the best claim to
   it — a downlink before an uplink, the deeper switch before the
   shallower one — and marked approximate, instead of disappearing.

v0.6 adds LLDP on top of steps 3-7. Where two polled switches name
each other over LLDP, the link and both its ports come from LLDP and
carry source="lldp"/"both"; the FDB inference then only fills the gaps
(source="fdb"). Ports carrying forwarded LLDP frames are excluded —
see lldp.forwarded_ports. Disagreements between the two sources are
collected in info["lldp_mismatches"] and printed by `diag --topology`
rather than silently resolved.
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

UNMANAGED_THRESHOLD = 3  # hosts per port; more — draw a pseudo-switch


@dataclass
class TopologyState:
    """Thread-safe storage of the current topology."""

    switches: list[dict] = field(default_factory=list)
    links: list[dict] = field(default_factory=list)
    hosts: list[dict] = field(default_factory=list)
    pseudo_switches: list[dict] = field(default_factory=list)
    # switches LLDP found behind our ports that nobody polls
    bridges: list[dict] = field(default_factory=list)
    vlan_names: dict[int, str] = field(default_factory=dict)
    # known devices that sit on no port of a polled switch: they are
    # inventory and search results, but nothing is drawn for them
    unlocated: list[dict] = field(default_factory=list)
    # one node per port whose devices are all offline right now
    offline_groups: list[dict] = field(default_factory=list)
    # spanning tree: the per-switch report and the network verdict
    stp: dict = field(default_factory=dict)
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
        unlocated: list[dict] | None = None,
        offline_groups: list[dict] | None = None,
        bridges: list[dict] | None = None,
        stp: dict | None = None,
    ) -> None:
        with self._lock:
            self.switches = switches
            self.links = links
            self.hosts = hosts
            self.pseudo_switches = pseudo_switches
            self.vlan_names = vlan_names
            self.unlocated = unlocated or []
            self.offline_groups = offline_groups or []
            self.bridges = bridges or []
            self.stp = stp or {}
            self.last_scan = time.time()

    def as_dict(self) -> dict:
        with self._lock:
            return {
                "switches": self.switches,
                "links": self.links,
                "hosts": self.hosts,
                "pseudo_switches": self.pseudo_switches,
                "bridges": self.bridges,
                "vlan_names": self.vlan_names,
                "unlocated": self.unlocated,
                "offline_groups": self.offline_groups,
                "stp": self.stp,
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
            for host in self.hosts + self.unlocated:
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


def port_name(sw: SwitchData, if_index: int) -> str:
    port = sw.ports.get(if_index)
    return port.name if port and port.name else str(if_index)


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


def _lag_info(sw: SwitchData, port: int) -> tuple[list[str], list[bool], int]:
    """The aggregate's physical members: names, oper states and the
    total speed of the ACTIVE members only — a degraded LAG must not
    pretend to have its full capacity.

    A positive port is an IEEE8023-LAG-MIB aggregate ifIndex; a negative
    one is a synthetic bridge-port whose members were inferred from the
    dot1dBasePortIfIndex gaps (SwitchData.lag_groups).
    """
    if port < 0:
        members = sorted(sw.lag_groups.get(-port, []))
    else:
        members = sorted(m for m, agg in sw.lag_members.items() if agg == port)
    names = [port_name(sw, m) for m in members]
    states = [bool(sw.ports[m].oper_up) if m in sw.ports else False
              for m in members]
    speed = sum(
        sw.ports[m].speed_mbps
        for m, up in zip(members, states) if up and m in sw.ports
    )
    return names, states, speed


def _make_link(
    a: SwitchData, pa: int | None, b: SwitchData, pb: int | None,
    source: str = "fdb",
) -> dict:
    """Link A(pA)—B(pB); a None port means that side is unknown ("?").

    `source` says where the link came from: "fdb" — inferred from the
    MAC tables, "lldp" — the two devices told us about each other,
    "both" — inferred and then confirmed. A reader deserves to know
    which of the two it is looking at.
    """
    link = {
        "a": a.ip,
        "b": b.ip,
        "a_port": port_name(a, pa) if pa is not None else "?",
        "b_port": port_name(b, pb) if pb is not None else "?",
        "speed_mbps": 0,
        "lag": None,
        "source": source,
    }
    a_members, a_states, a_speed = (
        _lag_info(a, pa) if pa is not None else ([], [], 0)
    )
    b_members, b_states, b_speed = (
        _lag_info(b, pb) if pb is not None else ([], [], 0)
    )
    if a_members or b_members:
        members = a_members or b_members
        # traffic flows only through members that are up on both ends;
        # with one side unknown, trust the side we can see
        active_counts = [sum(s) for m, s in ((a_members, a_states),
                                             (b_members, b_states)) if m]
        link["lag"] = {
            "members": members,
            "count": len(members),
            "active": min(active_counts),
            "a_members": a_members,
            "b_members": b_members,
            "a_states": a_states,
            "b_states": b_states,
        }
        # the slower side limits the aggregate when both are known;
        # speeds already count only the active members
        if a_members and b_members:
            link["speed_mbps"] = min(a_speed, b_speed)
        else:
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


def resolve_remote_port(
    sw: SwitchData, port_id: str, port_desc: str
) -> tuple[int | None, str]:
    """A neighbour's port id, as OUR port table knows it.

    LLDP reports the far end's port the way that device names it
    (ifName, a port MAC, or a private "local" string). When it can be
    matched to a real interface of the polled switch, the ifIndex comes
    with it — links then get speeds and LAG composition. Otherwise the
    raw string is kept: naming the port wrongly is worse than quoting
    the neighbour.
    """
    for candidate in (port_id, port_desc):
        key = (candidate or "").strip().lower()
        if not key:
            continue
        for port in sw.ports.values():
            if port.if_index < 0:
                continue
            if key in {
                (port.name or "").lower(), str(port.if_index), port.mac
            }:
                return port.if_index, port.name or str(port.if_index)
    return None, (port_id or port_desc or "?")


def lldp_link_candidates(
    switches: list[SwitchData],
) -> dict[frozenset, dict]:
    """Switch-to-switch links CONFIRMED by LLDP, keyed by the pair.

    A candidate needs one LLDP neighbour on the local port whose chassis
    id belongs to another polled switch. Ports flagged by
    `lldp.forwarded_ports` are skipped: on a switch that re-transmits
    foreign LLDP frames the neighbour is not on the port it claims.

    Each side's own end comes from its own local port table, which is
    the reliable half of the exchange; the far end is resolved from
    lldpRemPortId and only falls back to the raw string.
    """
    mac_to_switch: dict[str, SwitchData] = {}
    for sw in switches:
        for mac in sw.own_macs | ({sw.bridge_mac} if sw.bridge_mac else set()):
            mac_to_switch.setdefault(mac, sw)

    candidates: dict[frozenset, dict] = {}
    for sw in switches:
        for neighbor in sw.lldp_neighbors:
            if neighbor.local_ifindex is None:
                continue
            if neighbor.local_ifindex in sw.lldp_forwarded:
                continue
            other = mac_to_switch.get(neighbor.chassis_id)
            if other is None or other.ip == sw.ip:
                continue
            key = frozenset({sw.ip, other.ip})
            entry = candidates.setdefault(key, {})
            entry[sw.ip] = {
                "if_index": neighbor.local_ifindex,
                "name": port_name(sw, neighbor.local_ifindex),
            }
            # what this switch says the far end's port is — used only
            # when the far end does not report the link itself
            remote_index, remote_name = resolve_remote_port(
                other, neighbor.port_id, neighbor.port_desc
            )
            entry.setdefault(
                "remote:" + other.ip,
                {"if_index": remote_index, "name": remote_name},
            )
    resolved: dict[frozenset, dict] = {}
    for key, entry in candidates.items():
        sides = {}
        for ip in key:
            sides[ip] = entry.get(ip) or entry.get("remote:" + ip)
        if all(sides.values()):
            resolved[key] = sides
    return resolved


def detect_bridges(
    switches: list[SwitchData],
    own_macs: set[str],
    trunks: dict[str, dict[int, str]],
) -> list[dict]:
    """LLDP neighbours that are switches nobody polls.

    A neighbour with the `bridge` capability whose chassis id belongs to
    no polled switch is a bridge living behind one of our ports — the
    six MikroTik bridges the September survey found, none of which
    answers SNMP. Until now the map could only show them as an
    anonymous "switch without SNMP", or not at all.

    A neighbour that sends no capabilities TLV at all (D-Link DES-3526
    ships with System Name / Description / Capabilities disabled) is
    kept as an unidentified LLDP device: the ABSENCE of the bridge bit
    proves nothing, so it is shown and never alarmed on.

    One node per chassis id: a switch that forwards foreign LLDP frames
    would otherwise scatter the same device over several ports. The
    placement with the strongest claim wins — an untainted port before
    a forwarded one, an access port before a trunk.
    """
    best: dict[str, tuple] = {}
    found: dict[str, dict] = {}
    for sw in switches:
        for neighbor in sw.lldp_neighbors:
            if neighbor.local_ifindex is None:
                continue
            if neighbor.chassis_id in own_macs:
                continue  # a switch we poll ourselves — already a node
            if neighbor.cap_known and not neighbor.is_bridge:
                continue  # a host, a phone, a router: not a bridge
            if_index = neighbor.local_ifindex
            forwarded = if_index in sw.lldp_forwarded
            is_trunk = aggregate_port(sw, if_index) in trunks.get(sw.ip, {})
            claim = (forwarded, is_trunk, sw.ip, if_index)
            if neighbor.chassis_id in best and claim >= best[neighbor.chassis_id]:
                continue
            best[neighbor.chassis_id] = claim
            found[neighbor.chassis_id] = {
                "id": "bridge:" + neighbor.chassis_id,
                "chassis_id": neighbor.chassis_id,
                "name": neighbor.sys_name or neighbor.chassis_id,
                "sys_desc": neighbor.sys_desc,
                "mgmt_ip": neighbor.mgmt_ip,
                "switch": sw.ip,
                "port": port_name(sw, if_index),
                "remote_port": neighbor.port_id or neighbor.port_desc,
                "capabilities": sorted(neighbor.cap_enabled),
                # no capabilities TLV: the device is shown, but nothing
                # is claimed about what it is
                "unidentified": not neighbor.cap_known,
                "lldp_forwarded": forwarded,
                "trunk": is_trunk,
            }
    return [found[chassis] for chassis in sorted(found)]


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
                parent.ip, port_name(parent, port),
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


def merge_lldp_links(
    links: list[dict],
    lldp_pairs: dict[frozenset, dict],
    by_ip: dict[str, SwitchData],
) -> list[dict]:
    """Applies LLDP to the tree the MAC tables produced.

    LLDP wins where the two disagree: it is a direct statement by both
    devices, while the FDB tree is an inference. But the disagreement
    is never swallowed — every difference is returned so the operator
    can see it in `diag --topology` and in the log.
    """
    mismatches: list[dict] = []
    matched: set[frozenset] = set()
    for link in links:
        key = frozenset({link["a"], link["b"]})
        sides = lldp_pairs.get(key)
        if sides is None:
            link.setdefault("source", "fdb")
            continue
        matched.add(key)
        link["source"] = "both"
        was = (link["a_port"], link["b_port"])
        now = (sides[link["a"]]["name"], sides[link["b"]]["name"])
        if was != now:
            mismatches.append({
                "kind": "ports",
                "a": link["a"], "b": link["b"],
                "fdb_ports": was, "lldp_ports": now,
            })
        link["a_port"], link["b_port"] = now
    for key, sides in sorted(lldp_pairs.items(), key=lambda kv: sorted(kv[0])):
        if key in matched:
            continue
        a_ip, b_ip = sorted(key)
        a, b = by_ip.get(a_ip), by_ip.get(b_ip)
        if a is None or b is None:
            continue
        # the aggregate, when the LLDP port is a LACP member: the link
        # then carries the members and the real capacity
        pa = sides[a_ip]["if_index"]
        pb = sides[b_ip]["if_index"]
        link = _make_link(
            a, aggregate_port(a, pa), b, aggregate_port(b, pb),
            source="lldp",
        )
        link["a_port"] = sides[a_ip]["name"]
        link["b_port"] = sides[b_ip]["name"]
        links.append(link)
        mismatches.append({
            "kind": "missing_in_fdb",
            "a": a_ip, "b": b_ip,
            "fdb_ports": None,
            "lldp_ports": (link["a_port"], link["b_port"]),
        })
    for m in mismatches:
        if m["kind"] == "ports":
            log.info(
                "LLDP vs FDB: %s—%s is on %s/%s per LLDP, the MAC tables "
                "suggested %s/%s — LLDP wins",
                m["a"], m["b"], *m["lldp_ports"], *m["fdb_ports"],
            )
        else:
            log.info(
                "LLDP vs FDB: %s [%s] — %s [%s] is reported by LLDP only; "
                "the MAC tables do not show this link",
                m["a"], m["lldp_ports"][0], m["b"], m["lldp_ports"][1],
            )
    return mismatches


def trunk_ports(
    switches: list[SwitchData],
    switches_on_port: dict[str, dict[int, set[str]]],
    uplinks: dict[str, int | None],
    lldp_pairs: dict[frozenset, dict] | None = None,
) -> dict[str, dict[int, str]]:
    """Ports leading to other switches: ip -> {ifIndex: why it is a trunk}.

    A port is a trunk only when it structurally is one — another polled
    switch is visible on it, or the tree inference chose it as the
    uplink toward the root. Until v0.5.7 a port with more than eight
    MACs behind it also counted, a leftover from the naive v0.1 link
    algorithm. Trunks are excluded from host binding and from
    pseudo-switch detection, so on a real network that heuristic swept
    every device behind an unmanaged switch off the map and into the
    "not on map" list. Many devices on one port is exactly what
    unmanaged_threshold is for.
    """
    trunks: dict[str, dict[int, str]] = {}
    for sw in switches:
        ports: dict[int, str] = {}
        for if_index, neighbors in switches_on_port.get(sw.ip, {}).items():
            ports[if_index] = "sees " + ", ".join(sorted(neighbors))
        uplink = uplinks.get(sw.ip)
        if uplink is not None:
            ports.setdefault(uplink, "uplink toward the root")
        # A port LLDP confirms as a switch-to-switch link is a trunk
        # even when the MAC tables never showed the neighbour there —
        # one-way visibility is exactly the case LLDP was added for
        for key, sides in (lldp_pairs or {}).items():
            if sw.ip not in key:
                continue
            other = next(ip for ip in key if ip != sw.ip)
            if_index = sides[sw.ip]["if_index"]
            if if_index is None:
                continue
            ports.setdefault(
                aggregate_port(sw, if_index), f"LLDP neighbour {other}"
            )
        trunks[sw.ip] = ports
    return trunks


def tree_depth(root: str | None, links: list[dict]) -> dict[str, int]:
    """Hops from the root to every switch of the inferred tree."""
    if root is None:
        return {}
    neighbors: dict[str, list[str]] = {}
    for link in links:
        neighbors.setdefault(link["a"], []).append(link["b"])
        neighbors.setdefault(link["b"], []).append(link["a"])
    depth = {root: 0}
    queue = [root]
    while queue:
        current = queue.pop(0)
        for other in neighbors.get(current, []):
            if other not in depth:
                depth[other] = depth[current] + 1
                queue.append(other)
    return depth


def build_topology(
    collected: Iterable[SwitchData],
    unmanaged_threshold: int = UNMANAGED_THRESHOLD,
    fdb_stability: FdbStability | None = None,
    known_hosts_per_port: dict[tuple[str, str], int] | None = None,
    sticky_pseudo_ports: set[tuple[str, str]] | None = None,
    unconfirmed_macs: set[str] | None = None,
    place_trunk_only: bool = True,
) -> tuple[
    list[dict], list[dict], list[dict], list[dict], dict[int, str],
    list[dict], dict,
]:
    """Turns poll data into nodes and links for the map.

    known_hosts_per_port ((switch ip, port name) -> count) is how many
    devices the database knows behind a port, stale ones inside the
    grace window included; the caller computes it. Without it the
    unmanaged-switch threshold would only see this poll's FDB, which
    ages out in minutes, and pseudo-switch groups would form and
    dissolve from scan to scan. sticky_pseudo_ports are ports that had
    a pseudo node last time: they keep it while at least one device is
    still answering there.

    unconfirmed_macs are addresses too new to count as devices: they
    are still bound to their port (the caller stores them), but marked
    and left out of the per-port device counts.
    """
    known_hosts_per_port = known_hosts_per_port or {}
    sticky_pseudo_ports = sticky_pseudo_ports or set()
    unconfirmed_macs = unconfirmed_macs or set()
    switches = [sw for sw in collected if sw.reachable]
    fdb = normalized_fdb(switches)

    # For links and uplinks — FDB merged with previous polls (protection
    # against aging); host binding below uses the fresh fdb
    if fdb_stability is not None:
        link_fdb = {sw.ip: fdb_stability.merge(sw.ip, fdb[sw.ip]) for sw in switches}
    else:
        link_fdb = fdb

    switches_on_port, sees = switch_sightings(switches, link_fdb)
    switch_by_ip = {sw.ip: sw for sw in switches}

    # 1. Switch-to-switch links: a tree grown from the root, then
    #    corrected where LLDP has the two devices naming each other.
    #    LLDP is a statement, the FDB tree an inference — but the
    #    disagreements are reported, never quietly "fixed".
    links, uplinks, info = infer_tree(switches, switches_on_port, sees)
    lldp_pairs = lldp_link_candidates(switches)
    info["lldp_mismatches"] = merge_lldp_links(links, lldp_pairs, switch_by_ip)
    info["lldp_forwarded"] = {
        sw.ip: sorted(port_name(sw, i) for i in sw.lldp_forwarded)
        for sw in switches if sw.lldp_forwarded
    }

    # 2. Trunk ports: they lead to other switches and carry no hosts
    trunks = trunk_ports(switches, switches_on_port, uplinks, lldp_pairs)
    uplink_ports: dict[str, set[int]] = {
        ip: set(ports) for ip, ports in trunks.items()
    }
    for ip, ports in sorted(trunks.items()):
        # one line per poll: a device silently vanishing from the map
        # is almost always a port that became a trunk by mistake
        log.debug(
            "%s trunk ports: %s", ip,
            ", ".join(
                f"{port_name(switch_by_ip[ip], i)} ({why})"
                for i, why in sorted(ports.items())
            ) or "none",
        )

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

    # 3a. Devices every switch sees only through a trunk: they hang off
    # equipment nobody polls, and there is no port of ours to bind them
    # to. Dropping them left real, pinging devices in the "not on map"
    # list; instead they are placed on the trunk with the best claim —
    # a downlink before an uplink, the deeper switch before the
    # shallower one — and flagged as approximate.
    approximate: set[str] = set()
    if place_trunk_only:
        depth = tree_depth(info["root"], links)
        claims: dict[str, tuple] = {}
        for sw in switches:
            for mac, if_index in fdb[sw.ip].items():
                if mac in switch_macs or mac in best_location:
                    continue
                if if_index not in uplink_ports[sw.ip]:
                    continue
                downlink = if_index != uplinks.get(sw.ip)
                claim = (downlink, depth.get(sw.ip, 0), sw.ip, if_index)
                if mac not in claims or claim > claims[mac]:
                    claims[mac] = claim
        for mac, (_downlink, _depth, sw_ip, if_index) in claims.items():
            best_location[mac] = (0, sw_ip, if_index)
            approximate.add(mac)
        if approximate:
            log.debug(
                "%d devices are visible only through trunks and were placed "
                "approximately", len(approximate),
            )

    hosts = []
    # keyed by port NAME, the same key the database and the caller use
    hosts_per_port: dict[tuple[str, str], list[dict]] = {}
    for mac, (_, sw_ip, if_index) in sorted(best_location.items()):
        sw = switch_by_ip[sw_ip]
        host = {
            "mac": mac,
            "switch": sw_ip,
            "port": port_name(sw, if_index),
            "vlan": sw.port_pvid.get(if_index, 0),
            "name": "",  # names and IPs are added from the DB (ARP/DNS)
        }
        if mac in approximate:
            host["approximate"] = True
        hosts.append(host)
        if mac in unconfirmed_macs:
            host["unconfirmed"] = True
        if mac in approximate or mac in unconfirmed_macs:
            # neither votes on whether a switch hides behind the port:
            # one is not a device yet, the other is not really there
            continue
        hosts_per_port.setdefault((sw_ip, host["port"]), []).append(host)

    # 4. Many devices on a non-trunk port — an unmanaged switch behind
    # it. The count is the larger of what this poll sees and what the
    # database knows about the port, so a group does not dissolve when
    # half its devices go quiet and their MACs age out of the FDB. A
    # port where nothing answers at all is a different story: those
    # devices are only remembered, not seen, and the caller groups them
    # as an offline "temporary location" instead.
    pseudo_switches: list[dict] = []
    if unmanaged_threshold > 0:
        uplink_names = {
            sw.ip: {port_name(sw, i) for i in uplink_ports[sw.ip]}
            for sw in switches
        }
        candidates = set(hosts_per_port) | {
            key for key in known_hosts_per_port if key[0] in switch_by_ip
        }
        for sw_ip, port in sorted(candidates):
            if port in uplink_names.get(sw_ip, set()):
                continue  # a trunk, whatever the database remembers
            port_hosts = hosts_per_port.get((sw_ip, port), [])
            if not port_hosts:
                # Nothing is confirmed behind this port right now, so
                # there is no evidence of a switch: the caller draws
                # the devices as an offline group instead
                continue
            known = known_hosts_per_port.get((sw_ip, port), 0)
            total = max(len(port_hosts), known)
            sticky = (sw_ip, port) in sticky_pseudo_ports
            if total <= unmanaged_threshold and not sticky:
                continue
            pseudo_id = f"pseudo:{sw_ip}:{port}"
            for host in port_hosts:
                host["via"] = pseudo_id
            pseudo_switches.append({
                "id": pseudo_id,
                "switch": sw_ip,
                "port": port,
                "host_count": total,
                "host_count_live": len(port_hosts),
                "host_count_known": known,
            })

    # 5. Bridges nobody polls, named by LLDP.
    #
    #    One bridge behind a port: it IS the box on that cable, so it
    #    replaces the anonymous pseudo-switch there and inherits its
    #    devices — one node with a name beats two without.
    #
    #    Several bridges behind one port: an unmanaged box sits on the
    #    cable and the bridges hang off it (mb1 port 28 is a chain of
    #    Garage -> Workshop -> 10.3.6.124). The pseudo-switch keeps its
    #    devices and the bridges are drawn as separate nodes on the same
    #    port. Replacing it with one of them would claim the other
    #    bridges' devices belong to whichever bridge happened to sort
    #    first.
    bridges = detect_bridges(switches, switch_macs, trunks)
    bridges_on_port: dict[tuple[str, str], list[dict]] = {}
    for bridge in bridges:
        bridges_on_port.setdefault(
            (bridge["switch"], bridge["port"]), []
        ).append(bridge)
    pseudo_by_port = {(p["switch"], p["port"]): p for p in pseudo_switches}
    hosts_on_port: dict[tuple[str, str], list[dict]] = {}
    for host in hosts:
        hosts_on_port.setdefault((host["switch"], host["port"]), []).append(host)
    for key, group in bridges_on_port.items():
        port_hosts = hosts_on_port.get(key, [])
        alone = len(group) == 1 and not group[0]["trunk"]
        replaced = pseudo_by_port.pop(key, None) if alone else None
        if replaced is not None and replaced in pseudo_switches:
            pseudo_switches.remove(replaced)
        for bridge in group:
            if replaced is not None:
                bridge["host_count"] = replaced["host_count"]
                bridge["host_count_live"] = replaced["host_count_live"]
            if bridge["trunk"]:
                continue  # a trunk: its devices belong to the switch behind it
            if alone:
                for host in port_hosts:
                    host["via"] = bridge["id"]
            bridge.setdefault("host_count", len(port_hosts) if alone else 0)
            # several bridges share the port: their devices stay on the
            # pseudo-switch, because nothing says which bridge they are
            # behind
            bridge["shares_port"] = not alone
    if bridges:
        log.info(
            "LLDP found %d device(s) behind our ports that we do not "
            "poll: %s", len(bridges),
            "; ".join(
                f"{b['name']} on {b['switch']} {b['port']}"
                + (" (unidentified)" if b["unidentified"] else "")
                for b in bridges
            ),
        )
    info["bridges"] = bridges

    # 6. Spanning tree on the map: a port STP holds in discarding
    #    carries no traffic, and the root bridge is worth seeing at a
    #    glance. Only switches whose tree actually operates count — a
    #    disabled one still answers dot1dStp* and names itself root.
    for sw in switches:
        if sw.stp is None or not sw.stp.operating:
            continue
        blocking = {p.name for p in sw.stp.blocking_ports() if p.name}
        if not blocking:
            continue
        for link in links:
            for side in ("a", "b"):
                if link[side] == sw.ip and link[f"{side}_port"] in blocking:
                    link["stp_blocking"] = True
                    link["stp_blocking_side"] = sw.ip

    switch_dicts = [{
        "ip": sw.ip,
        "name": sw.sys_name or sw.ip,
        "mac": sw.bridge_mac,
        "descr": sw.sys_descr,
        "stp_operating": bool(sw.stp and sw.stp.operating),
        "stp_root": bool(sw.stp and sw.stp.is_root(sw.own_macs)),
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

    return (
        switch_dicts, links, hosts, pseudo_switches, vlan_names,
        bridges, info,
    )
