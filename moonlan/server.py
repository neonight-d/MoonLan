"""MoonLan web service: REST API and the static web UI."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import (
    __version__, corruption, counters, demo, loopdetect, pinger, stp,
)
from .alarms import AlarmEngine
from .config import Config, load_config, parse_uplink_ports
from .db import Database
from .notify import Notifier
from .snmp_collector import (
    SnmpCollector,
    SwitchData,
    is_random_mac,
    is_valid_mac,
)
from .topology import (
    FdbStability,
    TopologyState,
    build_topology,
    group_beyond_trunk,
    port_name,
    suspect_uplink_ports,
)

log = logging.getLogger("moonlan")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

state = TopologyState()
config: Config = load_config()
# In demo mode the DB lives in memory so the real one is not polluted
db = Database(":memory:" if config.demo else config.db_path)

# Ping state of switches (they are not in the hosts table): ip -> {ping_up, last_ping_ok}
switch_ping: dict[str, dict] = {}

# FDB merged with previous polls: protects links from MAC table aging
fdb_stability = FdbStability()

# Latest raw poll data per switch: the ports API, the counters loop and
# the link load labels all need port lists, speeds and LAG composition
switch_data: dict[str, SwitchData] = {}

counter_store = counters.CounterStore()
# Link-state transitions per port over a sliding window: a port can
# bounce four times between two counter polls, and oper status alone
# would show none of it
flap_tracker = counters.FlapTracker(
    config.thresholds.flap_window_minutes * 60
)
# (switch ip, port name) -> the flap count shown in the ports panel
flap_by_port: dict[tuple[str, str], counters.PortFlaps] = {}
ZERO_FLAPS = counters.PortFlaps(count=0, last=0.0)

# switch ip -> which counter columns answered in the latest poll, so the
# ports panel can say "unknown" where it used to say 0.0
counter_columns: dict[str, dict[str, counters.ColumnStatus]] = {}
notifier = Notifier(config, demo=config.demo)
alarm_engine = AlarmEngine(
    db, notifier, config.thresholds, config.notifications
)
demo_counters = demo.DemoCounters() if config.demo else None

# The first scan is the initial inventory: every MAC is "new" there,
# alerting on all of them would be pure noise
first_scan_done = False

# MACs present in the FDB of the latest scan. A host missing from it is
# "stale": still drawn at its last known port during the grace window,
# but never alarmed on — its presence is no longer confirmed.
fdb_macs: set[str] = set()

# (switch ip, port) of the pseudo-switches drawn last time: a group
# that already exists must not vanish while any device is left on the
# port, or the nodes would jump around between scans
prev_pseudo_ports: set[tuple[str, str]] = set()

# (switch ip, port) -> the distorted MACs of the latest scan, for the
# ports panel and the diagnostics
suspect_by_port: dict[tuple[str, str], list[dict]] = {}

# (switch ip, port) -> why the port looks like a way out of the network
# and might belong in config.uplink_ports
uplink_suspects: dict[tuple[str, str], dict] = {}

# Saved node positions are cleaned up once, after the first scan —
# not at startup proper. The rule is "old AND no longer on the map",
# and before the first scan there is no map: every position would look
# orphaned and the whole layout would be thrown away on a restart.
layout_purged = False

# Loop-detection profiles: the built-in ones plus whatever
# config.yaml adds or overrides by name (see loopdetect.py)
_loop_profiles: list | None = None


def loop_profiles() -> list:
    global _loop_profiles
    if _loop_profiles is None:
        extra, problems = loopdetect.parse_profiles(
            config.loop_detection.profiles
        )
        for problem in problems:
            log.warning("config.yaml loop_detection.profiles: %s", problem)
        _loop_profiles = loopdetect.merge_profiles(
            loopdetect.BUILTIN_PROFILES, extra
        )
        log.info(
            "Loop detection profiles: %s",
            ", ".join(p.name for p in _loop_profiles),
        )
    return _loop_profiles

# One SnmpEngine per process: a new engine per cycle leaks sockets and
# MIB state (OSError 24, MibNotFoundError, growing RSS). Recreate only
# if the SNMP config ever changes at runtime — it currently cannot.
_collector: SnmpCollector | None = None


# One poll per switch at a time. A DGS-1210 answers one request at a
# time, and the scan and the counters loop can otherwise land on the
# same agent together. Different switches stay parallel — this is a
# lock per IP, not a global one.
_host_locks: dict[str, asyncio.Lock] = {}


def host_lock(ip: str) -> asyncio.Lock:
    lock = _host_locks.get(ip)
    if lock is None:
        lock = _host_locks[ip] = asyncio.Lock()
    return lock


def get_collector() -> SnmpCollector:
    global _collector
    if _collector is None:
        _collector = SnmpCollector(
            community=config.snmp.community,
            timeout=config.snmp.timeout,
            retries=config.snmp.retries,
            retries_on_break=config.snmp.retries_on_break,
            per_host=config.switch_snmp,
            dead_oid_strikes=config.snmp.dead_oid_strikes,
            dead_oid_cooldown_scans=config.snmp.dead_oid_cooldown_scans,
        )
    return _collector


async def _collect_locked(collector: SnmpCollector, ip: str) -> SwitchData:
    """A full poll, holding this switch's lock for its duration."""
    async with host_lock(ip):
        return await collector.collect(ip)


async def _collect_within_budget(
    collector: SnmpCollector, ip: str, budget: float
) -> SwitchData | None:
    """A full poll, or None when it ran past its budget.

    `snmp.timeout` bounds one request. A poll is a dozen walks of a
    dozen requests each, so an agent that answers everything slowly
    stays inside every single timeout and still takes eight minutes —
    and `gather` waits for the last one. Nothing here makes that agent
    faster; it only stops it from deciding when the rest of the
    network gets its map.
    """
    started = time.monotonic()
    try:
        data = await asyncio.wait_for(
            _collect_locked(collector, ip), budget
        )
        # How long a complete poll really takes, so budgets can be set
        # from measurements instead of guesses
        data.poll_seconds = time.monotonic() - started
        return data
    except asyncio.TimeoutError:
        log.warning(
            "%s did not finish its poll within the %.0f s budget (gave up "
            "after %.0f s) — it is left out of this scan, which is not the "
            "same as not answering: see snmp.host_budget_seconds",
            ip, budget, time.monotonic() - started,
        )
        return None
    finally:
        state.host_polled()


def _counters_wait_budget() -> float:
    """How long a counters cycle waits for a switch the scan is holding.

    Long enough to outlast a normal scan of one host, short enough that
    cycles never pile up: half an interval, capped at twenty seconds.
    """
    return min(max(config.counters_interval_seconds, 2) / 2, 20)


async def _counters_locked(collector: SnmpCollector, ip: str):
    """A counters poll, waiting briefly if the scan holds this switch.

    Giving up the moment the lock was taken had a consequence nobody
    intended: a scan holds a host for as long as its budget allows, so
    any budget of two intervals or more guaranteed that this host
    missed cycles — and before v0.6.13 a missed cycle meant its whole
    panel went to dashes. A short wait catches the common case where
    the scan is nearly done with it.

    Queueing indefinitely is still not an option: cycles would pile up
    behind a slow host. Past the wait it is skipped, as before, and
    says so.
    """
    lock = host_lock(ip)
    wait = _counters_wait_budget()
    try:
        await asyncio.wait_for(lock.acquire(), wait)
    except asyncio.TimeoutError:
        log.info(
            "%s is still busy with the topology scan after %.0f s — "
            "skipping this counters cycle rather than queueing behind it. "
            "Its rates stay on the panel with their age; a poll budget at "
            "or above twice counters_interval_seconds makes this happen "
            "after every scan.", ip, wait,
        )
        return {}, {}, {}, None
    try:
        sw = switch_data.get(ip)
        # The interface table from the last scan: without it a truncated
        # column has no way of knowing which ports it failed to reach.
        # Real interfaces only — a negative ifIndex is one of our own
        # synthetic aggregates, which SNMP has never heard of.
        expected = (
            {p.if_index for p in sw.ports.values() if p.if_index > 0}
            if sw else None
        )
        samples, oper, columns = await counters.collect_samples(
            collector, ip, expected
        )
        # Loop detection rides this cycle rather than the ten-minute
        # scan: a loop is an incident, and one walk of a small vendor
        # branch is what it costs to hear about it within a minute.
        loop = None
        if config.loop_detection.enabled and sw is not None:
            loop = await loopdetect.collect_loop_detection(
                collector, ip, sw.ports, loop_profiles(),
                sys_object_id=sw.sys_object_id,
            )
        return samples, oper, columns, loop
    finally:
        lock.release()


