"""The switches on the map (v0.8.14).

A node for every configured switch with no reading, the old names of
synthetic ports, the journal's record of a link the inference took
back, and the spanning tree: per switch, judged across the network,
reported. Moved out of server.py word for word (docs/ARCHITECTURE.md);
the state is read as rt.X when they run. Nothing here imports
moonlan.server.
"""

from __future__ import annotations

import asyncio
import json
import logging

from .. import stp
from ..about import STARTED_AT
from ..derived import configured_switches
from ..runtime import rt
from ..snmp_collector import STATE_FRESH, STATE_NEVER, SwitchData
from ..topology import port_name

log = logging.getLogger("moonlan")


def _stub_switches(
    collected: list[SwitchData], hosts: list[dict]
) -> list[dict]:
    """A node for every switch with no full reading anywhere (v0.8.7).

    It used to vanish: no reading, nothing drawn, and its name went to
    "Notes without a node". It is drawn now with what is known — its
    address, the note on it — and nothing that is not. Where the MAC
    of its management address is in another switch's table, it stands
    on that port, the way a device would.
    """
    drawn = {sw.ip for sw in collected if sw.reachable}
    by_ip = {sw.ip: sw for sw in collected}
    where = {
        h["mac"]: h for h in hosts if h.get("switch") and h.get("port")
    }
    stubs = []
    for ip in configured_switches():
        if ip in drawn:
            continue
        sw = by_ip.get(ip) or SwitchData(ip=ip, state=STATE_NEVER)
        attached = None
        for mac in sorted(sw.own_macs):
            host = where.get(mac)
            if host:
                attached = {
                    "switch": host["switch"], "port": host["port"],
                    "mac": mac,
                }
                break
        stubs.append({
            "ip": ip,
            "name": ip,
            "named": False,
            "model": "",
            "mac": "",
            "descr": "",
            "stp_operating": False,
            "stp_root": False,
            "state": STATE_NEVER,
            "answered": sw.answered,
            "down_since": sw.down_since,
            "over_budget": bool(sw.over_budget),
            "polled_at": 0.0,
            "over_budget_scans": sw.over_budget_scans,
            "ports_total": 0,
            "ports_up": 0,
            "port_prefix": "",
            "attached": attached,
            # for the card: since when, and against which budget
            "started_at": STARTED_AT,
            "budget_seconds": rt.config.host_budget(ip),
        })
    return stubs


async def _retire_port_names(collected: list[SwitchData]) -> None:
    """The old names of synthetic ports, after each full poll (v0.8.6).

    Only a switch read in full this scan: a saved reading is not news
    about which aggregates exist. "bridge-port N" of an aggregate takes
    its new name ("LAG 1/25+1/26"); any other "bridge-port N" and every
    "-N" is reset — see Database.retire_port_names.
    """
    for sw in collected:
        if sw.map_state != STATE_FRESH:
            continue
        renames = {
            f"bridge-port {bridge_port}": sw.ports[-bridge_port].name
            for bridge_port in sw.lag_groups
            if -bridge_port in sw.ports and sw.ports[-bridge_port].name
        }
        counts = await asyncio.to_thread(
            rt.db.retire_port_names, sw.ip, renames
        )
        if any(counts.values()):
            log.info(
                "%s: port names from before v0.8.6 — %d device(s) moved to "
                "the aggregate's new name, %d reset to no port (listed off "
                "the map until seen again), %d layout/note key(s) renamed",
                sw.ip, counts["renamed"], counts["reset"], counts["keys"],
            )


def _dropped_link_details(dropped: dict) -> str:
    """The journal's record of a link the inference took back: the
    link, why ("behind" — LLDP puts one end behind another switch;
    "cycle" — the weakest edge of a ring nothing explains) and what
    the reason names. Data, not a sentence (v0.8.2): the page words it
    in its own language. The log says it in English when it happens."""
    return json.dumps({
        key: dropped[key]
        for key in ("a", "a_port", "b", "b_port", "source", "reason", "behind", "cycle")
        if dropped.get(key) not in (None, "")
    })


def _per_switch_stp(collected: list[SwitchData]) -> dict:
    """ip -> StpData for every switch that answered."""
    return {
        sw.ip: sw.stp for sw in collected
        if sw.reachable and sw.stp is not None
    }


def _judge_stp(collected: list[SwitchData]) -> None:
    """The cross-switch test, run once the whole network is in hand.

    A switch that names itself root is believed only when a neighbour
    names it too (see stp.judge_network). It mutates the StpData
    objects in place, so everything downstream — the map, the panel,
    the alarms — sees the same verdict, provided it runs first.
    """
    stp.judge_network(_per_switch_stp(collected))


def _stp_report(
    collected: list[SwitchData], trunk_names: dict[str, list[str]] | None = None
) -> dict:
    """Spanning tree of every polled switch plus the network verdict.

    The root, the cost and the root port are reported only for a switch
    whose tree is actually operating. A switch with STP turned off
    answers every dot1dStp* object, names itself root and reports cost
    0 — believing that is how five disabled switches turn into five
    root bridges (see moonlan/stp.py).
    """
    per_switch = _per_switch_stp(collected)
    verdict = stp.network_verdict(per_switch)
    trunk_names = trunk_names or {}
    switches = []
    for ip, data in sorted(per_switch.items()):
        sw = rt.switch_data.get(ip)
        own_macs = sw.own_macs if sw else set()
        # The VLANs of this switch's trunk ports. Untagged BPDUs are
        # handled in the VLAN of the port they arrive on, so two
        # segments whose trunks sit in different VLANs never meet and
        # form two trees legitimately. With the numbers in front of
        # them, an operator can tell that case from a real break
        # without going to the switch.
        trunk_vlans = sorted({
            sw.port_pvid[i]
            for i in (sw.ports if sw else ())
            if port_name(sw, i) in trunk_names.get(ip, ())
            and sw.port_pvid.get(i)
        }) if sw else []
        switches.append({
            "ip": ip,
            "name": sw.sys_name or ip if sw else ip,
            "supported": data.supported,
            "operating": data.operating,
            # the English sentence for an old page; a page of v0.8.2 or
            # later words the verdict itself from the code and numbers
            "reason": data.reason,
            "reason_code": data.reason_code,
            "reason_params": data.reason_params,
            "is_root": data.is_root(own_macs),
            "protocol": data.protocol_name,
            "version": data.version_name,
            "priority": data.priority,
            "designated_root": data.designated_root,
            # the agent put the priority in the wrong byte and MoonLan
            # corrected it; the panel says so rather than hiding it
            "root_nonstandard": data.root_nonstandard,
            # its neighbours established this, not its own answers:
            # the numbers beside it are zeros it cannot vouch for
            "confirmed_root": data.confirmed_root,
            "root_cost": data.root_cost,
            "root_port": (
                port_name(sw, data.ports[data.root_port].if_index)
                if sw and data.root_port in data.ports
                and data.ports[data.root_port].if_index is not None
                else str(data.root_port) if data.root_port else ""
            ),
            "top_changes": data.top_changes,
            "time_since_change": data.time_since_change / 100.0,
            "sys_uptime": data.sys_uptime / 100.0,
            "blocking_ports": [
                p.name or str(p.bridge_port) for p in data.blocking_ports()
            ],
            "trunk_vlans": trunk_vlans,
            # answers dot1dStp* and names no root: shown, but not
            # counted as a tree of its own
            "rootless": ip in verdict.get("rootless", ()),
        })
    return {"verdict": verdict, "switches": switches}
