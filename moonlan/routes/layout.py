"""Where the nodes stand: the saved layout (v0.8.13).

/api/layout — the positions, and the offsets of what hangs off a node;
saving them, placing and releasing one node, clearing them all — and
the anchors and node ids that the cleanup of saved positions in
server.py needs too. Moved out of server.py word for word
(docs/ARCHITECTURE.md); the state is read as rt.X when a route runs.
Nothing here imports moonlan.server.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .. import signin
from ..runtime import rt

log = logging.getLogger("moonlan")

router = APIRouter()


class NodePosition(BaseModel):
    x: float
    y: float
    pinned: bool | None = None


class Offset(BaseModel):
    x: float
    y: float


class LayoutBody(BaseModel):
    nodes: dict[str, NodePosition] = {}
    # Store only nodes that have no position yet, and say what the
    # others already have. What v0.7.2's automatic save used; kept for
    # a page still running that script.
    only_new: bool = False
    # pinned node -> {node hanging directly off it -> its offset}: how
    # the groups and switches around a pinned node stood when a person
    # placed it. The whole set for that anchor; it replaces the last.
    neighbours: dict[str, dict[str, Offset]] = {}


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


def _current_offsets(saved: dict[str, dict]) -> dict[str, dict]:
    """The saved layout without offsets that no longer describe the map.

    An offset is kept while its anchor is pinned and — for a node on
    the map — while the node still hangs off that anchor. Otherwise it
    is left out, and the node is laid out from the pinned ones like
    anything else.
    """
    anchors = _layout_anchors()
    kept = {}
    for node_id, pos in saved.items():
        anchor = pos.get("anchor")
        if anchor:
            base = saved.get(anchor)
            if not base or not base["pinned"]:
                continue
            if node_id in anchors and anchors[node_id] != anchor:
                continue
        kept[node_id] = pos
    return kept


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


@router.get("/api/layout")
async def api_layout() -> dict:
    """Saved node positions, and how they compare with the map."""
    saved = _current_offsets(await asyncio.to_thread(rt.db.layout))
    present = _layout_node_ids()
    return {
        "nodes": saved,
        "saved_at": await asyncio.to_thread(rt.db.layout_saved_at),
        # When somebody last reset the whole layout. An open page that
        # sees this move treats it as a reset of its own; the rows alone
        # could not tell it that.
        "cleared_at": await asyncio.to_thread(rt.db.layout_cleared_at),
        # Nodes on the map with no saved position: since v0.7.3 that is
        # everything nobody pinned, laid out again on every load from
        # the pinned nodes. A count, not a list of gaps to fill.
        "missing": sorted(present - set(saved)),
        # …and the other direction: a saved position whose node is not
        # on the map. Kept on purpose — a device switched off for the
        # night comes back to its place — and only forgotten by age.
        "orphans": sorted(set(saved) - present),
    }


@router.put("/api/layout")
async def api_put_layout(body: LayoutBody) -> dict:
    """Stores the whole picture as it is on screen right now.

    With `only_new`, only nodes nobody has placed yet: the rest keep
    what they have, and the answer says what that is, so the page can
    take it instead of believing its own copy.
    """
    positions = {
        node_id: pos.model_dump(exclude_none=True)
        for node_id, pos in body.nodes.items()
    }
    if body.only_new:
        stored, existing = await asyncio.to_thread(
            rt.db.add_new_positions, positions
        )
    else:
        await asyncio.to_thread(rt.db.save_layout, positions)
        stored, existing = list(positions), {}
    if stored:
        await asyncio.to_thread(
            rt.db.add_event, time.time(), "layout_saved", "",
            f"{len(stored)} node(s)", signin.actor(),
        )
        log.info("Map layout saved: %d node(s)", len(stored))
    return {
        "saved": len(stored),
        "stored": stored,
        "existing": existing,
        "saved_at": await asyncio.to_thread(rt.db.layout_saved_at),
    }


# How many node ids a journal entry about a layout action names; the
# count is always there, the list only while it is short enough to read
LAYOUT_EVENT_IDS = 5


async def _journal_layout_action(event: str, node_ids: list[str]) -> None:
    """One journal entry per action, however many nodes it touched.

    Pinning and releasing were not in the journal at all, which is why
    a pin wiped out by another page's save was visible to nobody. The
    details are data, not a sentence — the page words them in its own
    language and by the nodes' captions — and it names who did it.
    """
    if not node_ids:
        return
    details = json.dumps({
        "n": len(node_ids), "ids": node_ids[:LAYOUT_EVENT_IDS],
    })
    await asyncio.to_thread(
        rt.db.add_event, time.time(), event, "", details, signin.actor()
    )


async def _store_hand_placed(positions: dict[str, NodePosition]) -> dict:
    """Stores nodes placed or released by hand, journalled per kind.

    A node sent without `pinned` is pinned: this is the hand putting it
    somewhere. Released (`pinned: false`) is forgotten: only what a
    person placed is kept, and a released node is laid out from the
    pinned ones on the next load like everything else.
    """
    rows = {
        node_id: {"x": pos.x, "y": pos.y, "pinned": True}
        for node_id, pos in positions.items()
        if pos.pinned is None or pos.pinned
    }
    released = [node_id for node_id in positions if node_id not in rows]
    if rows:
        await asyncio.to_thread(rt.db.save_layout, rows)
    if released:
        await asyncio.to_thread(rt.db.forget_positions, released)
    pinned = list(rows)
    await _journal_layout_action("layout_pinned", pinned)
    await _journal_layout_action("layout_released", released)
    return {"pinned": pinned, "released": released}


@router.patch("/api/layout")
async def api_patch_positions(body: LayoutBody) -> dict:
    """Several nodes placed or released in one action — a dragged
    selection, `P` on a selection, one item of the menu — and how the
    nodes around each placed one stood at that moment."""
    result = await _store_hand_placed(body.nodes)
    result["neighbours"] = {}
    for anchor, offsets in body.neighbours.items():
        result["neighbours"][anchor] = await asyncio.to_thread(
            rt.db.set_neighbours, anchor,
            {node_id: {"x": o.x, "y": o.y} for node_id, o in offsets.items()},
        )
    return result


@router.patch("/api/layout/{node_id:path}")
async def api_patch_node_position(node_id: str, body: NodePosition) -> dict:
    """One node, moved by hand — pinned unless told otherwise."""
    await _store_hand_placed({node_id: body})
    pinned = True if body.pinned is None else body.pinned
    return {"node_id": node_id, "x": body.x, "y": body.y, "pinned": pinned}


@router.delete("/api/layout/{node_id:path}")
async def api_delete_node_position(node_id: str):
    """Forgets one node: it goes back under the physics engine.

    The page no longer releases a node this way — it keeps the row and
    clears the pin — but a page still running an older script does, so
    it is journalled as the release it means.
    """
    removed = await asyncio.to_thread(rt.db.forget_node_position, node_id)
    if not removed:
        return JSONResponse(
            {"error": "no saved position for this node"}, status_code=404
        )
    await _journal_layout_action("layout_released", [node_id])
    return {"node_id": node_id, "removed": True}


@router.delete("/api/layout")
async def api_clear_layout() -> dict:
    """Forgets the whole layout. The map is laid out from scratch."""
    removed = await asyncio.to_thread(rt.db.clear_layout)
    cleared_at = time.time()
    await asyncio.to_thread(
        rt.db.add_event, cleared_at, "layout_cleared", "",
        f"{removed} node(s)", signin.actor(),
    )
    log.info(
        "Map layout cleared: %d node(s) forgotten%s", removed,
        f" (by {signin.actor()})" if signin.actor() else "",
    )
    # the page that asked for it must not mistake its own reset for
    # somebody else's on the next refresh
    return {"removed": removed, "cleared_at": cleared_at}