async def run_scan() -> None:
    """One cycle of polling all switches and rebuilding the topology."""
    global first_scan_done, fdb_macs, prev_pseudo_ports
    if state.scanning:
        return
    state.scan_started(len(config.switches))
    over_budget: list[str] = []
    try:
        arp: dict[str, str] = {}
        if config.demo:
            collected = demo.demo_network()
        else:
            collector = get_collector()
            collector.begin_scan_cycle()
            results = await asyncio.gather(
                *(
                    _collect_within_budget(
                        collector, ip, config.host_budget(ip)
                    )
                    for ip in config.switches
                ),
                return_exceptions=True,
            )
            collected = []
            for ip, result in zip(config.switches, results):
                if isinstance(result, BaseException):
                    # unreachable is already handled inside collect();
                    # this is a poll that raised, and the rest of the
                    # network should still get a map
                    log.error(
                        "Poll of %s raised — it is left out of this scan",
                        ip, exc_info=result,
                    )
                    collected.append(SwitchData(ip=ip))
                    continue
                if result is None:
                    # Over budget. Not an answer, and not a silence
                    # either: the last reading that did arrive is kept
                    # and dated, so the branch behind this switch stays
                    # on the map instead of vanishing every cycle.
                    previous = switch_data.get(ip)
                    streak = (
                        previous.over_budget_scans + 1 if previous else 1
                    )
                    if previous is not None and previous.reachable:
                        previous.over_budget = True
                        previous.over_budget_scans = streak
                        collected.append(previous)
                    else:
                        # Nothing to keep — this one has never been read
                        # in full. The streak still counts: a switch
                        # that has never finished a poll is further from
                        # fine than one whose data is merely old.
                        collected.append(
                            SwitchData(ip=ip, over_budget=True,
                                       over_budget_scans=streak)
                        )
                    continue
                collected.append(result)
            if config.routers:
                arp = await collect_arp(collector)
            # The MAC of the switch's management IP is also its MAC:
            # neighbors see the switch under it in their FDB tables
            ip_to_mac = {ip: mac for mac, ip in arp.items()}
            for sw in collected:
                mac = ip_to_mac.get(sw.ip)
                if mac:
                    sw.own_macs.add(mac)
        # Demo mode marks a switch over budget too, so the list comes
        # from the data rather than from the polling loop
        over_budget = [sw.ip for sw in collected if sw.over_budget]
        for sw in collected:
            previous = switch_data.get(sw.ip)
            if previous is not None and sw.loop_detection is None:
                # Loop detection is refreshed by the counters cycle,
                # not by the scan. A fresh SwitchData carries none, and
                # dropping the last one made every switch report "the
                # model does not say" for up to a minute after every
                # scan — a claim about hardware, made because of our
                # own bookkeeping.
                sw.loop_detection = previous.loop_detection
            switch_data[sw.ip] = sw
        # The cross-switch spanning-tree test comes BEFORE the map is
        # built, because the map reads its results. A root recognised
        # only because its neighbours follow its address (v0.6.11) has
        # `operating` and `confirmed_root` set by judge_network — and
        # judge_network used to run after build_topology, so the node
        # was drawn with a plain border, no root caption and its
        # blocking ports ignored, while the STP panel two panels away
        # called it the root. One order of operations, three wrong
        # fields.
        _judge_stp(collected)
        # A MAC has to be seen more than once before it counts as a
        # device (unless ARP vouches for it); the verdict is needed
        # before the topology so that unconfirmed addresses stay out of
        # the per-port device counts as well as off the map
        db_rows = await asyncio.to_thread(db.hosts_by_mac)
        # An empty database is the initial inventory — there is no map
        # yet for a phantom to pollute, and waiting would only show the
        # operator an empty screen on the first poll
        confirm_scans = config.new_host_confirm_scans if db_rows else 1
        # Only switches actually read this cycle. A switch that ran out
        # of budget contributes the table it produced last time, and
        # counting a MAC again out of a copy of one reading is how an
        # address gets "seen in three polls" without anyone looking for
        # it twice.
        unconfirmed = await asyncio.to_thread(
            db.projected_unconfirmed,
            {
                mac for sw in collected
                if sw.reachable and not sw.over_budget
                for mac in sw.fdb
            },
            confirm_scans,
        )
        # What the database already knows about each port feeds the
        # unmanaged-switch threshold, so groups survive FDB aging
        (
            switches, links, hosts, pseudo_switches, vlan_names, bridges,
            topo_info,
        ) = build_topology(
            collected,
            config.unmanaged_threshold,
            fdb_stability=None if config.demo else fdb_stability,
            known_hosts_per_port=_known_hosts_per_port(
                db_rows, {sw.ip for sw in collected if sw.reachable}
            ),
            sticky_pseudo_ports=prev_pseudo_ports,
            unconfirmed_macs=unconfirmed,
            place_trunk_only=config.place_trunk_only_hosts,
            uplink_ports=_uplink_ports(),
            remembered_locations=_remembered_locations(db_rows),
        )
        # Links the inference removed, and rings it could not resolve.
        # The syslog line is for the moment it happens; the journal is
        # so the history of these decisions can be read from the
        # interface, where the map they changed is.
        for dropped in topo_info.get("dropped_links", []):
            await asyncio.to_thread(
                db.add_event, time.time(), "link_dropped", "",
                _dropped_link_text(dropped),
            )
        prev_pseudo_ports = {
            (p["switch"], p["port"]) for p in pseudo_switches
        }
        # Addresses a bit or two away from a real one on the same port:
        # a failing cable, not new devices. They are held back from
        # confirmation for as long as they look like copies — otherwise
        # a permanently damaged port would simply confirm its phantoms.
        suspects = corruption.find_suspects(
            hosts, unconfirmed, config.thresholds.corruption_hamming_bits
        )
        _apply_suspects(hosts, suspects)
        suspect_macs = {
            s["mac"] for found in suspects.values() for s in found
        }
        # Devices seen only on uplinks are not on the map, but they
        # were seen: the sighting is recorded with no location at all,
        # so the inventory keeps them without anyone claiming a port.
        # `approximate` is what stops the empty location from
        # overwriting whatever the database already holds.
        uplink_only = topo_info.get("uplink_only", {})
        stale_switches = {
            sw.ip for sw in collected if sw.reachable and sw.over_budget
        }
        seen_nowhere = [
            {"mac": mac, "switch": "", "port": "", "vlan": 0,
             "approximate": True}
            for mac, sightings in uplink_only.items()
            # every sighting of it came out of a saved table: nobody
            # saw this address either
            if any(ip not in stale_switches for ip, _port in sightings)
        ]
        # A device behind a switch that ran out of budget is drawn from
        # the reading that did arrive, and that is right — the map must
        # not lose a whole branch every cycle. Recording it as a
        # sighting is a different matter: "last seen" is a moment in
        # time, seen_count counts polls a MAC was found in, and a
        # confirmation is the claim that several polls agree. None of
        # the three survives being fed the same reading twice.
        observed = [h for h in hosts if not h.get("from_saved")]
        copied = len(hosts) - len(observed)
        if copied:
            log.info(
                "%d device(s) behind %d switch(es) that ran out of budget "
                "are drawn from saved readings: they stay on the map, and "
                "none of it is recorded as a sighting",
                copied, len(stale_switches),
            )
        new_macs = await asyncio.to_thread(
            db.upsert_hosts, observed + seen_nowhere, confirm_scans,
            suspect_macs if config.filter_suspect_macs else set(),
        )
        if config.demo:
            await asyncio.to_thread(demo.enrich_db, db, hosts)
        else:
            if arp:
                # ARP knows devices no switch port ever showed (behind a
                # router or an unpolled switch): keep them as inventory
                # with an empty switch_ip. Switches and routers
                # themselves are map nodes, not hosts.
                infra_ips = set(config.switches) | set(config.routers)
                own_macs = _switch_macs()
                arp_hosts = {
                    mac: ip for mac, ip in arp.items()
                    if mac not in own_macs and ip not in infra_ips
                }
                created = await asyncio.to_thread(
                    db.set_ips, arp_hosts, True
                )
                if created:
                    log.info(
                        "ARP: %d devices known but not seen on any switch "
                        "port", created,
                    )
            await resolve_names()
        # ARP vouches for an address, so a newcomer it knows joins the
        # map in the same poll it first appeared in
        new_macs += await asyncio.to_thread(db.confirm_hosts_with_ip)
        if new_macs:
            log.info("New MACs confirmed: %d", len(new_macs))
        unconfirmed = await asyncio.to_thread(db.unconfirmed_macs)
        if unconfirmed:
            log.info(
                "MACs awaiting confirmation (%d poll(s) each): %d",
                confirm_scans, len(unconfirmed),
            )
        if not config.filter_suspect_macs:
            unconfirmed -= suspect_macs  # draw the damage instead
        hosts = [h for h in hosts if h["mac"] not in unconfirmed]
        fdb_macs = {h["mac"] for h in hosts}
        db_rows = await asyncio.to_thread(db.hosts_by_mac)
        await _release_unconfirmed_ips(db_rows)
        hosts, offline_groups, unlocated, trunk_groups = _assemble_hosts(
            hosts, db_rows, pseudo_switches, {sw["ip"] for sw in switches},
            unconfirmed,
            {h["mac"]: h["merged_into"] for h in hosts if h.get("merged_into")},
            _nodes_on_port(bridges, pseudo_switches),
            uplink_only,
            topo_info.get("trunk_names", {}),
        )
        # A group that sits on a trunk was never seen there as devices:
        # its location is inherited guesswork, and the card should not
        # claim otherwise
        trunk_names = topo_info.get("trunk_names", {})
        for group in offline_groups:
            group["approximate"] = group["port"] in trunk_names.get(
                group["switch"], []
            )
        _merge_db_fields(hosts, db_rows)
        _merge_db_fields(unlocated, db_rows)
        # LLDP: remember the neighbours and the bridges nobody polls, so
        # a card can say since when a device has been behind that port
        now = time.time()
        await asyncio.to_thread(db.upsert_lldp, _lldp_rows(collected), now)
        await asyncio.to_thread(
            db.upsert_bridges,
            [
                {
                    "chassis_id": b["chassis_id"],
                    "sys_name": b["name"],
                    "sys_desc": b["sys_desc"],
                    "mgmt_ip": b["mgmt_ip"],
                    "switch_ip": b["switch"],
                    "port": b["port"],
                    "capabilities": ",".join(b["capabilities"]),
                }
                for b in bridges
            ],
            now,
        )
        bridge_rows = await asyncio.to_thread(db.bridges)
        for bridge in bridges:
            row = bridge_rows.get(bridge["chassis_id"], {})
            bridge["first_seen"] = row.get("first_seen", now)
            bridge["last_seen"] = row.get("last_seen", now)
            bridge["known"] = _is_known_bridge(bridge)
        _merge_bridge_identity(bridges, hosts, db_rows)
        # the address the operator reaches a router at, for the caption
        router_ips = _router_addresses(arp)
        for host in hosts:
            if host["mac"] in router_ips:
                host["router_ip"] = router_ips[host["mac"]]
        for bridge in bridges:
            if bridge["chassis_id"] in router_ips:
                bridge["router_ip"] = router_ips[bridge["chassis_id"]]
        # Ports that look like a way out but are not configured as one.
        # Addresses are only known once the DB has been merged in, so
        # this happens here rather than inside build_topology.
        uplink_suspects.clear()
        uplink_suspects.update(
            suspect_uplink_ports(
                [sw for sw in collected if sw.reachable],
                [
                    (h["switch"], h["port"], h.get("ip", ""))
                    for h in hosts if h.get("switch") and h.get("port")
                ],
                _uplink_ports(),
                infrastructure=_infrastructure(),
                infrastructure_macs=set(router_ips) | {
                    mac for mac, ip in arp.items() if ip in _infrastructure()
                },
            )
        )
        for (sw_ip, port), why in sorted(uplink_suspects.items()):
            log.info(
                "%s port %s looks like a way out of the network (%s). Add "
                "\"%s:%s\" to uplink_ports and what is behind it becomes "
                "one \"External network\" node instead of a crowd of "
                "devices, and raises no bridge alarms.",
                sw_ip, port, why["text"], sw_ip, port,
            )
        stp_report = _stp_report(collected, topo_info.get("trunk_names", {}))
        state.update(
            switches, links, hosts, pseudo_switches, vlan_names, unlocated,
            offline_groups, bridges, stp_report,
            topo_info.get("external_networks", []),
            trunk_groups,
        )
        if config.demo:
            await run_ping()  # set the switches' ping state right away

        # A switch that ran out of budget is not reported either way.
        # "Missed an SNMP poll" is a statement about the switch; this
        # one is a statement about us, and switch_down must not be
        # raised on the strength of it — nor cleared, which would be
        # just as much of an invention.
        await alarm_engine.on_scan(
            {sw.ip: sw.reachable for sw in collected if not sw.over_budget},
            {sw.ip: sw.sys_name or sw.ip for sw in collected},
        )
        # Every single switch silent at once is not ten faults, it is
        # one. The day `community: public` went back into the config,
        # the map emptied and the log said "does not respond to SNMP"
        # ten times over — true, unhelpful, and identical to what a
        # power cut would have printed.
        answered = [sw for sw in collected if sw.reachable]
        if collected and not answered and not over_budget:
            log.error(
                "All %d configured switch(es) are unreachable at once. One "
                "common cause is likelier than %d simultaneous faults: "
                "snmp.community (SNMPv2c does not answer a wrong one at "
                "all — silence is what a mismatch looks like), or the "
                "network of this machine. `python -m moonlan.diag --walk "
                "<switch> 1.3.6.1.2.1.1.5` says which.",
                len(collected), len(collected),
            )
        # A switch that keeps answering and keeps not finishing. It is
        # on the map, from a reading that may be hours old, and until
        # now the only way to learn that was to read the journal.
        stale_switches = {
            sw.ip: {
                "scans": sw.over_budget_scans,
                "polled_at": sw.polled_at,
            }
            for sw in collected
            if sw.over_budget
            and sw.over_budget_scans >= max(config.stale_switch_scans, 1)
        }
        await alarm_engine.on_stale_switches(
            stale_switches,
            {sw.ip: sw.sys_name or sw.ip for sw in collected},
        )
        if over_budget:
            log.warning(
                "Scan finished without %d of %d switch(es): %s did not "
                "answer in full inside the budget. The map below is "
                "everything else, and their last readings are kept as "
                "they were.",
                len(over_budget), len(config.switches),
                ", ".join(over_budget),
            )
        await alarm_engine.clear_suppressed(
            _suppressed_bridges(bridges, bridge_rows)
        )
        await alarm_engine.on_bridges(bridges, _known_bridge_ids())
        await alarm_engine.on_stp(stp_report)
        await alarm_engine.on_corruption(
            {f"{sw_ip}:{port}": found
             for (sw_ip, port), found in suspects.items()}
        )
        if new_macs and first_scan_done:
            rows = await asyncio.to_thread(db.hosts_by_mac)
            await alarm_engine.on_new_macs(
                new_macs,
                {
                    mac: f"{rows[mac]['switch_ip']} / {rows[mac]['port']}"
                    for mac in new_macs if mac in rows
                },
            )
        first_scan_done = True
        log.info(
            "Scan finished: %d switches, %d links, %d hosts",
            len(switches), len(links), len(hosts),
        )
        await _purge_layout_once()
    finally:
        state.scan_ended(over_budget)


