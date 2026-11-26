"""What both the API and the scan derive from the state (v0.8.13).

The helpers that the routes taken out of server.py need and that the
scan, still in server.py, needs as well: the uplink ports, the window
of MAC copies and the inbound errors of a subject, whether a host is
monitored, the LAG groups of a switch, sums and rounding for the
counters, how long rates stay shown, the loop report of a switch.
Moved out of server.py word for word (docs/ARCHITECTURE.md): the state
is read as rt.X when they run. Nothing here imports moonlan.server.
"""

from __future__ import annotations

import time

from . import demo
from .config import parse_uplink_ports
from .runtime import rt
from .snmp_collector import SwitchData
from .topology import port_name


def _uplink_ports() -> set[tuple[str, str]]:
    """Ports that leave the network.

    The demo answers with its own and nothing else: a real config.yaml
    naming a port of the real network would otherwise put an empty
    "External network" node on the demo map.
    """
    if rt.config.demo:
        return set(demo.UPLINK_PORTS)
    return parse_uplink_ports(rt.config.uplink_ports)


def _copies_window() -> float:
    """What "inbound errors in N min" counts over: one scan interval —
    the copies a scan finds were learned since the one before it."""
    return (rt.config.scan_interval_minutes or 10) * 60


def _subject_inbound_errors(subject: str) -> int | None:
    """Inbound errors of the port "switch ip:port" over _copies_window();
    None while the counters do not know."""
    ip, _sep, port = subject.partition(":")
    sw = rt.switch_data.get(ip)
    if sw is None:
        return None
    if_index = next(
        (i for i in sw.ports if port_name(sw, i) == port), None
    )
    if if_index is None:
        return None
    return rt.counter_store.inbound_errors(
        ip, if_index, time.time() - _copies_window()
    )


def _effective_monitored(row: dict) -> bool:
    """monitored_by_default=true restores the old alert-on-everything
    behavior regardless of the per-host flag."""
    return rt.config.monitored_by_default or bool(row.get("monitored", 0))


def _lag_groups(sw: SwitchData) -> dict[int, list[int]]:
    """Aggregate ifIndex -> member ifIndexes: both IEEE8023-LAG-MIB
    aggregates and D-Link trunks inferred from bridge-port gaps
    (keyed by the negative synthetic ifIndex)."""
    groups: dict[int, list[int]] = {}
    for member, aggregate in sw.lag_members.items():
        groups.setdefault(aggregate, []).append(member)
    for bridge_port, members in sw.lag_groups.items():
        groups.setdefault(-bridge_port, []).extend(members)
    return groups


def _sum_known(values) -> float | None:
    """Sum of an aggregate's members, or None if any of them is unknown.

    A LAG total built from three members out of four is not a total,
    and the utilization rule would read it as headroom.
    """
    total = 0.0
    for value in values:
        if value is None:
            return None
        total += value
    return total


def _rates_hide_age() -> float:
    """Past this age a measured rate stops being shown at all.

    It used to be three counters intervals, and anything older was
    dropped from the answer — which drew the same "—" as a column the
    agent never answered. Half an hour is the point at which a rate
    really is a memory rather than a measurement; everything younger is
    shown with its age next to it.
    """
    return max(rt.config.stale_rate_hide_minutes, 0.0) * 60


def _round(value: float | None) -> float | None:
    """None survives rounding: it means "the agent did not answer"."""
    return None if value is None else round(value, 1)


def _loop_report(sw: SwitchData | None) -> dict:
    """Loop detection of one switch as the UI sees it.

    A switch whose model says nothing over SNMP is reported as saying
    nothing. "No loops" is a claim, and this service does not make
    claims it cannot back — the same rule the STP verdict follows.
    """
    loop = sw.loop_detection if sw is not None else None
    if loop is None or not loop.supported:
        return {
            "supported": False,
            # "nobody has asked yet" is not "the model does not
            # answer": before the first counters cycle there is no
            # evidence either way, and saying otherwise is the same
            # mistake in miniature
            "status": "unsupported" if loop is not None else "not_polled",
            "sys_object_id": (
                loop.sys_object_id if loop is not None
                else (sw.sys_object_id if sw is not None else "")
            ),
            "polled": loop is not None,
        }
    return {
        "supported": True,
        "polled": True,
        "status": loop.status,
        "profile": loop.profile,
        "matched_by": loop.matched_by,
        "sys_object_id": loop.sys_object_id,
        "root": loop.root,
        "enabled": loop.enabled,
        "mode": loop.mode,
        "interval": loop.interval,
        "recover_time": loop.recover_time,
        "watched": sum(
            1 for p in loop.ports.values() if p.lbd_enabled is True
        ),
        "ports_total": len(loop.ports),
        "looped_ports": [
            port_name(sw, p.if_index) for p in loop.looped_ports()
            if p.if_index is not None
        ],
        "index_mismatch": loop.index_mismatch,
        "truncated": loop.truncated,
    }
