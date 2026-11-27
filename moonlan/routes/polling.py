"""A scan on demand and the state of polling (v0.8.14).

POST /api/scan — "Scan now" on the page — and GET /api/polling, when
each switch was last polled in full and how long it took. The only
routes that need the poller itself. Moved out of server.py word for
word (docs/ARCHITECTURE.md); the state is read as rt.X when a route
runs. Nothing here imports moonlan.server.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter

from .. import signin
from ..derived import configured_switches
from ..poller import run_scan
from ..runtime import rt
from ..snmp_collector import STATE_NEVER

log = logging.getLogger("moonlan")

router = APIRouter()


async def _scan_once() -> None:
    """A manual scan, recording a failure the same way the loop does."""
    try:
        await run_scan()
    except Exception as exc:
        rt.state.scan_failed(exc)
        log.exception("Network scan failed")


@router.post("/api/scan")
async def api_scan() -> dict:
    if signin.actor():
        log.info("Network scan requested by %s", signin.actor())
    asyncio.create_task(_scan_once())
    return {"status": "started"}


@router.get("/api/polling")
async def api_polling() -> dict:
    """When each switch was last polled in full, and how long it took.

    A poll budget set by eye is a budget set wrong. These are the
    numbers to set it from, and they live in this process, so
    `diag --config` asks for them rather than guessing.
    """
    return {
        "counters_interval_seconds": rt.config.counters_interval_seconds,
        "switches": [
            {
                "ip": ip,
                "name": (sw.sys_name or ip) if sw else "",
                "budget_seconds": rt.config.host_budget(ip),
                "polled_at": sw.polled_at if sw else 0.0,
                "poll_seconds": round(sw.poll_seconds, 1) if sw else 0.0,
                "over_budget_scans": sw.over_budget_scans if sw else 0,
                # Where the map's data on it comes from, and whether it
                # answered in this scan: true, false or null — not
                # known (v0.8.7). `reachable` was the default of an
                # empty reading, false for a switch that answered and
                # ran out of time; it is kept, and now means `answered`.
                "state": sw.map_state if sw else STATE_NEVER,
                "answered": sw.answered if sw else None,
                "reachable": bool(sw and sw.answered),
                # where the time of the poll went (v0.8.8)
                "cut_at": rt.poll_report.get(ip, {}).get("cut_at"),
                "slowest_walks": rt.poll_report.get(ip, {}).get("slowest_walks", []),
            }
            for ip, sw in (
                (ip, rt.switch_data.get(ip)) for ip in configured_switches()
            )
        ],
    }