def _dropped_link_text(dropped: dict) -> str:
    """One journal line about a link the inference took back."""
    a = f"{dropped['a']} [{dropped['a_port']}]"
    b = f"{dropped['b']} [{dropped['b_port']}]"
    if dropped.get("reason") == "behind":
        return (
            f"{a} — {b} ({dropped.get('source', 'fdb')}): LLDP puts "
            f"{dropped['b']} behind {dropped['behind']}, so it cannot "
            f"also hang off {dropped['a']}"
        )
    return (
        f"{a} — {b} ({dropped.get('source', 'fdb')}): "
        f"{dropped.get('reason', 'removed by the topology inference')}"
    )


async def _purge_layout_once() -> None:
    """Forgets positions of nodes that are both old and gone.

    Runs after the first scan of this process, because that is the
    first moment anything knows which nodes exist. A device switched
    off for the night keeps its place however long the night is; only
    age decides, and only for something the map no longer has.
    """
    global layout_purged
    if layout_purged:
        return
    layout_purged = True
    gone = await asyncio.to_thread(
        db.purge_layout, config.layout_keep_days, _layout_node_ids()
    )
    if gone:
        log.info(
            "Map layout: forgot the saved position of %d node(s) not seen "
            "for over %.0f day(s) and no longer on the map: %s",
            len(gone), config.layout_keep_days,
            ", ".join(sorted(gone)[:10])
            + (" and more" if len(gone) > 10 else ""),
        )


def _lldp_rows(collected: list[SwitchData]) -> list[dict]:
    """This poll's LLDP neighbours, flattened for the database."""
    rows: list[dict] = []
    for sw in collected:
        if not sw.reachable:
            continue
        for neighbor in sw.lldp_neighbors:
            rows.append({
                "switch_ip": sw.ip,
                "local_port": (
                    port_name(sw, neighbor.local_ifindex)
                    if neighbor.local_ifindex is not None
                    else f"lldpLocPortNum {neighbor.local_port_num}"
                ),
                "chassis_id": neighbor.chassis_id,
                "port_id": neighbor.port_id,
                "port_desc": neighbor.port_desc,
                "sys_name": neighbor.sys_name,
                "sys_desc": neighbor.sys_desc,
                "capabilities": ",".join(sorted(neighbor.cap_enabled)),
                "mgmt_ip": neighbor.mgmt_ip,
            })
    return rows


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
        sw = switch_data.get(ip)
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
            "reason": data.reason,
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


def _infrastructure() -> set[str]:
    """Addresses of kit that is ours: the switches and the routers.

    In demo mode these come from the demo network, not from the
    config.yaml that happens to be in the working directory.
    """
    if config.demo:
        return set(switch_data) | set(demo.ROUTERS)
    return set(config.switches) | set(config.routers)


def _router_addresses(arp: dict[str, str]) -> dict[str, str]:
    """MAC -> the address config.routers actually reaches it at.

    A router announces a management address per VLAN interface — the
    MikroTik behind mb0 Slot0/21 sends 38 — and the first one the walk
    returns is as likely to be a segment gateway as the address anyone
    uses. The one in `routers:` is the one the operator types, so that
    is what goes under the node's name.
    """
    routers = set(config.routers)
    found = {mac: ip for mac, ip in arp.items() if ip in routers}
    if config.demo:
        found.update(demo.ROUTER_ADDRESSES)
    return found


def _merge_bridge_identity(
    bridges: list[dict], hosts: list[dict], db_rows: dict[str, dict]
) -> None:
    """Gives a bridge node the identity of the device it IS.

    Two ways the same box shows up twice. Its chassis MAC is in the FDB
    of the port LLDP found it on — the topology builder already marked
    that host as merged, and here the bridge picks up its IP, its DNS
    name and its ping state, so the node answers "is it up?" like any
    other. And its management address may sit in the inventory under a
    different MAC (a router announces one per VLAN interface): a host
    holding such an address on the bridge's own port is the same
    device, so its node goes too and the card says which addresses
    came from where.
    """
    ip_owner = {
        row["ip"]: mac for mac, row in db_rows.items() if row["ip"]
    }
    for bridge in bridges:
        row = db_rows.get(bridge["chassis_id"], {})
        bridge["ip"] = row.get("ip", "") or bridge.get("mgmt_ip", "")
        bridge["name"] = bridge.get("name") or row.get("name", "")
        bridge["dns_name"] = row.get("name", "")
        bridge["ping_up"] = bool(row.get("ping_up", 0))
        bridge["last_ping_ok"] = row.get("last_ping_ok", 0)
        if row.get("first_seen"):
            bridge["first_seen"] = min(
                bridge.get("first_seen", row["first_seen"]), row["first_seen"]
            )
        # the same box under another MAC, on this very port
        also: list[str] = []
        for address in bridge.get("mgmt_ips", []):
            mac = ip_owner.get(address)
            if not mac or mac == bridge["chassis_id"]:
                continue
            twin = db_rows.get(mac, {})
            if (twin.get("switch_ip"), twin.get("port")) != (
                bridge["switch"], bridge["port"]
            ):
                continue  # same address, but not behind this cable
            also.append(address)
            for host in hosts:
                if host["mac"] == mac:
                    host["merged_into"] = bridge["id"]
        bridge["also_ips"] = also


