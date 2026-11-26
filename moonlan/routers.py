"""What a router in `routers:` says about itself (v0.8.11).

Until v0.8.11 MoonLan read one table from a router: ARP. A router's own
addresses are not in its own ARP table, so its own MACs came into the
inventory as bare addresses without an IP. A router moved from a single
port onto a LACP pair showed what that costs:

- the bond's MAC, the source of all its traffic in 38 VLANs, was two
  bits away from the MAC of the bond's second member, confirmed a few
  days before; with no IP to vouch for it, it was taken for a damaged
  copy and held off the map;
- the second member's MAC was drawn as a bare host, and the operator
  named it after the router;
- LLDP reported the router under a chassis ID that was the MAC of the
  port it had left, so its name reached no host;
- the old port's MAC stayed grey on the old port.

Three more walks per router answer all of it: ifPhysAddress (its MACs),
ifName or ifDescr (what to call them) and ipAdEntIfIndex (the interface
each of its addresses is on); ifType says which interface a MAC is
named after. The result is the router's identity. It is read on every
scan, with the ARP table: a router that does not answer those tables
has an empty one, and everything works as it did, through ARP.

This module knows nothing of SNMP or of the database: the collector
reads the tables, the server decides what the identity changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ifType -> the kind of interface. A MAC is named after the interface
# that owns it: an aggregate before its members (they take its MAC), a
# port before anything else, and a VLAN last — it borrows the MAC of
# the interface it is on. bond1, ether4 and 38 VLANs share one MAC,
# and it is bond1's.
IF_TYPE_KINDS = {
    161: "bond",       # ieee8023adLag
    6: "ethernet",     # ethernetCsmacd
    62: "ethernet",    # fastEther
    69: "ethernet",    # fastEtherFX
    117: "ethernet",   # gigabitEthernet
    135: "vlan",       # l2vlan
    136: "vlan",       # l3ipvlan
}
KIND_RANK = {"bond": 0, "ethernet": 1, "": 2, "vlan": 3}

# how many names of one MAC the card carries in its tooltip
NAMES_SHOWN = 40


@dataclass
class RouterIdentity:
    """One router of `routers:`, as it describes itself."""

    address: str                  # as written in `routers:`
    # MAC -> the names of the interfaces that have it, the one it is
    # called by first
    interfaces: dict[str, list[str]] = field(default_factory=dict)
    # MAC -> the kind of the interface it is called by ("" unknown)
    kinds: dict[str, str] = field(default_factory=dict)
    # each of its own IPs -> the MAC of the interface it is on
    ips: dict[str, str] = field(default_factory=dict)
    # the MAC of the interface `address` is on; "" when not known
    primary: str = ""

    def __bool__(self) -> bool:
        return bool(self.interfaces)

    @property
    def macs(self) -> set[str]:
        return set(self.interfaces)

    def name(self, mac: str) -> str:
        names = self.interfaces.get(mac) or []
        return names[0] if names else ""

    def addresses(self) -> set[str]:
        """Every IP of its own, the one in `routers:` included."""
        return set(self.ips) | {self.address}

    def is_it(self, chassis_id: str, mgmt_ips) -> bool:
        """Is an LLDP neighbour this router: its chassis ID is one of the
        router's MACs, or it announces one of the router's addresses."""
        if not self:
            return False
        if chassis_id and chassis_id in self.interfaces:
            return True
        return bool(set(ip for ip in mgmt_ips or () if ip) & self.addresses())

    def describe(self) -> str:
        """"bond1 02:ee:…, ether5 …" for the log"""
        return ", ".join(
            f"{self.name(mac)} {mac}" for mac in sorted(
                self.interfaces, key=lambda m: (m != self.primary, m)
            )
        )


def identity_from_tables(
    address: str,
    phys: dict[int, str],
    names: dict[int, str],
    types: dict[int, int] | None = None,
    ip_index: dict[str, int] | None = None,
) -> RouterIdentity:
    """The identity out of the router's tables.

    phys is ifIndex -> MAC (ifPhysAddress, the empty and all-zero ones
    already left out), names ifIndex -> ifName, or ifDescr where ifName
    is empty, types ifIndex -> ifType, ip_index each IP -> its ifIndex
    (ipAdEntIfIndex). No MACs — an empty identity.
    """
    types = types or {}
    ip_index = ip_index or {}
    found: dict[str, list[tuple]] = {}
    for if_index, mac in phys.items():
        kind = IF_TYPE_KINDS.get(types.get(if_index, 0), "")
        name = names.get(if_index) or str(if_index)
        found.setdefault(mac, []).append(
            (KIND_RANK[kind], if_index, name, kind)
        )
    identity = RouterIdentity(address=address)
    for mac, rows in found.items():
        rows.sort()
        identity.interfaces[mac] = [row[2] for row in rows]
        identity.kinds[mac] = rows[0][3]
    for ip, if_index in ip_index.items():
        mac = phys.get(if_index)
        if mac:
            identity.ips[ip] = mac
    identity.primary = identity.ips.get(address, "")
    return identity


def own_macs(identities) -> dict[str, RouterIdentity]:
    """MAC -> the router it is an interface of. A MAC two routers claim
    (a shared virtual address) stays with the first by address."""
    owners: dict[str, RouterIdentity] = {}
    for identity in sorted(identities, key=lambda i: i.address):
        for mac in identity.interfaces:
            owners.setdefault(mac, identity)
    return owners


@dataclass
class RouterNode:
    """Where one router is on the map."""

    identity: RouterIdentity
    node_id: str          # "host:<mac>", or the id of a bridge node
    mac: str              # the MAC on the map ("" — none of them)
    switch: str
    port: str
    # own MACs in this scan's tables, and the LLDP neighbour that is
    # this router on the node's port (the raw entry; None — none)
    seen: set[str] = field(default_factory=set)
    lldp: dict | None = None

    @property
    def is_bridge(self) -> bool:
        return not self.node_id.startswith("host:")


@dataclass
class Placement:
    nodes: list[RouterNode] = field(default_factory=list)
    # every own MAC not drawn -> the node it is drawn as
    merged: dict[str, str] = field(default_factory=dict)


def _rank(identity: RouterIdentity, mac: str, *, stale: bool,
          approximate: bool, lldp_here: bool = False,
          last_seen: float = 0.0) -> tuple:
    """Which own MAC is the router's node: one this scan's tables have
    before one only the database remembers, a real port before a guess
    on a trunk, the interface the router's address is on before the
    rest, then one on the port the router's LLDP is heard on — that is
    where its cable is — then the most recent and the lowest."""
    return (
        stale, approximate, mac != identity.primary, not lldp_here,
        -last_seen, mac,
    )


def place(
    identities,
    hosts: list[dict],
    bridges: list[dict],
    neighbours: list[dict],
    remembered: dict[str, dict] | None = None,
) -> Placement:
    """One node per router, and what every other MAC of its is drawn as.

    hosts are this scan's devices as the topology placed them (mac,
    switch, port, approximate) — never on a trunk the switches lead
    to each other through; remembered are records the map draws out of
    the grace window, mac -> {switch, port, last_seen}; bridges and
    neighbours the LLDP entries (bridges, and the rest).

    An LLDP bridge that is the router is its node: the router's MACs
    are drawn as that bridge. Otherwise the node is one of its MACs on
    the map (see _rank), and an LLDP neighbour that is the router, on
    the node's own port, gives the node its LLDP data — whatever its
    chassis ID is. Every other MAC of the router, on that port or
    anywhere else, in this scan or only in the database, is drawn as
    the node and not as a host of its own.

    A router none of whose MACs is on the map gets no node, and its
    MACs are left as they are.
    """
    remembered = remembered or {}
    placement = Placement()
    for identity in sorted(identities, key=lambda i: i.address):
        if not identity:
            continue
        macs = identity.macs
        mine = [h for h in hosts if h["mac"] in macs]
        seen = {h["mac"] for h in mine}
        bridge = next(
            (
                b for b in sorted(bridges, key=lambda b: b["id"])
                if identity.is_it(
                    b["chassis_id"], b.get("mgmt_ips") or [b.get("mgmt_ip")]
                )
            ),
            None,
        )
        heard = {
            (n["switch"], n["port"]) for n in neighbours
            if identity.is_it(n["chassis_id"], n.get("mgmt_ips"))
        }
        node: RouterNode | None = None
        if bridge is not None:
            here = [
                h for h in mine
                if (h["switch"], h["port"]) == (bridge["switch"], bridge["port"])
            ]
            on_map = bridge.get("mac") if bridge.get("mac") in macs else ""
            if not on_map and here:
                on_map = min(
                    here, key=lambda h: _rank(
                        identity, h["mac"], stale=False,
                        approximate=bool(h.get("approximate")),
                    )
                )["mac"]
            node = RouterNode(
                identity, bridge["id"], on_map, bridge["switch"],
                bridge["port"], seen,
            )
        else:
            candidates = [
                (_rank(identity, h["mac"], stale=False,
                       approximate=bool(h.get("approximate")),
                       lldp_here=(h["switch"], h["port"]) in heard),
                 h["mac"], h["switch"], h["port"])
                for h in mine
            ] + [
                (_rank(identity, mac, stale=True, approximate=False,
                       lldp_here=(row["switch"], row["port"]) in heard,
                       last_seen=row.get("last_seen", 0.0)),
                 mac, row["switch"], row["port"])
                for mac, row in remembered.items()
                if mac in macs and mac not in seen
            ]
            if candidates:
                _key, mac, switch, port = min(candidates)
                node = RouterNode(
                    identity, "host:" + mac, mac, switch, port, seen
                )
                matching = [
                    n for n in neighbours
                    if (n["switch"], n["port"]) == (switch, port)
                    and identity.is_it(n["chassis_id"], n.get("mgmt_ips"))
                ]
                if matching:
                    node.lldp = min(
                        matching,
                        key=lambda n: (n["chassis_id"] not in macs,
                                       n["chassis_id"]),
                    )
        if node is None:
            continue
        placement.nodes.append(node)
        for mac in sorted(macs):
            if node.is_bridge or mac != node.mac:
                placement.merged.setdefault(mac, node.node_id)
    return placement


def card(
    identity: RouterIdentity, on_map: str, seen: set[str],
    known: set[str],
) -> dict:
    """The router's interfaces, for the card of its node.

    Each MAC once, by the name of the interface it is called by: the one
    on the map first, then the ones in this scan's tables, then the ones
    the database has met. The MACs MoonLan never met in a MAC table —
    the router's other ports — are `unmet`, for one line of their own:
    a list of every port of a 24-port router would bury the three that
    matter.
    """
    def entry(mac: str) -> dict:
        names = identity.interfaces.get(mac) or []
        return {
            "mac": mac,
            "name": identity.name(mac),
            "kind": identity.kinds.get(mac, ""),
            "names": names[:NAMES_SHOWN],
            "more": max(0, len(names) - NAMES_SHOWN),
            "on_map": mac == on_map,
        }

    def order(mac: str) -> tuple:
        return (
            mac != on_map, mac not in seen,
            KIND_RANK.get(identity.kinds.get(mac, ""), 2),
            identity.name(mac), mac,
        )

    met = [m for m in identity.interfaces
           if m == on_map or m in seen or m in known]
    unmet = [m for m in identity.interfaces if m not in set(met)]
    return {
        "address": identity.address,
        "interfaces": [entry(mac) for mac in sorted(met, key=order)],
        "unmet": [entry(mac) for mac in sorted(unmet, key=order)],
    }
