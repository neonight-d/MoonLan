"""Names and notes on nodes (v0.8.13).

/api/notes — reading, importing and exporting them, writing, deleting
and moving one — and the notes put on the nodes of /api/topology. The
node ids that carry a note are in mapping/upkeep.py, which the scan
needs too. Moved out of server.py word for word (docs/ARCHITECTURE.md);
the state is read as rt.X when a route runs. Nothing here imports
moonlan.server.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from .. import notes, signin
from ..mapping.upkeep import _noted_node_ids, _stand_in
from ..runtime import rt
from .common import _refuse

log = logging.getLogger("moonlan")

router = APIRouter()


# ---------- names and notes on nodes (v0.8.0) ----------

class NoteBody(BaseModel):
    label: str = ""
    note: str = ""


class NoteMove(BaseModel):
    to: str
    # the target has a note of its own: replace it (the page asks first)
    replace: bool = False


async def _note_key_for(node_id: str) -> str:
    """notes.note_key — and for an offline group, the switch without
    SNMP it stands in for, when that has a note and is not drawn. An
    offline group on a port that never had one gets nothing: a name is
    not put on a port where no switch without SNMP ever was."""
    if node_id.startswith("offline:"):
        key = _stand_in(node_id)
        drawn = {p["id"] for p in rt.state.as_dict().get("pseudo_switches") or ()}
        if key not in drawn and await asyncio.to_thread(rt.db.note, key):
            return key
    return notes.note_key(node_id)


async def _existing_note_key(node_id: str) -> str:
    """The key of a note that is there to be deleted or moved away —
    including one on a kind of node that takes no notes any more"""
    try:
        return await _note_key_for(node_id)
    except notes.NoteError:
        key = notes.stored_key(node_id)
        if await asyncio.to_thread(rt.db.note, key):
            return key
        raise


def _note_refused(exc: notes.NoteError) -> JSONResponse:
    return _refuse(exc.code, 400, text=str(exc))


async def _journal_note(event: str, node_id: str, details: dict) -> None:
    """One entry per change, as data the page words: the node, and what
    happened to its name and note. The log gets the same in a line."""
    await asyncio.to_thread(
        rt.db.add_event, time.time(), event, node_id, json.dumps(details),
        signin.actor(),
    )
    log.info(
        "Note %s on %s%s: %s", event.replace("note_", ""), node_id,
        f" by {signin.actor()}" if signin.actor() else "",
        json.dumps(details, ensure_ascii=False),
    )


@router.get("/api/notes")
async def api_notes() -> dict:
    """Every name and note, those without a node on the map included."""
    rows = await asyncio.to_thread(rt.db.notes)
    return {
        "notes": [
            {
                "node_id": node_id, **notes.public(row),
                "state": notes.state(row),
                "missing_since": row["missing_since"],
                # on a group behind a trunk: kept, but no longer a name
                "retired": notes.retired(node_id),
            }
            for node_id, row in sorted(rows.items())
        ],
        "missing_scans": notes.MISSING_SCANS,
    }


def _mac_directory() -> dict[str, str]:
    """MAC -> the node an imported note goes on: a device (on the map
    or off it; one drawn as a bridge is that bridge), then a switch by
    its bridge MAC, then an LLDP bridge by its chassis MAC."""
    topo = rt.state.as_dict()
    found: dict[str, str] = {}
    for host in topo["hosts"]:
        found.setdefault(host["mac"], host.get("merged_into") or "host:" + host["mac"])
    for host in topo.get("unlocated") or ():
        found.setdefault(host["mac"], "host:" + host["mac"])
    for sw in topo["switches"]:
        mac = notes.mac_key(sw.get("mac"))
        if mac:
            found.setdefault(mac, "sw:" + sw["ip"])
    for bridge in topo.get("bridges") or ():
        mac = notes.mac_key(bridge.get("chassis_id"))
        if mac:
            found.setdefault(mac, bridge["id"])
    return found


@router.post("/api/notes/import")
async def api_import_notes(request: Request, dry_run: bool = Query(default=False)):
    """Names and notes from a CSV (mac;label;note), the body of the
    request. With dry_run, what would be written and nothing written —
    the page shows that first. A note written by hand is never
    overwritten; one line per MAC, the last one counts."""
    data = await request.body()
    if len(data) > notes.IMPORT_MAX_BYTES:
        return _refuse("too_big", 413, text="a CSV of notes is at most 2 MB")
    rows, errors, warnings = notes.parse_csv(data)
    existing = await asyncio.to_thread(rt.db.notes)
    plan = notes.plan_import(rows, existing, _mac_directory())
    report = notes.import_report(plan, errors, warnings, dry_run)
    if dry_run:
        return report
    ts = time.time()
    for write in plan["writes"]:
        await asyncio.to_thread(
            rt.db.set_note, write["node_id"], write["label"], write["note"],
            "import", signin.actor(), ts, seen=write["found"],
        )
    summary = {"written": report["written"], "skipped": len(report["skipped_manual"]),
               "errors": len(report["errors"])}
    await asyncio.to_thread(
        rt.db.add_event, ts, "notes_imported", "", json.dumps(summary), signin.actor()
    )
    log.info(
        "Notes imported%s: %d written (%d on the map, %d not found yet), "
        "%d skipped (written by hand), %d error(s)",
        f" by {signin.actor()}" if signin.actor() else "", report["written"],
        report["on_map"], report["not_found"], summary["skipped"], summary["errors"],
    )
    return report


@router.get("/api/notes/export")
async def api_export_notes(preview: bool = Query(default=False)):
    """The notes that have a MAC, as the CSV the import takes: UTF-8
    with a BOM, `;`. With preview, only how many go in and how many
    cannot — notes of nodes without a MAC."""
    rows = await asyncio.to_thread(rt.db.notes)
    switch_macs = {sw["ip"]: sw.get("mac") or "" for sw in rt.state.as_dict()["switches"]}
    data, left_out = notes.export_csv(rows, switch_macs)
    if preview:
        return {"exported": len(rows) - left_out, "left_out": left_out}
    return Response(
        content=data, media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="moonlan-notes.csv"'},
    )


@router.put("/api/notes/{node_id:path}")
async def api_put_note(node_id: str, body: NoteBody):
    """A node's name and note, as a person wrote them. Both empty
    deletes the note. A note is written only on a node the map has:
    one without a node comes from a node that has gone, or an import."""
    try:
        key = await _note_key_for(node_id)
        label = notes.clean_label(body.label)
        text = notes.clean_note(body.note)
    except notes.NoteError as exc:
        return _note_refused(exc)
    before = await asyncio.to_thread(rt.db.note, key)
    if before is None and key not in _noted_node_ids():
        return _refuse("no_node", 404, text="no such node on the map")
    row = await asyncio.to_thread(
        rt.db.set_note, key, label, text, "manual", signin.actor(), time.time()
    )
    if row is None:
        if before is not None:
            await _journal_note("note_changed", key, {"deleted": True})
        return {"node_id": key, "note": None}
    details = {}
    if before is None or before["label"] != label:
        details["label"] = label
    if before is None or before["note"] != text:
        details["note"] = bool(text)
    if details:
        await _journal_note("note_changed", key, details)
    return {"node_id": key, "note": notes.public(row)}


@router.delete("/api/notes/{node_id:path}")
async def api_delete_note(node_id: str):
    try:
        key = await _existing_note_key(node_id)
    except notes.NoteError as exc:
        return _note_refused(exc)
    if not await asyncio.to_thread(rt.db.delete_note, key):
        return _refuse("no_note", 404, text="this node has no note")
    await _journal_note("note_changed", key, {"deleted": True})
    return {"node_id": key, "deleted": True}


@router.post("/api/notes/{node_id:path}/move")
async def api_move_note(node_id: str, body: NoteMove):
    """A note whose node is gone, put on a node that is there — a cable
    moved to another port. A target with a note of its own is replaced
    only when asked to."""
    try:
        source = await _existing_note_key(node_id)
        target = await _note_key_for(body.to)
    except notes.NoteError as exc:
        return _note_refused(exc)
    if source == target:
        return _refuse("same_node", 400, text="the note is already there")
    if await asyncio.to_thread(rt.db.note, source) is None:
        return _refuse("no_note", 404, text="this node has no note")
    if target not in _noted_node_ids():
        return _refuse("no_node", 404, text="no such node on the map")
    if not body.replace and await asyncio.to_thread(rt.db.note, target):
        return _refuse("target_has_note", 409,
                       text="the target node has a note of its own")
    row = await asyncio.to_thread(
        rt.db.move_note, source, target, signin.actor(), time.time()
    )
    await _journal_note("note_moved", target, {"from": source, "to": target})
    return {"node_id": target, "note": notes.public(row)}


def _attach_notes(topo: dict, rows: dict[str, dict]) -> None:
    """Every node with a name or a note carries it as `note`. A device
    off the map has the note of the same device on it.

    The nodes are copied, not written to: the lists are the ones the
    state keeps, and a note set on one of them stayed there after the
    note was deleted, until the next scan replaced the list.

    An offline group stands in for the switch without SNMP of its port
    (v0.8.1). When every device of a room is off for the night, the
    room's switch without SNMP is not drawn and an offline group is;
    the group carries the room's name, and `note_key` says where the
    name is kept. Where that switch is drawn — some devices still
    answer — the name is on it, and the group hangs off it anyway."""
    if not rows:
        return

    def noted(node: dict, key: str) -> dict:
        row = rows.get(key)
        return {**node, "note": notes.public(row)} if row else node

    topo["switches"] = [noted(sw, "sw:" + sw["ip"]) for sw in topo["switches"]]
    for key in ("hosts", "unlocated"):
        topo[key] = [noted(h, "host:" + h["mac"]) for h in topo.get(key) or ()]
    # not groups behind a trunk: they take no names (v0.8.1)
    for key in ("bridges", "pseudo_switches", "external_networks"):
        topo[key] = [noted(n, n.get("id", "")) for n in topo.get(key) or ()]
    drawn = {node["id"] for node in topo.get("pseudo_switches") or ()}
    groups = []
    for group in topo.get("offline_groups") or ():
        key = _stand_in(group["id"])
        row = rows.get(key)
        if row and key not in drawn:
            group = {**group, "note": notes.public(row), "note_key": key}
        groups.append(group)
    topo["offline_groups"] = groups