def _uplink_ports() -> set[tuple[str, str]]:
    """Ports that leave the network.

    The demo answers with its own and nothing else: a real config.yaml
    naming a port of the real network would otherwise put an empty
    "External network" node on the demo map.
    """
    if config.demo:
        return set(demo.UPLINK_PORTS)
    return parse_uplink_ports(config.uplink_ports)


def _suppressed_bridges(
    bridges: list[dict], bridge_rows: dict[str, dict]
) -> dict[str, str]:
    """chassis id -> why the configuration says it is not an alarm.

    Covers bridges seen in this scan and bridges only the database
    remembers: a device behind a port that has since been declared an
    uplink may not be in LLDP right now, and its alarm would otherwise
    outlive the setting that answers it. The same applies to a device
    that turned out to be one of our own polled switches — the alarm
    for it is closed by this path, with the reason in the journal,
    rather than waiting for a neighbour that is never going away.
    """
    uplinks = _uplink_ports()
    known = set(config.known_bridges)
    ours = _polled_identities()
    located: dict[str, tuple[str, str, str]] = {
        row["chassis_id"]: (row["switch_ip"], row["port"], row["mgmt_ip"])
        for row in bridge_rows.values()
    }
    located.update({
        b["chassis_id"]: (b["switch"], b["port"], b.get("mgmt_ip", ""))
        for b in bridges
    })
    suppressed: dict[str, str] = {}
    for chassis_id, (switch_ip, port, mgmt_ip) in located.items():
        if (switch_ip, port) in uplinks:
            suppressed[chassis_id] = (
                f"{switch_ip} {port} is listed in uplink_ports"
            )
        elif chassis_id.lower() in known or (mgmt_ip or "").lower() in known:
            suppressed[chassis_id] = "listed in known_bridges"
        elif chassis_id.lower() in ours:
            suppressed[chassis_id] = ours[chassis_id.lower()]
        elif (mgmt_ip or "").lower() in ours:
            suppressed[chassis_id] = ours[mgmt_ip.lower()]
    return suppressed


def _polled_identities() -> dict[str, str]:
    """Every way a device we already poll can appear in someone's LLDP.

    A switch listed in `switches:` is polled by definition, and so a
    neighbour reporting it is not "a switch nobody polls". It used to
    be: the ES3528M sat in `switches:`, answered every scan, and still
    carried an unmanaged_bridge_detected alarm for 91 hours, because
    only `known_bridges` counted as known. Making the operator copy
    their own switch addresses into a second list to silence that is
    asking them to work around a bug.

    Returns identity -> why it is ours, lowercased: the configured
    switch and router addresses, and every MAC each polled switch is
    known by (bridge base MAC, interface MACs, its management-IP MAC
    from ARP) — a neighbour may name any of them as the chassis id.
    """
    ours: dict[str, str] = {}
    for ip in _infrastructure():
        ours[ip.lower()] = (
            "it is a router MoonLan polls" if ip in set(config.routers)
            else "it is a switch MoonLan polls"
        )
    for ip, sw in switch_data.items():
        why = f"it is {sw.sys_name or ip}, a switch MoonLan polls"
        for mac in sw.own_macs:
            ours[mac.lower()] = why
        if sw.bridge_mac:
            ours[sw.bridge_mac.lower()] = why
    return ours


def _known_bridge_ids() -> set[str]:
    """Chassis ids and addresses that must never raise a bridge alarm."""
    return set(config.known_bridges) | set(_polled_identities())


def _is_known_bridge(bridge: dict) -> bool:
    """Known by configuration, or one of the devices we poll ourselves."""
    known = _known_bridge_ids()
    return (
        bridge["chassis_id"].lower() in known
        or bridge.get("mgmt_ip", "").lower() in known
    )


def _apply_suspects(
    hosts: list[dict], suspects: dict[tuple[str, str], list[dict]]
) -> None:
    """Marks the distorted MACs and remembers them for the ports panel."""
    suspect_by_port.clear()
    suspect_by_port.update(suspects)
    flat = {
        s["mac"]: s for found in suspects.values() for s in found
    }
    for host in hosts:
        found = flat.get(host["mac"])
        if found:
            host["suspect_corrupt"] = True
            host["suspect_of"] = found["sample"]
    if flat:
        log.info(
            "Suspected frame corruption: %d distorted MACs on %d port(s): %s",
            len(flat), len(suspects),
            ", ".join(f"{ip} {port}" for ip, port in sorted(suspects)),
        )


def _switch_macs() -> set[str]:
    """Every MAC belonging to a polled switch — those are nodes of the
    map, never hosts, even when a router's ARP table lists them."""
    macs: set[str] = set()
    for sw in switch_data.values():
        macs |= sw.own_macs
        if sw.bridge_mac:
            macs.add(sw.bridge_mac)
    return macs


async def _release_unconfirmed_ips(db_rows: dict[str, dict]) -> None:
    """Takes the IP off hosts that no longer own it.

    Pings go by IP while staleness is judged by MAC, so a record whose
    MAC left every switch table keeps "answering" with whatever device
    holds that address now — the host looks alive hours after it left.
    Once ARP has not confirmed the pair for ip_confirm_hours, the
    address is released and the record stops being pinged. db_rows is
    updated in place so the rest of this scan sees the change.
    """
    hours = config.ip_confirm_hours
    if hours <= 0:
        return
    now = time.time()
    cutoff = now - hours * 3600
    released = []
    for mac, row in db_rows.items():
        if not row["ip"] or mac in fdb_macs:
            continue
        if row["ip_confirmed"] >= cutoff:
            continue
        if await asyncio.to_thread(
            db.release_ip, mac, now,
            f"not confirmed by ARP for {hours:g} h",
        ):
            released.append(mac)
            row["ip"] = ""
            row["ip_confirmed"] = 0
            row["ping_up"] = 0
    if released:
        log.info(
            "Released %d IP addresses of hosts missing from every MAC "
            "table and unconfirmed by ARP", len(released),
        )


def _nodes_on_port(
    bridges: list[dict], pseudo_switches: list[dict]
) -> dict[tuple[str, str], dict]:
    """(switch, port) -> the node standing on that cable, if any.

    A pseudo-switch wins over a bridge where both are present: that is
    the case of several bridges behind one unmanaged box, and the live
    devices stay on the box, so the quiet ones must too.

    A device MoonLan polls itself is never offered as a parent. Groups
    hang off what is on the cable, and if devices were behind a switch
    we poll they would be on its downlink ports — believing otherwise
    is the whole of the v0.6.9 defect. Such a switch is drawn as its
    own node on the same port instead.
    """
    ours = set(_polled_identities())
    nodes: dict[tuple[str, str], dict] = {}
    for bridge in bridges:
        if bridge.get("trunk") or bridge.get("shares_port"):
            continue
        if (bridge["chassis_id"].lower() in ours
                or bridge.get("mgmt_ip", "").lower() in ours):
            continue
        nodes[(bridge["switch"], bridge["port"])] = {
            "id": bridge["id"], "name": bridge["name"],
        }
    for pseudo in pseudo_switches:
        nodes[(pseudo["switch"], pseudo["port"])] = {
            "id": pseudo["id"], "name": "",
        }
    return nodes


def _remembered_locations(
    db_rows: dict[str, dict]
) -> dict[str, tuple[str, str]]:
    """MAC -> the (switch, port) the database last placed it at.

    Consulted before a device visible only through a trunk is guessed
    onto one. What the database remembers is a port a device was
    actually seen on; a trunk is where its frames happened to pass.
    """
    return {
        mac: (row["switch_ip"], row["port"])
        for mac, row in db_rows.items()
        if row["switch_ip"] and row["port"]
    }


def _known_hosts_per_port(
    db_rows: dict[str, dict], switch_ips: set[str]
) -> dict[tuple[str, str], int]:
    """How many devices the database knows behind each port, counting
    the ones inside the grace window — the same set that stays on the
    map — so the unmanaged-switch threshold sees the whole group and
    not only the MACs this particular poll happened to catch."""
    grace = config.host_grace_hours * 3600
    now = time.time()
    counts: Counter = Counter()
    for row in db_rows.values():
        if not row["confirmed"]:
            continue  # not a device yet: it must not push a port over
                      # the unmanaged-switch threshold
        if row["switch_ip"] in switch_ips and row["port"]:
            if grace > 0 and now - row["last_seen"] < grace:
                counts[(row["switch_ip"], row["port"])] += 1
    return dict(counts)


