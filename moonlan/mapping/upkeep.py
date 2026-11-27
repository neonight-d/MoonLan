"""The notes and saved positions in step with the map (v0.8.14).

Which node ids the map draws and what each hangs off, the nodes a note
can be written on, the notes whose node is gone after a scan, and the
cleanup of positions both old and gone. The routes of notes and the
layout need these as well as the scan: they import them from here.
Moved out of server.py and routes/ word for word
(docs/ARCHITECTURE.md); the state is read as rt.X when they run.
Nothing here imports moonlan.server.
"""

from __future__ import annotations

import asyncio
import logging
import time

from .. import notes
from ..runtime import rt

log = logging.getLogger("moonlan")


def _layout_anchors() -> dict[str, str]:
    """node id -> the node it hangs off, as the map draws it.

    The page reads this off the edges it has just built (the first edge
    into a node); this builds the same edges in the same order from the
    same topology. Needed here to tell whether an offset recorded around
    a pinned node still describes the map: a device that moved to
    another switch has nothing to do with the old one.
    """
    topo = rt.state.as_dict()
    anchors: dict[str, str] = {}
    for link in topo["links"]:
        anchors.setdefault("sw:" + link["b"], "sw:" + link["a"])
    for pseudo in topo.get("pseudo_switches") or ():
        anchors.setdefault(pseudo["id"], "sw:" + pseudo["switch"])
    for bridge in topo.get("bridges") or ():
        anchors.setdefault(bridge["id"], "sw:" + bridge["switch"])
    for key in ("external_networks", "trunk_groups", "offline_groups"):
        for group in topo.get(key) or ():
            anchors.setdefault(
                group["id"], group.get("via") or "sw:" + group["switch"]
            )
    for host in topo["hosts"]:
        if host.get("merged_into"):
            continue
        anchors.setdefault(
            "host:" + host["mac"], host.get("via") or "sw:" + host["switch"]
        )
    return anchors


def _layout_node_ids() -> set[str]:
    """Every node id the current map draws.

    The same ids the front end uses, built here so the housekeeping and
    the diagnostics do not have to ask the browser what exists.
    """
    topo = rt.state.as_dict()
    ids = {"sw:" + sw["ip"] for sw in topo["switches"]}
    # A device drawn AS another node — a bridge that answers LLDP and
    # is also in somebody's MAC table — has no node of its own, so it
    # has no position of its own either. Counting it would put two
    # nodes in "not in the saved layout" that nobody can place.
    ids |= {
        "host:" + h["mac"] for h in topo["hosts"] if not h.get("merged_into")
    }
    for key in ("pseudo_switches", "bridges", "external_networks",
                "offline_groups", "trunk_groups"):
        ids |= {node["id"] for node in topo.get(key) or () if node.get("id")}
    return ids


def _stand_in(offline_id: str) -> str:
    """The switch without SNMP an offline group stands in for: the same
    switch and port"""
    return "pseudo:" + offline_id[len("offline:"):]


def _noted_node_ids() -> set[str]:
    """The nodes a note can be written on now: everything on the map,
    and the devices MoonLan knows that are not on it. An offline group
    keeps the switch without SNMP of its port present: a night with
    every device of a room off is not a cable moved."""
    topo = rt.state.as_dict()
    ids = {i for i in _layout_node_ids() if not notes.retired(i)}
    ids |= {"host:" + h["mac"] for h in topo.get("unlocated") or ()}
    ids |= {_stand_in(g["id"]) for g in topo.get("offline_groups") or ()}
    return ids


async def _reconcile_notes() -> None:
    """Marks the notes whose node is not on the map after this scan, and
    unmarks those whose node is back (a cable put back in its port). A
    note is "without a node" only after notes.MISSING_SCANS scans in a
    row; it is never deleted."""
    changed = await asyncio.to_thread(
        rt.db.reconcile_notes, _noted_node_ids(), time.time()
    )
    if changed["lost"]:
        log.info(
            "Notes: %d note(s) whose node has not been on the map for %d "
            "scans — kept, and listed under \"Notes without a node\": %s",
            len(changed["lost"]), notes.MISSING_SCANS,
            ", ".join(sorted(changed["lost"])[:10]),
        )
    if changed["back"]:
        log.info(
            "Notes: the node of %d note(s) is on the map again: %s",
            len(changed["back"]), ", ".join(sorted(changed["back"])[:10]),
        )


async def _purge_layout_once() -> None:
    """Forgets positions of nodes that are both old and gone.

    Runs after the first scan of this process, because that is the
    first moment anything knows which nodes exist — and once a day
    after it (periodic_cleanup), so a layout_keep_days changed in
    Settings holds without a restart. A device switched
    off for the night keeps its place however long the night is; only
    age decides, and only for something the map no longer has. The
    same scan says what each node hangs off, so an offset recorded
    around a pinned node that a device has since left goes too.
    """
    if rt.layout_purged:
        return
    rt.layout_purged = True
    await purge_layout()


async def purge_layout() -> None:
    gone = await asyncio.to_thread(
        rt.db.purge_layout, rt.config.layout_keep_days, _layout_node_ids()
    )
    if gone:
        log.info(
            "Map layout: forgot the saved position of %d node(s) not seen "
            "for over %.0f day(s) and no longer on the map: %s",
            len(gone), rt.config.layout_keep_days,
            ", ".join(sorted(gone)[:10])
            + (" and more" if len(gone) > 10 else ""),
        )
    moved = await asyncio.to_thread(
        rt.db.drop_misplaced_offsets, _layout_anchors()
    )
    if moved:
        log.info(
            "Map layout: forgot the offset of %d node(s) that no longer "
            "hang off the pinned node it was measured from: %s",
            len(moved), ", ".join(sorted(moved)[:10])
            + (" and more" if len(moved) > 10 else ""),
        )
