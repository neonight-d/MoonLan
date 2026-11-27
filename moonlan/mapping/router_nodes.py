"""A router drawn as one node (v0.8.11, moved in v0.8.14).

The MACs of a router of `routers:` — its own, by its identity — are
one node on the map: out of the damaged-copy rules, remembered while
it is off, merged into the node, which learns its address and
interfaces; a note or pin on one of those MACs goes to the node. Not
to be confused with moonlan/routers.py, which reads the identity.
Moved out of server.py word for word (docs/ARCHITECTURE.md); the state
is read as rt.X when they run. Nothing here imports moonlan.server.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from .. import demo
from ..routers import (
    RouterIdentity,
    card as router_card,
    own_macs as router_own_macs,
)
from ..runtime import rt
from ..topology import lldp_metadata

log = logging.getLogger("moonlan")


def _router_addresses(
    arp: dict[str, str], identities: list[RouterIdentity] | None = None,
) -> dict[str, str]:
    """MAC -> the address config.routers actually reaches it at.

    A router announces a management address per VLAN interface — the
    MikroTik behind sw0 Slot0/21 sends 38 — and the first one the walk
    returns is as likely to be a segment gateway as the address anyone
    uses. The one in `routers:` is the one the operator types, so that
    is what goes under the node's name.

    Every MAC a router reports as its own is that router (v0.8.11). ARP
    is what is left when it reports nothing: a router's own addresses
    are not in its own ARP table, only in another router's.
    """
    found = {
        mac: identity.address
        for mac, identity in router_own_macs(identities or ()).items()
    }
    listed = set(rt.config.routers)
    for mac, ip in arp.items():
        if ip in listed:
            found.setdefault(mac, ip)
    if rt.config.demo:
        for mac, ip in demo.ROUTER_ADDRESSES.items():
            found.setdefault(mac, ip)
    return found


async def _release_router_macs(own: dict[str, RouterIdentity]) -> None:
    """A router's own MACs, out of what the damaged-copy rules did to
    them: the mark of a copy cleared, a record held back confirmed. One
    INFO line for what was undone; a record that was only new is
    confirmed without one."""
    released = await asyncio.to_thread(
        rt.db.vouch_for, set(own), max(rt.config.new_host_confirm_scans, 1)
    )
    undone = [r for r in released if r["was"] != "new"]
    if not undone:
        return
    log.info(
        "Own MACs of routers in routers: are not damaged copies, and are "
        "devices now: %s",
        "; ".join(
            f"{r['mac']} ({own[r['mac']].name(r['mac'])} of "
            f"{own[r['mac']].address}, "
            + (
                f"was marked a copy of {r['sample']}" if r["was"] == "copy"
                else "was held back as a possible copy"
            ) + ")"
            for r in undone
        ),
    )


def _remembered_router_macs(
    own: dict[str, RouterIdentity], db_rows: dict[str, dict],
    switch_ips: set[str], drawn: set[str],
) -> dict[str, dict]:
    """The own MACs of routers the map would draw out of the grace
    window — the records _assemble_hosts puts on the map grey — as
    mac -> {switch, port, last_seen}. `drawn` are the MACs this scan
    has already (and the unconfirmed ones)."""
    grace = rt.config.host_grace_hours * 3600
    now = time.time()
    remembered: dict[str, dict] = {}
    for mac in own:
        row = db_rows.get(mac)
        if row is None or mac in drawn:
            continue
        if row["switch_ip"] not in switch_ips or not row["port"]:
            continue
        if grace > 0 and now - row["last_seen"] < grace:
            remembered[mac] = {
                "switch": row["switch_ip"], "port": row["port"],
                "last_seen": row["last_seen"],
            }
    return remembered


def _merge_router_macs(placement, hosts: list[dict], bridges: list[dict]) -> None:
    """This scan's hosts that are a router's MAC other than its node:
    drawn as the node. A bridge that is the router no longer counts
    them among the devices behind it — they are the bridge."""
    by_id = {bridge["id"]: bridge for bridge in bridges}
    for host in hosts:
        target = placement.merged.get(host["mac"])
        if not target or host.get("merged_into"):
            continue
        host["merged_into"] = target
        bridge = by_id.get(target)
        if (
            bridge is not None and bridge.get("host_count")
            and host.get("via") == target
        ):
            bridge["host_count"] -= 1


def _mark_router_nodes(
    placement, hosts: list[dict], bridges: list[dict],
    db_rows: dict[str, dict],
) -> None:
    """The node of each router learns it is one: its address, its
    interfaces for the card, and — for a host — the LLDP neighbour that
    is the router on its port, whatever chassis ID that one sent."""
    by_mac = {host["mac"]: host for host in hosts}
    by_id = {bridge["id"]: bridge for bridge in bridges}
    for node in placement.nodes:
        identity = node.identity
        target = by_id.get(node.node_id) if node.is_bridge else by_mac.get(node.mac)
        if target is None:
            continue
        target["router_ip"] = identity.address
        target["router"] = router_card(
            identity, node.mac, node.seen, identity.macs & set(db_rows),
        )
        if node.lldp is not None and not node.is_bridge:
            target["lldp"] = lldp_metadata(node.lldp)


# who the journal says moved a note or a pin that followed a router's
# node: MoonLan itself, worded by the page like "@console"
MOONLAN_ACTOR = "@moonlan"


async def _router_nodes_take_over(placement) -> None:
    """A note or a pin on a router's MAC now drawn as its node goes to
    the node, once (v0.8.11).

    The operator named and pinned the MAC that was on the map; the
    router is drawn as another one now. Only when the node has none of
    its own: a node with its own note keeps it, and the note of the
    merged MAC goes to "Notes without a node" after notes.MISSING_SCANS
    scans, as any note whose node is gone. Journalled as a note moved
    and a node pinned by hand are, by MoonLan.
    """
    if not placement.nodes:
        return
    layout = await asyncio.to_thread(rt.db.layout)
    stored = await asyncio.to_thread(rt.db.notes)
    for node in placement.nodes:
        sources = [
            "host:" + mac for mac, target in sorted(placement.merged.items())
            if target == node.node_id
        ]
        if node.node_id not in stored:
            noted = [key for key in sources if key in stored]
            if noted:
                # the one written last, if the operator wrote several
                source = max(noted, key=lambda key: stored[key]["updated_at"])
                await asyncio.to_thread(
                    rt.db.move_note, source, node.node_id, MOONLAN_ACTOR,
                    time.time(),
                )
                details = {"from": source, "to": node.node_id}
                await asyncio.to_thread(
                    rt.db.add_event, time.time(), "note_moved", node.node_id,
                    json.dumps(details), MOONLAN_ACTOR,
                )
                log.info(
                    "Note moved from %s to %s by MoonLan: router %s is "
                    "drawn as %s now", source, node.node_id,
                    node.identity.address, node.node_id,
                )
        if not (layout.get(node.node_id) or {}).get("pinned"):
            pinned = [key for key in sources if (layout.get(key) or {}).get("pinned")]
            if pinned:
                source = max(pinned, key=lambda key: layout[key]["updated_at"] or 0)
                if await asyncio.to_thread(rt.db.move_position, source, node.node_id):
                    await asyncio.to_thread(
                        rt.db.add_event, time.time(), "layout_pinned", "",
                        json.dumps({
                            "n": 1, "ids": [node.node_id], "from": source,
                        }),
                        MOONLAN_ACTOR,
                    )
                    log.info(
                        "Pinned position moved from %s to %s by MoonLan: "
                        "router %s is drawn as %s now", source,
                        node.node_id, node.identity.address, node.node_id,
                    )