def _assemble_hosts(
    fresh: list[dict],
    db_rows: dict[str, dict],
    pseudo_switches: list[dict],
    switch_ips: set[str],
    unconfirmed: set[str] | None = None,
    merged: dict[str, str] | None = None,
    nodes_on_port: dict[tuple[str, str], dict] | None = None,
    uplink_only: dict[str, list[tuple[str, str]]] | None = None,
    trunk_names: dict[str, list[str]] | None = None,
) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """Splits the known inventory into map hosts, offline groups and
    off-map devices.

    On the map: this scan's FDB hosts, plus hosts whose MAC left the
    FDB less than host_grace_hours ago, drawn at their last known port
    (FDB entries age out in minutes — without the grace window a quiet
    device blinks in and out on every scan) and marked stale. Stale
    hosts sharing a port are collected under one offline group instead
    of surrounding the switch with a cloud of grey dots.

    Off the map: everything else the DB knows — devices ARP sees but
    no switch port ever showed, and hosts whose grace window ran out.

    Unconfirmed MACs are in neither list: they are stored, and nothing
    more, until enough polls agree that they exist.

    `uplink_only` are MACs every sighting of which was on an uplink.
    They go off the map whatever the database remembers about them,
    because what it remembers is an earlier guess at the same question
    — that is how twenty devices of the rest of the network came to be
    drawn behind a switch with every port but its uplink dark.
    """
    for h in fresh:
        h["stale"] = False
    now = time.time()
    merged = merged or {}
    grace = config.host_grace_hours * 3600
    uplink_only = uplink_only or {}
    known = {h["mac"] for h in fresh} | _switch_macs() | (unconfirmed or set())
    pseudo_by_port = {(p["switch"], p["port"]): p["id"] for p in pseudo_switches}
    on_map = list(fresh)
    unlocated: list[dict] = []
    for mac, row in db_rows.items():
        if mac in known:
            continue
        host = {
            "mac": mac,
            "switch": row["switch_ip"],
            "port": row["port"],
            "vlan": row["vlan"],
            "name": "",
            "stale": True,
        }
        # a device drawn as its bridge node keeps that identity while
        # it is quiet, instead of reappearing as a bare MAC beside it
        if mac in merged:
            host["merged_into"] = merged[mac]
        if mac in uplink_only:
            # seen this very scan, and seen nowhere that means anything
            host["stale"] = False
            host["unlocated"] = True
            host["uplink_only"] = True
            host["seen_on"] = [
                {"switch": ip, "port": port}
                for ip, port in uplink_only[mac]
            ]
            host["switch"] = ""
            host["port"] = ""
            unlocated.append(host)
            continue
        located = row["switch_ip"] in switch_ips
        if located and grace > 0 and now - row["last_seen"] < grace:
            via = pseudo_by_port.get((row["switch_ip"], row["port"]))
            if via:
                host["via"] = via
            on_map.append(host)
        else:
            host["unlocated"] = True
            unlocated.append(host)
    # devices ARP still knows first, then by how recently anything saw them
    unlocated.sort(
        key=lambda h: (
            not db_rows[h["mac"]]["ip"],
            -max(db_rows[h["mac"]]["last_arp"], db_rows[h["mac"]]["last_seen"]),
        )
    )
    # Devices with no place of their own go first: "we do not know
    # where this is" is a stronger statement than "this has been quiet
    # for a while", and the offline grouping below leaves anything that
    # already has a `via` alone. So one trunk is one node, with the
    # quiet ones counted inside it rather than standing beside it.
    silent = {
        mac for mac, row in db_rows.items()
        if row.get("ip") and not row.get("ping_up")
    }
    trunk_groups = group_beyond_trunk(
        on_map, trunk_names or {}, config.trunk_group_threshold,
        nodes_on_port, silent,
    )
    return (
        on_map, _group_offline(on_map, db_rows, nodes_on_port), unlocated,
        trunk_groups,
    )


def _group_offline(
    on_map: list[dict],
    db_rows: dict[str, dict],
    nodes_on_port: dict[tuple[str, str], dict] | None = None,
) -> list[dict]:
    """Collects the stale hosts of one port under a single group node.

    A port whose devices are all offline gets a "temporary location"
    node — the devices are shown where they were last seen, and one
    node says so instead of a dozen grey dots. Ports that already have
    a pseudo-switch keep it: their hosts hang off that node.

    `nodes_on_port` is what build_topology put on each port — a named
    bridge, or an anonymous pseudo-switch. The group hangs off that
    node rather than off the switch. Behind mb1 1/27 there were three
    neighbours of the switch at once: RouterOS-Sport with 22 live
    devices, a "switch without SNMP", and an "Offline · 19" of
    10.3.5.x addresses standing beside them — one segment drawn as
    three places, with the quiet half apparently somewhere else
    entirely.
    """
    threshold = config.offline_group_threshold
    if threshold <= 0:
        return []
    nodes_on_port = nodes_on_port or {}
    by_port: dict[tuple[str, str], list[dict]] = {}
    for host in on_map:
        if host.get("merged_into"):
            continue  # it is drawn as its bridge, not as a dot
        if host["stale"] and not host.get("via"):
            by_port.setdefault((host["switch"], host["port"]), []).append(host)
    groups: list[dict] = []
    for (sw_ip, port), members in sorted(by_port.items()):
        if len(members) < threshold:
            continue  # one or two lone dots read fine on their own
        group_id = f"offline:{sw_ip}:{port}"
        for host in members:
            host["via"] = group_id
        parent = nodes_on_port.get((sw_ip, port))
        groups.append({
            "id": group_id,
            "switch": sw_ip,
            "port": port,
            "count": len(members),
            # the live devices of this port hang off this node too, and
            # the quiet ones belong in the same place
            "via": parent["id"] if parent else "",
            "via_name": parent["name"] if parent else "",
            "last_seen_max": max(
                db_rows[m["mac"]]["last_seen"] for m in members
            ),
        })
    return groups


def _effective_monitored(row: dict) -> bool:
    """monitored_by_default=true restores the old alert-on-everything
    behavior regardless of the per-host flag."""
    return config.monitored_by_default or bool(row.get("monitored", 0))


def _merge_db_fields(hosts: list[dict], db_hosts: dict[str, dict]) -> None:
    """Enriches topology hosts with DB fields (IP, name, ping state)."""
    for h in hosts:
        row = db_hosts.get(h["mac"], {})
        h["ip"] = row.get("ip", "")
        h["name"] = row.get("name", "")
        h["ping_up"] = bool(row.get("ping_up", 0))
        h["last_ping_ok"] = row.get("last_ping_ok", 0)
        h["first_seen"] = row.get("first_seen", 0)
        h["last_seen"] = row.get("last_seen", 0)
        h["last_arp"] = row.get("last_arp", 0)
        h["ip_confirmed"] = row.get("ip_confirmed", 0)
        h["monitored"] = _effective_monitored(row)
        # phones and laptops randomize their MAC per network, which is
        # why one device can leave a trail of one-off entries
        h["random_mac"] = is_random_mac(h["mac"])


async def collect_arp(collector: SnmpCollector) -> dict[str, str]:
    """The merged ARP table of all routers: MAC -> IP."""
    tables = await asyncio.gather(
        *(collector.collect_arp(ip) for ip in config.routers)
    )
    merged: dict[str, str] = {}
    for table in tables:  # on conflict the last entry wins
        merged.update(table)
    return merged


# mac -> unix time of the last reverse DNS attempt
_dns_attempts: dict[str, float] = {}
DNS_RETRY_SECONDS = 3600
DNS_TIMEOUT = 1.0


async def _reverse_dns(ip: str) -> str:
    try:
        name, _, _ = await asyncio.wait_for(
            asyncio.to_thread(socket.gethostbyaddr, ip), timeout=DNS_TIMEOUT
        )
        return name
    except (OSError, asyncio.TimeoutError):
        return ""


async def resolve_names() -> None:
    """Reverse DNS for hosts with an IP but no name, at most once an hour per host."""
    now = time.time()
    candidates = [
        (mac, ip)
        for mac, ip in await asyncio.to_thread(db.hosts_without_name)
        if now - _dns_attempts.get(mac, 0) >= DNS_RETRY_SECONDS
    ]
    if not candidates:
        return
    for mac, _ in candidates:
        _dns_attempts[mac] = now
    names = await asyncio.gather(*(_reverse_dns(ip) for _, ip in candidates))
    resolved = 0
    for (mac, _), name in zip(candidates, names):
        if name:
            await asyncio.to_thread(db.set_name, mac, name)
            resolved += 1
    if resolved:
        log.info("Reverse DNS: got %d names out of %d", resolved, len(candidates))


async def periodic_scan() -> None:
    while True:
        try:
            await run_scan()
        except Exception as exc:
            # The map from the previous scan stays on screen; what
            # changes is that the header now says the last attempt
            # failed, and with what
            state.scan_failed(exc)
            log.exception("Network scan failed")
        interval = config.scan_interval_minutes
        await asyncio.sleep(interval * 60 if interval > 0 else 3600)


async def run_ping() -> None:
    """One ping cycle: every host with an IP and every switch."""
    now = time.time()
    if config.demo:
        # No real pings: the demo scenario drives the ping state
        # (one host stays silent, another recovers after a while)
        results_by_mac = await asyncio.to_thread(demo.ping_results, db)
        for sw in state.as_dict()["switches"]:
            switch_ping[sw["ip"]] = {"ping_up": True, "last_ping_ok": now}
    else:
        targets = await asyncio.to_thread(db.hosts_with_ip)
        # One ping per unique address, the result applied to every host
        # holding it: IPs are unique in the DB since v0.5.3, but a
        # single probe per address is the right shape regardless
        unique_ips = list(dict.fromkeys(ip for _, ip in targets))
        ips = unique_ips + list(config.switches)
        if not ips:
            return
        results = await pinger.ping_many(ips)
        results_by_mac = {mac: results.get(ip, False) for mac, ip in targets}
        await asyncio.to_thread(db.update_ping, results_by_mac, now)
        for ip in config.switches:
            prev = switch_ping.get(ip, {})
            up = results.get(ip, False)
            switch_ping[ip] = {
                "ping_up": up,
                "last_ping_ok": now if up else prev.get("last_ping_ok", 0),
            }
    if results_by_mac:
        meta = await asyncio.to_thread(db.hosts_by_mac)
        for mac, row in meta.items():
            row["monitored"] = _effective_monitored(row)
            # A located host missing from the latest FDB is not
            # confirmed present, so it raises no alarms. Hosts that
            # were never located (ARP-only) keep alerting normally.
            row["stale"] = bool(row["switch_ip"]) and mac not in fdb_macs
        await alarm_engine.on_ping(results_by_mac, meta)


