"""Ports, STP, search and status (v0.8.13).

/api/switch/{ip}/ports — the ports panel of a switch: its counters,
loop detection, LLDP, MAC copies — and /api/stp, /api/search,
/api/skipped-oids, /api/status. /api/topology, /api/polling and
/api/scan stay in server.py with the poller for now. Moved out of
server.py word for word (docs/ARCHITECTURE.md); the state is read as
rt.X when a route runs. Nothing here imports moonlan.server.
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter

from fastapi import APIRouter, Query

from .. import __version__, counters
from ..derived import (
    _effective_monitored,
    _lag_groups,
    _loop_report,
    _rates_hide_age,
    _round,
    _sum_known,
    _uplink_ports,
)
from ..housekeeping import resource_usage
from ..runtime import rt
from ..snmp_collector import SwitchData
from ..topology import port_name
from .common import _copy_samples

router = APIRouter()


ZERO_FLAPS = counters.PortFlaps(count=0, last=0.0)


def _copy_counts(found: list[dict]) -> dict[str, int]:
    """The addresses a port's copies are of -> how many, most first"""
    counts = Counter(s["sample"] for s in found)
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


@router.get("/api/switch/{ip}/ports")
async def api_switch_ports(ip: str) -> dict:
    """Port table of one switch with current rates."""
    sw = rt.switch_data.get(ip)
    if sw is None:
        return {"switch": ip, "name": ip, "ports": []}
    # Everything measured, however old. What is too old to show is
    # decided below, once, and said out loud rather than by omission.
    rates = rt.counter_store.current(ip)
    hide_age = _rates_hide_age()
    now = time.time()
    db_hosts = await asyncio.to_thread(rt.db.hosts_by_mac)
    host_counts: Counter = Counter()
    monitored_counts: Counter = Counter()
    for h in rt.state.as_dict()["hosts"]:
        if h["switch"] != ip:
            continue
        host_counts[h["port"]] += 1
        if _effective_monitored(db_hosts.get(h["mac"], {})):
            monitored_counts[h["port"]] += 1
    member_of: dict[int, str] = {}
    for aggregate, members in _lag_groups(sw).items():
        for m in members:
            member_of[m] = port_name(sw, aggregate)
    flapping = {
        port: info for (sw_ip, port), info in rt.flap_by_port.items()
        if sw_ip == ip
    }
    external = {port for sw_ip, port in _uplink_ports() if sw_ip == ip}
    lldp_by_port: dict[int, list[dict]] = {}
    for neighbor in sw.lldp_neighbors:
        if neighbor.local_ifindex is None:
            continue
        lldp_by_port.setdefault(neighbor.local_ifindex, []).append(
            _lldp_dict(neighbor)
        )
    ports = []
    for p in sw.ports.values():
        r, age = counters.rate_for_display(
            rates, p.if_index, now, hide_age
        )
        name = p.name or str(p.if_index)
        rate_ts = None if age is None else rates[p.if_index].ts
        members = (
            sorted(sw.lag_groups.get(-p.if_index, []))
            if p.if_index < 0 else []
        )
        aggregate = None
        if members:
            aggregate = _aggregate_row(sw, members, rates, now, hide_age)
        ports.append({
            "if_index": p.if_index,
            "name": name,
            "oper_up": p.oper_up,
            "is_physical": p.is_physical,
            "speed_mbps": p.speed_mbps,
            "pvid": sw.port_pvid.get(p.if_index, 0),
            "lag": member_of.get(p.if_index, ""),
            "in_mbps": _round(r.in_mbps if r else None),
            "out_mbps": _round(r.out_mbps if r else None),
            "errors_per_min": _round(r.errors_per_min if r else None),
            "discards_per_min": _round(r.discards_per_min if r else None),
            # When the numbers above were measured, and how long ago.
            # None in both means this port has never been measured at
            # all — the one case "—" is allowed to mean.
            "rate_ts": rate_ts,
            "rate_age_seconds": None if age is None else round(age),
            # an inferred aggregate (v0.8.6): no counters of its own,
            # ever (v0.6.6) — its row is its members' (see below)
            "aggregate": bool(members),
            "members": [port_name(sw, m) for m in members],
            "hosts": host_counts.get(name, 0),
            "monitored_hosts": monitored_counts.get(name, 0),
            # MACs that look like damaged copies of a real one here,
            # and the addresses they are copies of: one of them points
            # at that device's adapter, several at the port (v0.8.5)
            "suspect_macs": rt.suspect_by_port.get((ip, name), []),
            "suspect_samples": _copy_samples(
                _copy_counts(rt.suspect_by_port.get((ip, name), []))
            ),
            # link-state transitions inside the flap window
            "flaps": flapping.get(name, ZERO_FLAPS).count,
            "flaps_last": flapping.get(name, ZERO_FLAPS).last,
            # what the device on the other end says about itself
            "lldp": lldp_by_port.get(p.if_index, []),
            # …and whether that can be believed: a switch forwarding
            # foreign LLDP frames shows neighbours on the wrong ports
            "lldp_forwarded": p.if_index in sw.lldp_forwarded,
            # several devices behind one port is not forwarding, it is
            # an unmanaged switch on the cable
            "lldp_crowded": p.if_index in sw.lldp_crowded,
            # the administrative name an operator typed into the switch
            "label": sw.port_labels.get(p.if_index, ""),
            # what the switch's own Loop Detection says about this port
            "loop": _loop_port(sw, p.if_index),
            # a port that leaves the network (config.uplink_ports)
            "external": name in external,
            # …or one that looks like it should be, and is not listed
            "uplink_hint": rt.uplink_suspects.get((ip, name)),
        })
        if aggregate is not None:
            ports[-1].update(aggregate)
    # active ports first, then by port number
    ports.sort(key=lambda p: (not p["oper_up"], abs(p["if_index"])))
    return {
        "switch": ip,
        "name": sw.sys_name or ip,
        "ports": ports,
        # which counter columns the agent answered for, so a column of
        # dashes can say why it is a column of dashes
        "columns": _column_report(ip),
        # …and the same honesty about loop detection: which profile
        # answered, or that the model does not report it at all
        "loop_detection": _loop_report(sw),
        # When a measured rate stops being fresh (shown dimmed, with
        # its age) and when it stops being shown at all. The panel
        # needs both to tell "stale" from "never measured"; neither
        # belongs hardcoded in the front end.
        "rates": {
            "stale_after_seconds": max(rt.config.counters_interval_seconds, 5) * 3,
            "hide_after_seconds": hide_age,
        },
        # so the panel can colour the values it shows
        "thresholds": {
            "errors_per_minute": rt.config.thresholds.errors_per_minute,
            "discards_per_minute": rt.config.thresholds.discards_per_minute,
        },
    }


