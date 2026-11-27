"""Alarms and the journal (v0.8.13).

/api/alarms — the open ones, with what the page needs to word them —
clearing one, and /api/journal, whose filters choose from the groups
in journal.py. Moved out of server.py word for word
(docs/ARCHITECTURE.md); the state is read as rt.X when a route runs.
Nothing here imports moonlan.server.
"""

from __future__ import annotations

import asyncio
import functools

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from .. import signin
from ..derived import _copies_window, _subject_inbound_errors
from ..journal import EVENT_GROUP, JOURNAL_GROUPS, JOURNAL_OTHER
from ..runtime import rt
from .common import _copy_samples

router = APIRouter()


def _alarm_meta(
    row: dict, hosts: dict[str, dict], sw_names: dict[str, str]
) -> dict:
    """Subject broken into display parts so the UI parses no strings:
    the device label, and the switch and port it belongs to."""
    subject = row["subject"]
    meta = {"display": subject, "switch_ip": "", "switch_name": "", "port": ""}
    if row["type"] in ("host_down", "new_mac"):
        host = hosts.get(subject, {})
        meta["display"] = host.get("name") or host.get("ip") or subject
        meta["switch_ip"] = host.get("switch_ip", "")
        meta["port"] = host.get("port", "")
    elif row["type"] in ("switch_down", "stp_root_changed",
                         "stp_topology_change",
                         "loop_detection_disabled"):
        meta["switch_ip"] = subject
    elif row["type"] == "unmanaged_bridge_detected":
        # the subject is a chassis id, which is a MAC — splitting it on
        # ":" the way port subjects are split would produce nonsense
        bridge = next(
            (b for b in rt.state.as_dict()["bridges"]
             if b["chassis_id"] == subject),
            {},
        )
        meta["display"] = bridge.get("name") or subject
        meta["switch_ip"] = bridge.get("switch", "")
        meta["port"] = bridge.get("port", "")
    elif row["type"] in ("stp_fragmented", "tls_cert_expiring",
                         "passkey_clone_suspected"):
        # not a port: a network verdict, or MoonLan itself — a host name,
        # "person: key" — whose ":" is not a switch's
        meta["display"] = subject
    else:
        ip, sep, port = subject.partition(":")
        if sep:
            meta["switch_ip"] = ip
            meta["port"] = (
                "LAG " + port[4:-1]
                if port.startswith("lag[") and port.endswith("]")
                else port
            )
    if meta["switch_ip"]:
        meta["switch_name"] = sw_names.get(meta["switch_ip"], meta["switch_ip"])
        # For a port or a switch alarm the switch IS the subject. For a
        # bridge it is only where the bridge was found, and overwriting
        # the display with it labelled every bridge alarm with the name
        # of the switch it hangs off.
        if row["type"] not in (
            "host_down", "new_mac", "unmanaged_bridge_detected",
        ):
            meta["display"] = meta["switch_name"]
    return meta


@router.get("/api/alarms")
async def api_alarms(
    active: int = Query(default=1, ge=0, le=1),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict:
    rows = await asyncio.to_thread(rt.db.alarms, bool(active), limit)
    db_hosts = await asyncio.to_thread(rt.db.hosts_by_mac)
    sw_names = {sw["ip"]: sw["name"] for sw in rt.state.as_dict()["switches"]}
    flapping = rt.alarm_engine.flapping_keys()
    stats = rt.alarm_engine.raise_stats()
    for row in rows:
        key = (row["type"], row["subject"])
        row.update(_alarm_meta(row, db_hosts, sw_names))
        row["flapping"] = key in flapping
        row["raise_count"], row["last_raise"] = stats.get(key, (1, row["ts_raised"]))
        if row["type"] == "port_frame_corruption" and active:
            # which fault is likelier, as data the page words (v0.8.5)
            samples = rt.alarm_engine.corruption_samples(row["subject"])
            if samples:
                row["corruption"] = {
                    "samples": _copy_samples(samples),
                    "errors_in": _subject_inbound_errors(row["subject"]),
                    "minutes": _copies_window() / 60,
                }
    return {
        "alarms": rows,
        "flap_window_hours": rt.config.notifications.flap_window_seconds / 3600,
    }


@router.post("/api/alarms/{alarm_id}/clear")
async def api_clear_alarm(alarm_id: int):
    row = await rt.alarm_engine.manual_clear(alarm_id, user=signin.actor())
    if row is None:
        return JSONResponse({"error": "no such active alarm"}, status_code=404)
    return {"id": alarm_id, "cleared": True}


@router.get("/api/journal")
async def api_journal(
    limit: int = Query(default=100, ge=1, le=1000),
    group: list[str] | None = Query(default=None),
    before: int | None = Query(default=None, ge=1),
):
    """The journal, newest first, a page at a time (v0.8.6).

    `group` may repeat, none means every event; `before` is an id, and
    the page is the events below it. `next_before` is the id to ask for
    the page after this one, null when there is none.
    """
    chosen = list(dict.fromkeys(group or []))
    unknown = [
        g for g in chosen if g not in JOURNAL_GROUPS and g != JOURNAL_OTHER
    ]
    if unknown:
        return JSONResponse(
            {"error": "unknown_group", "groups": unknown,
             "known": [*JOURNAL_GROUPS, JOURNAL_OTHER]},
            status_code=400,
        )
    events = not_events = None
    if chosen:
        events = [
            event for g in chosen if g != JOURNAL_OTHER
            for event in JOURNAL_GROUPS[g]
        ]
        if JOURNAL_OTHER in chosen:
            not_events = list(EVENT_GROUP)
    rows = await asyncio.to_thread(
        functools.partial(
            rt.db.journal, limit + 1, events=events, not_events=not_events,
            before=before,
        )
    )
    more = len(rows) > limit
    rows = rows[:limit]
    for event in rows:
        event["group"] = EVENT_GROUP.get(event["event"], JOURNAL_OTHER)
        # who made the device an event is about — looked up now, not
        # stored: the register is updated, the journal is not
        name = rt.vendors.lookup(event.get("mac") or "").name
        if name:
            event["vendor"] = name
    return {
        "events": rows,
        "next_before": rows[-1]["id"] if more and rows else None,
    }