async def periodic_ping() -> None:
    while True:
        try:
            await run_ping()
        except Exception:
            log.exception("Ping monitoring failed")
        await asyncio.sleep(max(config.ping_interval_seconds, 1))


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


def _lag_label(sw: SwitchData, members: list[int]) -> str:
    """Stable alarm label of a LAG, e.g. "lag[Slot0/1+Slot0/2]".

    Built from the member port names: a member going down changes only
    its oper status, never the composition, while the synthetic
    bridge-port number D-Link exposes DOES change on member flaps —
    keying alarms by it left them hanging active forever (v0.5.1 bug).
    """
    names = [port_name(sw, m) for m in sorted(members)]
    return "lag[" + "+".join(names) + "]"


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


def _port_metrics(
    sw: SwitchData, rates: dict[int, counters.PortRates]
) -> list[dict]:
    """Alarm inputs for one counters cycle.

    Physical ports are evaluated individually; LAG members get
    speed_mbps=0 so the utilization rule fires on the aggregate entry
    (sum of members against the total speed) instead, per the spec.
    """
    groups = _lag_groups(sw)
    in_lag = {m for members in groups.values() for m in members}
    metrics: list[dict] = []
    for if_index, r in rates.items():
        port = sw.ports.get(if_index)
        if port is None or not port.is_physical:
            continue
        metrics.append({
            "port": port.name or str(if_index),
            "speed_mbps": 0 if if_index in in_lag else port.speed_mbps,
            "in_mbps": r.in_mbps,
            "out_mbps": r.out_mbps,
            "in_errors_per_min": r.in_errors_per_min,
            "out_errors_per_min": r.out_errors_per_min,
            "in_discards_per_min": r.in_discards_per_min,
            "out_discards_per_min": r.out_discards_per_min,
            "error_ratio": r.error_ratio,
        })
    for aggregate, members in groups.items():
        member_rates = [rates[m] for m in members if m in rates]
        member_ports = [sw.ports[m] for m in members if m in sw.ports]
        # utilization is judged against the ACTIVE capacity: a degraded
        # LAG saturates earlier
        metrics.append({
            "port": _lag_label(sw, members),
            "speed_mbps": sum(p.speed_mbps for p in member_ports if p.oper_up),
            "in_mbps": _sum_known(r.in_mbps for r in member_rates),
            "out_mbps": _sum_known(r.out_mbps for r in member_rates),
            # member errors are already alarmed individually
            "in_errors_per_min": 0.0,
            "out_errors_per_min": 0.0,
            "in_discards_per_min": 0.0,
            "out_discards_per_min": 0.0,
            "error_ratio": None,
            "lag_total": len(member_ports),
            "lag_up": sum(1 for p in member_ports if p.oper_up),
        })
    return metrics


async def run_counters() -> None:
    """One light counters poll of every switch (no topology rebuild)."""
    if not switch_data:
        return  # port lists are unknown until the first scan
    oper_by_ip: dict[str, dict[int, bool]] = {}
    loop_by_ip: dict[str, object] = {}
    if config.demo:
        # the demo flips port states directly in switch_data
        samples_by_ip = demo_counters.sample(list(switch_data.values()))
        counter_columns.clear()
        counter_columns.update(
            {ip: demo_counters.columns(ip) for ip in samples_by_ip}
        )
        loop_by_ip = {
            ip: demo_counters.loop_detection(switch_data[ip])
            for ip in samples_by_ip if ip in switch_data
        }
    else:
        collector = get_collector()
        ips = list(config.switches)
        # One switch failing must not cost the others their counters:
        # a single malformed OID on mb1 emptied every column on all
        # five for as long as the service ran.
        collected = await asyncio.gather(
            *(_counters_locked(collector, ip) for ip in ips),
            return_exceptions=True,
        )
        polls: dict[str, tuple] = {}
        for ip, result in zip(ips, collected):
            if isinstance(result, BaseException):
                log.error(
                    "Counter poll of %s failed — the other switches are "
                    "unaffected", ip, exc_info=result,
                )
                continue
            polls[ip] = result
        samples_by_ip = {ip: p[0] for ip, p in polls.items()}
        oper_by_ip = {ip: p[1] for ip, p in polls.items()}
        counter_columns.clear()
        counter_columns.update({ip: p[2] for ip, p in polls.items()})
        loop_by_ip = {
            ip: p[3] for ip, p in polls.items() if p[3] is not None
        }
    for ip, samples in samples_by_ip.items():
        if not samples:
            continue
        sw = switch_data.get(ip)
        oper = oper_by_ip.get(ip) or {}
        if not oper and sw is not None:
            # demo mode drives the port states directly in switch_data
            oper = {p.if_index: p.oper_up for p in sw.ports.values()}
        if sw is not None:
            # fresh port states at the counters cadence: LAG degradation
            # and the ports panel must not wait for the next full scan
            for if_index, up in oper.items():
                port = sw.ports.get(if_index)
                if port is not None:
                    port.oper_up = up
        speeds = (
            {p.if_index: p.speed_mbps for p in sw.ports.values()}
            if sw is not None else None
        )
        rates = counter_store.update(ip, samples, speeds)
        if sw is not None:
            await alarm_engine.on_counters(ip, _port_metrics(sw, rates))
            await _check_flaps(sw, oper, samples)
    for ip, loop in loop_by_ip.items():
        sw = switch_data.get(ip)
        if sw is None or loop is None:
            continue
        sw.loop_detection = loop
        _log_loop_state(sw, loop)
        await alarm_engine.on_loop_detection(
            ip, sw.sys_name or ip, loop,
            {i: p.name or str(i) for i, p in sw.ports.items()},
        )
    await alarm_engine.janitor(_observed_subjects())


# What each switch's loop detection last said, so the same line is not
# repeated every minute for a network where nothing is happening
_loop_logged: dict[str, str] = {}


def _log_loop_state(sw: SwitchData, loop) -> None:
    """One line per switch, and only when the answer changes."""
    line = loopdetect.describe(loop)
    if _loop_logged.get(sw.ip) == line:
        return
    _loop_logged[sw.ip] = line
    level = logging.WARNING if loop.looped_ports() else logging.INFO
    log.log(level, "%s loop detection: %s", sw.sys_name or sw.ip, line)
    if loop.index_mismatch:
        log.warning(
            "%s: the loop-detection table has %d row(s) and the interface "
            "table %d physical port(s)%s — the rows that do not match a "
            "port are not used",
            sw.ip, loop.rows,
            sum(1 for p in sw.ports.values() if p.is_physical and p.if_index > 0),
            f"; unmatched: {loop.unmapped}" if loop.unmapped else "",
        )


async def _check_flaps(
    sw: SwitchData, oper: dict[int, bool], samples: dict[int, counters.Sample]
) -> None:
    """Link-state transitions of one switch, for the alarm and the panel."""
    # only ports this poll actually returned: a port missing from the
    # walk has no ifLastChange, and a zero there would read as the
    # agent restarting its clock
    flaps = flap_tracker.update(
        sw.ip,
        {i: up for i, up in oper.items() if i in samples},
        {i: s.last_change for i, s in samples.items()},
    )
    entries = []
    for if_index, info in flaps.items():
        port = sw.ports.get(if_index)
        if port is None or not port.is_physical:
            continue
        name = port.name or str(if_index)
        flap_by_port[(sw.ip, name)] = info
        entries.append(
            {"port": name, "flaps": info.count, "last": info.last}
        )
    if entries:
        await alarm_engine.on_flaps(sw.ip, entries)


def _observed_subjects() -> set[tuple[str, str]]:
    """(type, subject) pairs that exist in the current switch data —
    the janitor auto-clears active alarms that fell out of this set."""
    observed: set[tuple[str, str]] = set()
    for ip, sw in switch_data.items():
        labels = [
            p.name or str(p.if_index)
            for p in sw.ports.values() if p.is_physical
        ]
        labels += [_lag_label(sw, members) for members in _lag_groups(sw).values()]
        for label in labels:
            subject = f"{ip}:{label}"
            for alarm_type in (
                "port_errors", "port_discards", "port_util", "port_hosts_down",
                "port_frame_corruption", "port_flapping",
            ):
                observed.add((alarm_type, subject))
            if label.startswith("lag["):
                observed.add(("lag_degraded", subject))
    return observed


async def periodic_counters() -> None:
    while True:
        try:
            await run_counters()
        except Exception:
            log.exception("Counter polling failed")
        await asyncio.sleep(max(config.counters_interval_seconds, 5))


def resource_usage() -> tuple[int, int]:
    """(open file descriptors, RSS in kB) — leak watch, Linux only."""
    fds = len(os.listdir("/proc/self/fd"))
    rss = 0
    with open("/proc/self/status", encoding="ascii", errors="replace") as f:
        for line in f:
            if line.startswith("VmRSS:"):
                rss = int(line.split()[1])
                break
    return fds, rss


RESOURCE_LOG_SECONDS = 600


async def periodic_resource_log() -> None:
    while True:
        await asyncio.sleep(RESOURCE_LOG_SECONDS)
        try:
            fds, rss = resource_usage()
            # INFO on purpose: the leak watch must be visible in
            # journalctl at the default log level
            log.info("open fds: %d, rss: %d kB", fds, rss)
        except OSError:
            pass  # not a Linux /proc — skip silently


def _rates_hide_age() -> float:
    """Past this age a measured rate stops being shown at all.

    It used to be three counters intervals, and anything older was
    dropped from the answer — which drew the same "—" as a column the
    agent never answered. Half an hour is the point at which a rate
    really is a memory rather than a measurement; everything younger is
    shown with its age next to it.
    """
    return max(config.stale_rate_hide_minutes, 0.0) * 60