def _aggregate_row(
    sw: SwitchData, members: list[int], rates: dict, now: float,
    hide_age: float,
) -> dict:
    """An inferred aggregate's row in the ports panel, out of its members.

    Its synthetic port has no interface behind it: no link of its own,
    no speed, and counters are never polled for it (v0.6.6). Shown as
    it was, it read "no link, 0, —" next to its own live members, a
    port that looked down. So: up when a member is up, the speed of
    the members that are up (as the link's, `_lag_info`), traffic,
    errors and discards summed — one member not measured and the sum
    is not known either (`_sum_known`) — and as old as its oldest
    member's reading.
    """
    ports = [sw.ports[m] for m in members if m in sw.ports]
    shown = [
        counters.rate_for_display(rates, m, now, hide_age) for m in members
    ]
    known = [r for r, _age in shown]
    ages = [age for _r, age in shown]

    def total(field: str) -> float | None:
        value = _sum_known(
            None if r is None else getattr(r, field) for r in known
        )
        return _round(value)

    stamps = [rates[m].ts for m, (_r, age) in zip(members, shown)
              if age is not None]
    measured = all(age is not None for age in ages)
    return {
        "oper_up": any(p.oper_up for p in ports),
        "speed_mbps": sum(p.speed_mbps for p in ports if p.oper_up),
        "in_mbps": total("in_mbps"),
        "out_mbps": total("out_mbps"),
        "errors_per_min": total("errors_per_min"),
        "discards_per_min": total("discards_per_min"),
        "rate_ts": min(stamps) if measured and stamps else None,
        "rate_age_seconds": round(max(ages)) if measured and ages else None,
    }


# Per port, what loop detection has to say about it:
# "off"     — LBD is not switched on for this port
# "ok"      — the agent reports the known-normal value
# "loop"    — anything else, which is the whole point of the rule
# "unknown" — no status arrived, which is not the same as "ok"
def _loop_port(sw: SwitchData, if_index: int) -> dict | None:
    loop = sw.loop_detection
    if loop is None or not loop.supported:
        return None
    state = loop.port(if_index)
    if state is None:
        return {"state": "unknown", "raw": ""}
    if state.lbd_enabled is False:
        return {"state": "off", "raw": state.status_raw}
    if state.looped is None:
        return {"state": "unknown", "raw": ""}
    return {
        "state": "loop" if state.looped else "ok",
        "raw": state.status_raw,
    }


# Which counter columns feed which table column
COLUMN_GROUPS = {
    "in": ("in_octets",),
    "out": ("out_octets",),
    "err": ("in_errors", "out_errors"),
    "disc": ("in_discards", "out_discards"),
}