def _refresh_link_lag(link: dict) -> None:
    """Recomputes LAG member states, active count and speed from the
    latest port data, so a member going down between topology rebuilds
    is reflected on the edge without waiting for the next scan."""
    lag = link.get("lag")
    if not lag or not lag.get("members"):
        return
    lag = dict(lag)  # the caller passes a copy of the link, not of lag
    active_counts: list[int] = []
    speeds: dict[str, int] = {}
    for side in ("a", "b"):
        sw = switch_data.get(link[side])
        names = lag.get(f"{side}_members")
        if sw is None or not names:
            continue
        by_name = {p.name: p for p in sw.ports.values() if p.name}
        states = [bool(by_name.get(n) and by_name[n].oper_up) for n in names]
        lag[f"{side}_states"] = states
        active_counts.append(sum(states))
        speeds[side] = sum(
            by_name[n].speed_mbps
            for n, up in zip(names, states) if up and n in by_name
        )
    if not active_counts:
        return
    lag["active"] = min(active_counts)
    link["lag"] = lag
    if len(speeds) == 2:
        link["speed_mbps"] = min(speeds.values())
    elif speeds:
        link["speed_mbps"] = next(iter(speeds.values()))


def _link_load(link: dict) -> dict | None:
    """Current traffic through a link, summed over LAG members.

    Prefers the A side (the parent in the tree); falls back to the B
    side with in/out flipped. in_mbps/out_mbps are from A's viewpoint:
    out = toward B (downstream).
    """
    lag = link.get("lag") or {}
    for side, flip in (("a", False), ("b", True)):
        sw = switch_data.get(link[side])
        if sw is None:
            continue
        rates = counter_store.current(link[side], max_age=_rates_hide_age())
        if not rates:
            continue
        names = lag.get(f"{side}_members") or [link[f"{side}_port"]]
        by_name = {p.name: p.if_index for p in sw.ports.values() if p.name}
        found = [
            rates[by_name[n]]
            for n in names
            if n in by_name and by_name[n] in rates
        ]
        if not found:
            continue
        in_mbps = _sum_known(r.in_mbps for r in found)
        out_mbps = _sum_known(r.out_mbps for r in found)
        if in_mbps is None and out_mbps is None:
            continue  # this side knows nothing; try the other one
        if flip:  # B's in is A's out and vice versa
            in_mbps, out_mbps = out_mbps, in_mbps
        # The oldest of the measurements this number is made of: an
        # edge label is as fresh as its stalest member, and the tooltip
        # has to be able to say so.
        age = round(max(time.time() - r.ts for r in found))
        return {
            "in_mbps": _round(in_mbps),
            "out_mbps": _round(out_mbps),
            "age_seconds": age,
        }
    return None


def _log_config() -> None:
    """One line on how much of the configuration is actually yours —
    a config.yaml left over from an older version silently overrides
    settings whose defaults have changed since.

    Both paths are absolute on purpose. With MOONLAN_CONFIG in play
    there can be two instances running out of the same directory, and
    the log was the one place that could not say which file and which
    database each of them had opened.
    """
    report = config.report
    if report is None:
        return
    log.info(
        "Config file: %s%s", report.path,
        "" if report.exists else "  (not found — every setting is a default)",
    )
    log.info(
        "Database:    %s%s", Path(db.path).resolve() if db.path != ":memory:"
        else "in memory (demo mode)",
        "" if not config.demo else "  — the real inventory is not touched",
    )
    summary = (
        f"Config: {len(report.overrides)} from {report.path}, "
        f"{len(report.defaults)} defaults"
    )
    if report.unknown:
        log.warning(
            "%s; unknown keys ignored: %s — check them with "
            "python -m moonlan.diag --config",
            summary, ", ".join(report.unknown),
        )
    else:
        log.info("%s", summary)
    for problem in report.problems:
        log.warning("config.yaml switches: %s", problem)
    starved = config.starved_counters()
    if starved:
        log.warning(
            "Poll budget at or above twice counters_interval_seconds "
            "(%d s) on %d switch(es): %s. While a scan holds one of "
            "these, its counters cycle is skipped, so its rates will "
            "visibly age between scans — the panel shows them with their "
            "age rather than hiding them. Lower host_budget_seconds for "
            "those devices, or raise counters_interval_seconds.",
            config.counters_interval_seconds, len(starved),
            ", ".join(f"{ip} ({budget} s)" for ip, budget in starved),
        )
    custom = config.custom_switches()
    if custom:
        log.info(
            "SNMP settings of their own on %d of %d switch(es): %s. "
            "Everything else is inherited from the snmp: section; "
            "python -m moonlan.diag --config prints which is which.",
            len(custom), len(config.switches),
            ", ".join(
                f"{ip} ("
                + ", ".join(sorted(config.switch_snmp[ip].explicit))
                + ")"
                for ip in custom
            ),
        )


async def purge_invalid_macs() -> None:
    """Startup cleanup: drop records whose MAC cannot be real.

    Before the FDB rows were validated, malformed table entries were
    stored as devices; those records are still in the database and on
    the map.
    """
    rows = await asyncio.to_thread(db.hosts_by_mac)
    bad = [mac for mac in rows if not is_valid_mac(mac)]
    if not bad:
        return
    removed = await asyncio.to_thread(db.delete_hosts, bad)
    log.info(
        "Removed %d host records with impossible MAC addresses "
        "(multicast, reserved or malformed)", removed,
    )
    await asyncio.to_thread(
        db.add_event, time.time(), "hosts_purged", "",
        f"{removed} records with impossible MAC addresses",
    )