def _column_report(ip: str) -> dict:
    """Per table column: did the agent answer, and for which OIDs not.

    "no answer" and "all zeros" produce the same empty column and mean
    opposite things — sw0 reported honest zeros for errors while its
    outbound octets were simply never returned.
    """
    columns = rt.counter_columns.get(ip) or {}
    report: dict[str, dict] = {}
    for name, sources in COLUMN_GROUPS.items():
        present = [columns[src] for src in sources if src in columns]
        silent = [st for st in present if not st.answered]
        partial = [st for st in present if st.truncated]
        report[name] = {
            "answered": not silent,
            # answered, but not for every port: the ports at the end of
            # the table are the ones that go missing
            "partial": bool(partial),
            "oids": [st.oid for st in silent + partial],
            "error": next(
                (st.error for st in silent + partial if st.error), ""
            ),
            "filled": sum(st.gaps_filled for st in present),
        }
    return report


def _lldp_dict(neighbor) -> dict:
    """One LLDP neighbour as the API returns it."""
    return {
        "chassis_id": neighbor.chassis_id,
        "port_id": neighbor.port_id,
        "port_desc": neighbor.port_desc,
        "sys_name": neighbor.sys_name,
        "sys_desc": neighbor.sys_desc,
        "capabilities": sorted(neighbor.cap_enabled),
        "cap_known": neighbor.cap_known,
        "mgmt_ip": neighbor.mgmt_ip,
        "mgmt_ips": list(neighbor.mgmt_ips),
        # how many lldpRemTable rows this one device sent
        "rows": neighbor.rows,
        # who made it, when the chassis id is a MAC the register knows
        "vendor": rt.vendors.lookup(neighbor.chassis_id).name,
    }


@router.get("/api/stp")
async def api_stp() -> dict:
    """Spanning tree per switch plus the verdict for the network.

    Deliberately flat: whether the switch's tree is operating at all
    comes first, and root / cost / root port are empty strings for a
    switch where they would be meaningless.
    """
    return rt.state.as_dict()["stp"] or {
        "verdict": {"verdict": "not_operating", "roots": {}, "operating": [],
                    "total": 0},
        "switches": [],
    }


@router.get("/api/search")
async def api_search(q: str = Query(default="")) -> dict:
    results = rt.state.search(q)
    # …and by maker: "mercusys" finds every MERCUSYS device
    needle = q.strip().lower()
    if needle:
        seen = {r.get("mac") for r in results}
        topo = rt.state.as_dict()
        for host in topo["hosts"] + topo.get("unlocated", []):
            name = rt.vendors.lookup(host["mac"]).name
            if name and needle in name.lower() and host["mac"] not in seen:
                results.append({"type": "host", **host, "vendor": name})
                seen.add(host["mac"])
    return {"query": q, "results": results}


@router.get("/api/skipped-oids")
async def api_skipped_oids() -> dict:
    """OIDs MoonLan has stopped asking for, and for how much longer.

    The pause lives in the running process, so `diag --skipped` asks
    the service rather than guessing: a separate CLI run has its own
    collector and has never seen any of this.
    """
    collector = rt._collector
    paused = collector.paused_oids() if collector is not None else []
    return {
        "strikes": rt.config.snmp.dead_oid_strikes,
        "cooldown_scans": rt.config.snmp.dead_oid_cooldown_scans,
        "polled": collector is not None,
        "paused": paused,
        # walks an agent breaks (v0.8.8): asked for every scan — the
        # loop costs one request now — but the table is left out
        "walk_faults": collector.walk_faults() if collector is not None else [],
    }


@router.get("/api/status")
async def api_status() -> dict:
    try:
        open_fds, rss_kb = resource_usage()
    except OSError:
        open_fds, rss_kb = 0, 0
    return {
        "version": __version__,
        "demo": rt.config.demo,
        "switch_count": len(rt.state.switches),
        "host_count": len(rt.state.hosts),
        # every record off the map, as before; and the two lists of
        # the page (v0.8.7): there now without a place, and long gone
        "unlocated_count": len(rt.state.unlocated),
        "unlocated_recent": sum(
            1 for h in rt.state.unlocated if h.get("recent")
        ),
        "unlocated_gone": sum(
            1 for h in rt.state.unlocated if not h.get("recent")
        ),
        "last_scan": rt.state.last_scan,
        "last_scan_ok": rt.state.last_scan_ok,
        "last_error": rt.state.last_error,
        "last_error_ts": rt.state.last_error_ts,
        **rt.state.scan_progress(),
        "layout_saved_at": await asyncio.to_thread(rt.db.layout_saved_at),
        "uptime_hint": time.time(),
        "open_fds": open_fds,
        "rss_kb": rss_kb,
    }