async def purge_old_hosts() -> None:
    """Startup cleanup: drop hosts nothing has seen for retention days.

    LLDP rows follow the same retention: a neighbour that has not been
    seen since a cable was moved is history nobody asked to keep.
    """
    days = config.host_retention_days
    if days <= 0:
        return
    now = time.time()
    dropped = await asyncio.to_thread(db.purge_lldp, now - days * 86400)
    if dropped:
        log.info("Removed %d LLDP records older than %g days", dropped, days)
    removed = await asyncio.to_thread(db.purge_old_hosts, now - days * 86400)
    if removed:
        log.info(
            "Removed %d hosts not seen for %g days", removed, days
        )
        await asyncio.to_thread(
            db.add_event, now, "hosts_purged", "",
            f"{removed} hosts not seen for {days:g} days",
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    _log_config()
    if config.demo:
        log.info("MoonLan started in DEMO mode (virtual network)")
    elif not config.switches:
        log.warning(
            "No switches are configured in config.yaml. Add addresses to "
            "the switches section or start with MOONLAN_DEMO=1."
        )
    await purge_invalid_macs()
    await purge_old_hosts()
    await alarm_engine.load()
    await alarm_engine.clear_missing_hosts(
        set(await asyncio.to_thread(db.hosts_by_mac))
    )
    tasks = [
        asyncio.create_task(periodic_scan()),
        asyncio.create_task(periodic_ping()),
        asyncio.create_task(periodic_counters()),
        asyncio.create_task(periodic_resource_log()),
    ]
    yield
    for task in tasks:
        task.cancel()


app = FastAPI(title="MoonLan", version=__version__, lifespan=lifespan)


@app.get("/api/topology")
async def api_topology() -> JSONResponse:
    topo = state.as_dict()
    # Fresh DB data: ping is updated more often than SNMP polls happen
    db_hosts = await asyncio.to_thread(db.hosts_by_mac)
    hosts = [dict(h) for h in topo["hosts"]]
    _merge_db_fields(hosts, db_hosts)
    topo["hosts"] = hosts
    unlocated = [dict(h) for h in topo.get("unlocated", [])]
    _merge_db_fields(unlocated, db_hosts)
    topo["unlocated"] = unlocated
    # a bridge node answers "is it up?" like any other device
    bridges = []
    for bridge in topo.get("bridges", []):
        bridge = dict(bridge)
        row = db_hosts.get(bridge["chassis_id"], {})
        if row:
            bridge["ip"] = row.get("ip", "") or bridge.get("mgmt_ip", "")
            bridge["ping_up"] = bool(row.get("ping_up", 0))
            bridge["last_ping_ok"] = row.get("last_ping_ok", 0)
        bridges.append(bridge)
    topo["bridges"] = bridges
    topo["switches"] = [
        {
            **sw,
            "ping_up": switch_ping.get(sw["ip"], {}).get("ping_up", False),
            "last_ping_ok": switch_ping.get(sw["ip"], {}).get("last_ping_ok", 0),
            # loop detection is polled at the counters cadence, not by
            # the scan the rest of this map comes from, so it is
            # attached per request rather than baked into the topology
            "loop": _loop_report(switch_data.get(sw["ip"])),
        }
        for sw in topo["switches"]
    ]
    # Live trunk load on the edges; recomputed on every request so the
    # periodic UI refresh picks it up without a topology rebuild
    links = []
    for link in topo["links"]:
        link = dict(link)
        _refresh_link_lag(link)
        load = _link_load(link)
        if load:
            link["load"] = load
        links.append(link)
    topo["links"] = links
    return JSONResponse(topo)


@app.get("/api/switch/{ip}/ports")
async def api_switch_ports(ip: str) -> dict:
    """Port table of one switch with current rates."""
    sw = switch_data.get(ip)
    if sw is None:
        return {"switch": ip, "name": ip, "ports": []}
    # Everything measured, however old. What is too old to show is
    # decided below, once, and said out loud rather than by omission.
    rates = counter_store.current(ip)
    hide_age = _rates_hide_age()
    now = time.time()
    db_hosts = await asyncio.to_thread(db.hosts_by_mac)
    host_counts: Counter = Counter()
    monitored_counts: Counter = Counter()
    for h in state.as_dict()["hosts"]:
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
        port: info for (sw_ip, port), info in flap_by_port.items()
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
            "rate_ts": None if age is None else rates[p.if_index].ts,
            "rate_age_seconds": None if age is None else round(age),
            "hosts": host_counts.get(name, 0),
            "monitored_hosts": monitored_counts.get(name, 0),
            # MACs that look like damaged copies of a real one here
            "suspect_macs": suspect_by_port.get((ip, name), []),
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
            "uplink_hint": uplink_suspects.get((ip, name)),
        })
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
            "stale_after_seconds": max(config.counters_interval_seconds, 5) * 3,
            "hide_after_seconds": hide_age,
        },
        # so the panel can colour the values it shows
        "thresholds": {
            "errors_per_minute": config.thresholds.errors_per_minute,
            "discards_per_minute": config.thresholds.discards_per_minute,
        },
    }


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
    opposite things — mb0 reported honest zeros for errors while its
    outbound octets were simply never returned.
    """
    columns = counter_columns.get(ip) or {}
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
    }


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
            (b for b in state.as_dict()["bridges"]
             if b["chassis_id"] == subject),
            {},
        )
        meta["display"] = bridge.get("name") or subject
        meta["switch_ip"] = bridge.get("switch", "")
        meta["port"] = bridge.get("port", "")
    elif row["type"] == "stp_fragmented":
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


@app.get("/api/stp")
async def api_stp() -> dict:
    """Spanning tree per switch plus the verdict for the network.

    Deliberately flat: whether the switch's tree is operating at all
    comes first, and root / cost / root port are empty strings for a
    switch where they would be meaningless.
    """
    return state.as_dict()["stp"] or {
        "verdict": {"verdict": "not_operating", "roots": {}, "operating": [],
                    "total": 0},
        "switches": [],
    }


@app.get("/api/alarms")
async def api_alarms(
    active: int = Query(default=1, ge=0, le=1),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict:
    rows = await asyncio.to_thread(db.alarms, bool(active), limit)
    db_hosts = await asyncio.to_thread(db.hosts_by_mac)
    sw_names = {sw["ip"]: sw["name"] for sw in state.as_dict()["switches"]}
    flapping = alarm_engine.flapping_keys()
    stats = alarm_engine.raise_stats()
    for row in rows:
        key = (row["type"], row["subject"])
        row.update(_alarm_meta(row, db_hosts, sw_names))
        row["flapping"] = key in flapping
        row["raise_count"], row["last_raise"] = stats.get(key, (1, row["ts_raised"]))
    return {
        "alarms": rows,
        "flap_window_hours": config.notifications.flap_window_seconds / 3600,
    }


@app.post("/api/alarms/{alarm_id}/clear")
async def api_clear_alarm(alarm_id: int):
    row = await alarm_engine.manual_clear(alarm_id)
    if row is None:
        return JSONResponse({"error": "no such active alarm"}, status_code=404)
    return {"id": alarm_id, "cleared": True}


@app.get("/api/journal")
async def api_journal(limit: int = Query(default=100, ge=1, le=1000)) -> dict:
    return {"events": await asyncio.to_thread(db.journal, limit)}


class HostPatch(BaseModel):
    monitored: bool


@app.patch("/api/host/{mac}")
async def api_patch_host(mac: str, body: HostPatch):
    mac = mac.lower()
    ok = await asyncio.to_thread(db.set_monitored, mac, body.monitored)
    if not ok:
        return JSONResponse({"error": "unknown host"}, status_code=404)
    return {"mac": mac, "monitored": body.monitored}


class NodePosition(BaseModel):
    x: float
    y: float
    pinned: bool | None = None


class LayoutBody(BaseModel):
    nodes: dict[str, NodePosition]
    # Store only nodes that have no position yet, and say what the
    # others already have. What the page's automatic save uses: its
    # idea of "new" is as old as the page.
    only_new: bool = False


def _layout_node_ids() -> set[str]:
    """Every node id the current map draws.

    The same ids the front end uses, built here so the housekeeping and
    the diagnostics do not have to ask the browser what exists.
    """
    topo = state.as_dict()
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


@app.get("/api/layout")
async def api_layout() -> dict:
    """Saved node positions, and which of the current nodes lack one."""
    saved = await asyncio.to_thread(db.layout)
    present = _layout_node_ids()
    return {
        "nodes": saved,
        "saved_at": await asyncio.to_thread(db.layout_saved_at),
        # Nodes on the map that the saved layout does not cover. A
        # switch added yesterday has no place in a picture drawn the
        # day before, and that gap has to be visible rather than found
        # at printing time.
        "missing": sorted(present - set(saved)),
        # …and the other direction: a saved position whose node is not
        # on the map. Kept on purpose — a device switched off for the
        # night comes back to its place — and only forgotten by age.
        "orphans": sorted(set(saved) - present),
    }


@app.put("/api/layout")
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
            db.add_new_positions, positions
        )
    else:
        await asyncio.to_thread(db.save_layout, positions)
        stored, existing = list(positions), {}
    if stored:
        await asyncio.to_thread(
            db.add_event, time.time(), "layout_saved", "",
            f"{len(stored)} node(s)",
        )
        log.info("Map layout saved: %d node(s)", len(stored))
    return {
        "saved": len(stored),
        "stored": stored,
        "existing": existing,
        "saved_at": await asyncio.to_thread(db.layout_saved_at),
    }


@app.patch("/api/layout/{node_id:path}")
async def api_patch_node_position(node_id: str, body: NodePosition) -> dict:
    """One node, moved by hand — pinned unless told otherwise."""
    pinned = True if body.pinned is None else body.pinned
    await asyncio.to_thread(
        db.set_node_position, node_id, body.x, body.y, pinned
    )
    return {"node_id": node_id, "x": body.x, "y": body.y, "pinned": pinned}


@app.delete("/api/layout/{node_id:path}")
async def api_delete_node_position(node_id: str):
    """Forgets one node: it goes back under the physics engine."""
    removed = await asyncio.to_thread(db.forget_node_position, node_id)
    if not removed:
        return JSONResponse(
            {"error": "no saved position for this node"}, status_code=404
        )
    return {"node_id": node_id, "removed": True}


@app.delete("/api/layout")
async def api_clear_layout() -> dict:
    """Forgets the whole layout. The map is laid out from scratch."""
    removed = await asyncio.to_thread(db.clear_layout)
    await asyncio.to_thread(
        db.add_event, time.time(), "layout_cleared", "",
        f"{removed} node(s)",
    )
    log.info("Map layout cleared: %d node(s) forgotten", removed)
    return {"removed": removed}


async def _scan_once() -> None:
    """A manual scan, recording a failure the same way the loop does."""
    try:
        await run_scan()
    except Exception as exc:
        state.scan_failed(exc)
        log.exception("Network scan failed")


@app.post("/api/scan")
async def api_scan() -> dict:
    asyncio.create_task(_scan_once())
    return {"status": "started"}


@app.get("/api/search")
async def api_search(q: str = Query(default="")) -> dict:
    return {"query": q, "results": state.search(q)}


@app.get("/api/skipped-oids")
async def api_skipped_oids() -> dict:
    """OIDs MoonLan has stopped asking for, and for how much longer.

    The pause lives in the running process, so `diag --skipped` asks
    the service rather than guessing: a separate CLI run has its own
    collector and has never seen any of this.
    """
    collector = _collector
    paused = collector.paused_oids() if collector is not None else []
    return {
        "strikes": config.snmp.dead_oid_strikes,
        "cooldown_scans": config.snmp.dead_oid_cooldown_scans,
        "polled": collector is not None,
        "paused": paused,
    }


@app.get("/api/polling")
async def api_polling() -> dict:
    """When each switch was last polled in full, and how long it took.

    A poll budget set by eye is a budget set wrong. These are the
    numbers to set it from, and they live in this process, so
    `diag --config` asks for them rather than guessing.
    """
    return {
        "counters_interval_seconds": config.counters_interval_seconds,
        "switches": [
            {
                "ip": ip,
                "name": (sw.sys_name or ip) if sw else "",
                "budget_seconds": config.host_budget(ip),
                "polled_at": sw.polled_at if sw else 0.0,
                "poll_seconds": round(sw.poll_seconds, 1) if sw else 0.0,
                "over_budget_scans": sw.over_budget_scans if sw else 0,
                "reachable": bool(sw and sw.reachable),
            }
            for ip, sw in (
                (ip, switch_data.get(ip)) for ip in config.switches
            )
        ],
    }


@app.get("/api/status")
async def api_status() -> dict:
    try:
        open_fds, rss_kb = resource_usage()
    except OSError:
        open_fds, rss_kb = 0, 0
    return {
        "version": __version__,
        "demo": config.demo,
        "switch_count": len(state.switches),
        "host_count": len(state.hosts),
        "unlocated_count": len(state.unlocated),
        "last_scan": state.last_scan,
        "last_scan_ok": state.last_scan_ok,
        "last_error": state.last_error,
        "last_error_ts": state.last_error_ts,
        **state.scan_progress(),
        "layout_saved_at": await asyncio.to_thread(db.layout_saved_at),
        "uptime_hint": time.time(),
        "open_fds": open_fds,
        "rss_kb": rss_kb,
    }


class RevalidatedStaticFiles(StaticFiles):
    """The web UI, which the browser must re-check on every load.

    Served without a Cache-Control header, a file last modified weeks
    ago is "fresh" by the browser's own reckoning for days: after an
    upgrade the page went on running the previous app.js, and a fix to
    the map reached nobody until their cache happened to expire.
    `no-cache` is not "do not cache" — the browser keeps its copy and
    asks each time, and an unchanged file costs a 304.
    """

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


# The static web UI comes last so it does not shadow /api/*
app.mount(
    "/", RevalidatedStaticFiles(directory=str(WEB_DIR), html=True),
    name="web",
)
