"""The poller: everything that asks the network (v0.8.14).

The SNMP collector and the lock per switch, the scan within its budget
and the readings restored after a restart, the routers' ARP and
identity, ping, the counters and what they say about loops and flaps —
one function per cycle — and _build_map, which turns a scan into the
map with the helpers of mapping/. Moved out of server.py word for word,
the fourth stage of taking it apart (docs/ARCHITECTURE.md). The state
is read as rt.X when they run; what they keep between runs — the
collector, the locks, what was last said — are fields of rt that
server.py fills with the rest. Nothing here imports moonlan.server.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from . import __version__, corruption, counters, demo, loopdetect, pinger
from .about import STARTED_AT
from .derived import (
    _copies_window,
    _effective_monitored,
    _lag_groups,
    _subject_inbound_errors,
    _sum_known,
    _uplink_ports,
    configured_switches,
)
from .housekeeping import hide_confirmed_copies
from .mapping.bridges import (
    _infrastructure,
    _is_known_bridge,
    _known_bridge_ids,
    _lldp_rows,
    _merge_bridge_identity,
    _nodes_on_port,
    _suppressed_bridges,
)
from .mapping.hosts import (
    _apply_suspects,
    _assemble_hosts,
    _known_hosts_per_port,
    _marked_copies,
    _merge_db_fields,
    _new_device_text,
    _release_unconfirmed_ips,
    _remembered_locations,
    _switch_macs,
    resolve_names,
)
from .mapping.router_nodes import (
    _mark_router_nodes,
    _merge_router_macs,
    _release_router_macs,
    _remembered_router_macs,
    _router_addresses,
    _router_nodes_take_over,
)
from .mapping.switches import (
    _dropped_link_details,
    _judge_stp,
    _retire_port_names,
    _stp_report,
    _stub_switches,
)
from .mapping.upkeep import _purge_layout_once, _reconcile_notes
from .routers import (
    RouterIdentity,
    own_macs as router_own_macs,
    place as place_routers,
)
from .runtime import rt
from .snmp_collector import (
    READING_FORMAT,
    STATE_FRESH,
    STATE_NEVER,
    STATE_OVER_BUDGET,
    STATE_RESTORED,
    STATE_UNREACHABLE,
    OverBudget,
    SnmpCollector,
    SwitchData,
)
from .topology import build_topology, port_name, suspect_uplink_ports

log = logging.getLogger("moonlan")


def loop_profiles() -> list:
    if rt._loop_profiles is None:
        extra, problems = loopdetect.parse_profiles(
            rt.config.loop_detection.profiles
        )
        for problem in problems:
            log.warning("config.yaml loop_detection.profiles: %s", problem)
        rt._loop_profiles = loopdetect.merge_profiles(
            loopdetect.BUILTIN_PROFILES, extra
        )
        log.info(
            "Loop detection profiles: %s",
            ", ".join(p.name for p in rt._loop_profiles),
        )
    return rt._loop_profiles


def host_lock(ip: str) -> asyncio.Lock:
    lock = rt._host_locks.get(ip)
    if lock is None:
        lock = rt._host_locks[ip] = asyncio.Lock()
    return lock


def get_collector() -> SnmpCollector:
    if rt._collector is None:
        rt._collector = SnmpCollector(
            community=rt.config.snmp.community,
            timeout=rt.config.snmp.timeout,
            retries=rt.config.snmp.retries,
            retries_on_break=rt.config.snmp.retries_on_break,
            per_host=rt.config.switch_snmp,
            dead_oid_strikes=rt.config.snmp.dead_oid_strikes,
            dead_oid_cooldown_scans=rt.config.snmp.dead_oid_cooldown_scans,
        )
    return rt._collector


async def _collect_locked(collector: SnmpCollector, ip: str) -> SwitchData:
    """A full poll, holding this switch's lock for its duration."""
    async with host_lock(ip):
        return await collector.collect(ip)


async def _collect_within_budget(
    collector: SnmpCollector, ip: str, budget: float
) -> SwitchData | OverBudget:
    """A full poll, or OverBudget when it ran past its budget.

    `snmp.timeout` bounds one request. A poll is a dozen walks of a
    dozen requests each, so an agent that answers everything slowly
    stays inside every single timeout and still takes eight minutes —
    and `gather` waits for the last one. Nothing here makes that agent
    faster; it only stops it from deciding when the rest of the
    network gets its map.
    """
    started = time.monotonic()
    started_at = time.time()
    try:
        data = await asyncio.wait_for(
            _collect_locked(collector, ip), budget
        )
        # How long a complete poll really takes, so budgets can be set
        # from measurements instead of guesses
        data.poll_seconds = time.monotonic() - started
        if data.reachable and hasattr(collector, "slowest_walks"):
            rt.poll_report[ip] = {
                "cut_at": None, "slowest_walks": collector.slowest_walks(ip),
            }
        return data
    except asyncio.TimeoutError:
        # Where the time went (v0.8.8): read before anything else can
        # take this host, while the cancelled walk is still "running"
        cut = collector.poll_cut(ip) if hasattr(collector, "poll_cut") else None
        walks = (
            collector.slowest_walks(ip)
            if hasattr(collector, "slowest_walks") else []
        )
        log.warning(
            "%s did not finish its poll within the %.0f s budget (gave up "
            "after %.0f s) — it is left out of this scan, which is not the "
            "same as not answering: see snmp.host_budget_seconds%s",
            ip, budget, time.monotonic() - started, _cut_words(cut),
        )
        if cut is not None:
            rt.poll_report[ip] = {"cut_at": cut, "slowest_walks": walks}
        answered_at = getattr(collector, "answered_at", {}).get(ip, 0.0)
        return OverBudget(
            True if answered_at >= started_at else None, cut=cut, walks=walks,
        )
    finally:
        rt.state.host_polled()


def _cut_words(cut: dict | None) -> str:
    """"… cut in the walk of X" — the tail of the over-budget WARNING."""
    if not cut:
        return ""
    name = f" ({cut['name']})" if cut.get("name") else ""
    if cut.get("in_walk"):
        return (
            f"; cut in the walk of {cut['oid']}{name} after {cut['rows']} "
            f"row(s) and {cut['seconds']:.1f} s"
        )
    if cut.get("oid"):
        return (
            f"; cut between walks; the last finished was {cut['oid']}{name} "
            f"({cut['seconds']:.1f} s)"
        )
    return "; cut before its first walk"


def _counters_wait_budget() -> float:
    """How long a counters cycle waits for a switch the scan is holding.

    Long enough to outlast a normal scan of one host, short enough that
    cycles never pile up: half an interval, capped at twenty seconds.
    """
    return min(max(rt.config.counters_interval_seconds, 2) / 2, 20)


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
        sw = rt.switch_data.get(ip)
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
        if rt.config.loop_detection.enabled and sw is not None:
            loop = await loopdetect.collect_loop_detection(
                collector, ip, sw.ports, loop_profiles(),
                sys_object_id=sw.sys_object_id,
            )
        return samples, oper, columns, loop
    finally:
        lock.release()


def _settle(ip: str, result, now: float) -> SwitchData:
    """This scan's outcome for one switch, as what the map is drawn from.

    A full poll is fresh. Anything else keeps the last full reading
    there is — this process's, or the one restored from the database —
    marked with why: over budget, or not answering. With no reading
    anywhere it is `never`: a node with nothing on it, still a node.
    The reading object is the same one every time, so a switch that
    goes from late to silent keeps its data and its loop detection.
    """
    previous = rt.switch_data.get(ip)
    # a full reading of some age: fresh, over budget, silent or restored
    reading = previous if previous is not None and previous.reachable else None
    if isinstance(result, SwitchData) and result.reachable:
        rt.readings_from_database.discard(ip)
        result.state = STATE_FRESH
        result.answered = True
        result.over_budget = False
        result.over_budget_scans = 0
        result.down_since = 0.0
        return result
    if isinstance(result, OverBudget) or result is None:
        # Not an answer, and not a silence either: the last reading that
        # did arrive is kept and dated, so the branch behind this switch
        # stays on the map instead of vanishing every cycle. A switch
        # that has never been read in full still counts its streak: it
        # is further from fine than one whose data is merely old.
        answered = result.answered if isinstance(result, OverBudget) else None
        streak = previous.over_budget_scans + 1 if previous else 1
        target = reading or SwitchData(ip=ip, state=STATE_NEVER)
        if reading is None:
            target.state = STATE_NEVER
        else:
            target.state = STATE_OVER_BUDGET
        target.over_budget = True
        target.over_budget_scans = streak
        target.answered = answered
        target.down_since = 0.0
        return target
    # It did not answer — or the poll raised, which is not an answer
    # either. The reading stays on the map, marked; switch_down goes by
    # `answered`, not by what is drawn.
    since = previous.down_since if previous and previous.down_since else now
    target = reading or SwitchData(ip=ip)
    target.state = STATE_UNREACHABLE if reading is not None else STATE_NEVER
    target.answered = False
    target.over_budget = False
    target.over_budget_scans = 0
    target.down_since = since
    return target


async def run_scan() -> None:
    """One cycle of polling all switches and rebuilding the topology."""
    if rt.state.scanning:
        return
    ips = configured_switches()
    rt.state.scan_started(len(ips))
    over_budget: list[str] = []
    try:
        arp: dict[str, str] = {}
        now = time.time()
        if rt.config.demo:
            results = demo.demo_poll()
            for ip, result in results:
                rt.state.host_polled()
                times = demo.poll_times(ip, result)
                if times is not None:
                    rt.poll_report[ip] = times
        else:
            collector = get_collector()
            collector.begin_scan_cycle()
            polled = await asyncio.gather(
                *(
                    _collect_within_budget(
                        collector, ip, rt.config.host_budget(ip)
                    )
                    for ip in ips
                ),
                return_exceptions=True,
            )
            results = []
            for ip, result in zip(ips, polled):
                if isinstance(result, BaseException):
                    # unreachable is already handled inside collect();
                    # this is a poll that raised, and the rest of the
                    # network should still get a map
                    log.error(
                        "Poll of %s raised — it is left out of this scan",
                        ip, exc_info=result,
                    )
                    result = SwitchData(ip=ip)
                results.append((ip, result))
        collected = [_settle(ip, result, now) for ip, result in results]
        identities: list[RouterIdentity] = []
        if rt.config.demo:
            ip_to_mac = {ip: mac for mac, ip in demo.SWITCH_ARP.items()}
            identities = demo.router_identities()
        else:
            if rt.config.routers:
                arp, identities = await asyncio.gather(
                    collect_arp(collector), collect_identities(collector)
                )
            ip_to_mac = {ip: mac for mac, ip in arp.items()}
        # The MAC of the switch's management IP is also its MAC:
        # neighbors see the switch under it in their FDB tables
        for sw in collected:
            mac = ip_to_mac.get(sw.ip)
            if mac:
                sw.own_macs.add(mac)
        over_budget = [sw.ip for sw in collected if sw.over_budget]
        await _build_map(collected, arp, observe=True, identities=identities)
    finally:
        rt.state.scan_ended(over_budget)


async def restore_readings() -> None:
    """At the start: the saved readings, and the map drawn from them.

    Each switch of `switches:` gets its last full reading back from the
    database as `restored`, unless the reading is in another format or
    older than host_retention_days. Readings of switches no longer in
    `switches:` are deleted. The map is drawn from what came back at
    once — the first scan of a slow network takes minutes, and a map
    that is empty until then says the network is gone — and none of it
    is recorded as a sighting: the first full poll of each switch
    replaces its reading.
    """
    ips = configured_switches()
    rows = await asyncio.to_thread(rt.db.switch_readings)
    gone = sorted(ip for ip in rows if ip not in set(ips))
    if gone:
        await asyncio.to_thread(rt.db.delete_switch_readings, gone)
        log.info(
            "Saved readings of %d switch(es) no longer in switches: "
            "deleted: %s", len(gone), ", ".join(gone),
        )
    days = rt.config.host_retention_days
    cutoff = time.time() - days * 86400 if days > 0 else 0.0
    restored = []
    for ip in ips:
        row = rows.get(ip)
        if row is None:
            continue
        if row["format"] != READING_FORMAT:
            log.info(
                "saved reading of %s is in an older format — ignored "
                "(format %s, this version reads %d); the first full poll "
                "writes a new one", ip, row["format"], READING_FORMAT,
            )
            continue
        if cutoff and (row["polled_at"] or 0) < cutoff:
            log.info(
                "saved reading of %s is from %s, older than "
                "host_retention_days (%g) — ignored", ip,
                time.strftime(
                    "%Y-%m-%d %H:%M", time.localtime(row["polled_at"] or 0)
                ), days,
            )
            continue
        try:
            sw = SwitchData.from_dict(json.loads(row["data"]))
        except (ValueError, KeyError, TypeError) as exc:
            log.warning(
                "saved reading of %s could not be read (%s) — ignored",
                ip, exc,
            )
            continue
        sw.state = STATE_RESTORED
        sw.answered = None
        rt.switch_data[ip] = sw
        rt.readings_from_database.add(ip)
        restored.append(sw)
    if not restored:
        return
    log.info(
        "Restored the saved readings of %d of %d switch(es) — on the map "
        "as \"data from before the restart\" until each one's first full "
        "poll", len(restored), len(ips),
    )
    collected = [
        rt.switch_data.get(ip) or SwitchData(ip=ip, state=STATE_NEVER)
        for ip in ips
    ]
    await _build_map(collected, {}, observe=False)


async def _build_map(
    collected: list[SwitchData], arp: dict[str, str], observe: bool,
    identities: list[RouterIdentity] | None = None,
) -> None:
    """The map from what `collected` holds, and — when `observe` — the
    inventory, the journal and the alarms from what this scan saw.

    `observe=False` is the start, drawing the saved readings: the map
    is built the same way, so it is the same map, and nothing is
    written down — no sighting, no event, no alarm raised or cleared.

    `identities` are what the routers of `routers:` said about
    themselves in this scan (v0.8.11, see routers.py); none at the
    start, where there is no ARP either.
    """
    over_budget = [sw.ip for sw in collected if sw.over_budget]
    for sw in collected:
        previous = rt.switch_data.get(sw.ip)
        if previous is not None and sw.loop_detection is None:
            # Loop detection is refreshed by the counters cycle,
            # not by the scan. A fresh SwitchData carries none, and
            # dropping the last one made every switch report "the
            # model does not say" for up to a minute after every
            # scan — a claim about hardware, made because of our
            # own bookkeeping.
            sw.loop_detection = previous.loop_detection
        rt.switch_data[sw.ip] = sw
    # The last full reading of each switch read in full, for the next
    # start (v0.8.7). Taken here, before the spanning-tree test and the
    # map write their conclusions into the objects: a reading is what
    # the switch said, and the restored one is judged again.
    readings = [
        (sw.ip, sw.polled_at, json.dumps(sw.to_dict(), separators=(",", ":")))
        for sw in collected if observe and sw.map_state == STATE_FRESH
    ]
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
    # Port names from before v0.8.6 in the database, renamed or
    # reset by what this poll knows — before anything below reads
    # the database by port name
    if observe:
        await _retire_port_names(collected)
    # A router's own MACs vouch for themselves the way an IP from ARP
    # does (v0.8.11): never a damaged copy, never held back. What the
    # copy rules did to them before is undone here, before anything
    # below reads the database.
    identities = [identity for identity in identities or () if identity]
    own = router_own_macs(identities)
    if observe and own:
        await _release_router_macs(own)
    # A MAC has to be seen more than once before it counts as a
    # device (unless ARP vouches for it); the verdict is needed
    # before the topology so that unconfirmed addresses stay out of
    # the per-port device counts as well as off the map
    db_rows = await asyncio.to_thread(rt.db.hosts_by_mac)
    # An empty database is the initial inventory — there is no map
    # yet for a phantom to pollute, and waiting would only show the
    # operator an empty screen on the first poll
    confirm_scans = rt.config.new_host_confirm_scans if db_rows else 1
    # Only switches actually read this cycle. A switch that ran out
    # of budget contributes the table it produced last time, and
    # counting a MAC again out of a copy of one reading is how an
    # address gets "seen in three polls" without anyone looking for
    # it twice.
    unconfirmed = await asyncio.to_thread(
        rt.db.projected_unconfirmed,
        {
            mac for sw in collected
            if sw.reachable and not sw.from_reading
            for mac in sw.fdb
        },
        confirm_scans,
    ) - set(own)
    # Damaged copies of a MAC are looked for among the addresses the
    # database has not accepted, and measured against the ones it
    # had accepted BEFORE this poll (v0.8.5). The projection above
    # will not do for that: a copy seen for the second time is not
    # in it — it is about to be confirmed — and it used to be taken
    # for a sample and confirmed. An empty database is the initial
    # inventory, as above: nothing in it to be a copy of.
    with_ip = set(arp) | set(own)
    grace = rt.config.host_grace_hours * 3600
    copy_candidates = corruption.candidates(
        {mac for sw in collected if sw.reachable for mac in sw.fdb},
        db_rows, with_ip,
    ) if db_rows else set()
    # …and the ones that will be found to be copies keep out of the
    # per-port device counts already, or a few of them seen twice
    # would make the port behind one real device a switch without
    # SNMP. Neither do the records marked as copies (v0.8.5).
    voting_out = unconfirmed
    if rt.config.filter_suspect_macs:
        voting_out = unconfirmed | _marked_copies(db_rows)
        voting_out |= corruption.likely_copies(
            {sw.ip: set(sw.fdb) for sw in collected if sw.reachable},
            copy_candidates,
            corruption.samples_by_switch(
                [], db_rows, with_ip, grace, time.time()
            ),
            rt.config.thresholds.corruption_hamming_bits,
        )
    # What the database already knows about each port feeds the
    # unmanaged-switch threshold, so groups survive FDB aging
    (
        switches, links, hosts, pseudo_switches, vlan_names, bridges,
        topo_info,
    ) = build_topology(
        collected,
        rt.config.unmanaged_threshold,
        # a reading drawn at the start does not touch the smoothing
        fdb_stability=None if rt.config.demo or not observe else rt.fdb_stability,
        known_hosts_per_port=_known_hosts_per_port(
            db_rows, {sw.ip for sw in collected if sw.reachable}
        ),
        sticky_pseudo_ports=rt.prev_pseudo_ports,
        unconfirmed_macs=voting_out,
        place_trunk_only=rt.config.place_trunk_only_hosts,
        uplink_ports=_uplink_ports(),
        remembered_locations=_remembered_locations(db_rows),
    )
    # Links the inference removed, and rings it could not resolve.
    # The syslog line is for the moment it happens; the journal is
    # so the history of these decisions can be read from the
    # interface, where the map they changed is.
    for dropped in topo_info.get("dropped_links", []) if observe else ():
        await asyncio.to_thread(
            rt.db.add_event, time.time(), "link_dropped", "",
            _dropped_link_details(dropped),
        )
    # The switches with no reading anywhere (v0.8.7): an empty node
    # each, standing on the port its management MAC is seen on — and
    # that MAC is the switch, not a device beside it
    stubs = _stub_switches(collected, hosts)
    stub_macs = {
        mac for sw in collected
        if not sw.reachable for mac in sw.own_macs
    }
    hosts = [h for h in hosts if h["mac"] not in stub_macs]
    switches = switches + stubs
    rt.prev_pseudo_ports = {
        (p["switch"], p["port"]) for p in pseudo_switches
    }
    # Addresses a bit or two away from a real one on the same
    # switch: damaged frames, not new devices. They are held back
    # from confirmation for as long as they look like copies —
    # otherwise a permanently damaged port would simply confirm its
    # phantoms. A sample counts on any port of the switch and for
    # the grace window after it was last seen: a device switched
    # off or moved to the next port leaves its copies behind.
    suspects = corruption.find_suspects(
        hosts, copy_candidates,
        corruption.samples_by_switch(
            hosts, db_rows, with_ip, grace, time.time()
        ),
        rt.config.thresholds.corruption_hamming_bits,
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
        sw.ip for sw in collected if sw.reachable and sw.from_reading
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
    if copied and observe:
        log.info(
            "%d device(s) behind %d switch(es) without a full poll in "
            "this scan (over budget or not answering) are drawn from "
            "their last readings: they stay on the map, and none of it "
            "is recorded as a sighting",
            copied, len(stale_switches),
        )
    new_macs = []
    if not observe:
        pass
    elif rt.config.demo:
        new_macs = await asyncio.to_thread(
            rt.db.upsert_hosts, observed + seen_nowhere, confirm_scans,
            suspect_macs if rt.config.filter_suspect_macs else set(),
            set(own),
        )
        await asyncio.to_thread(demo.enrich_db, rt.db, hosts)
    else:
        new_macs = await asyncio.to_thread(
            rt.db.upsert_hosts, observed + seen_nowhere, confirm_scans,
            suspect_macs if rt.config.filter_suspect_macs else set(),
            set(own),
        )
        if arp:
            # ARP knows devices no switch port ever showed (behind a
            # router or an unpolled switch): keep them as inventory
            # with an empty switch_ip. Switches and routers
            # themselves are map nodes, not hosts.
            infra_ips = set(rt.config.switches) | set(rt.config.routers)
            own_macs = _switch_macs()
            arp_hosts = {
                mac: ip for mac, ip in arp.items()
                if mac not in own_macs and ip not in infra_ips
            }
            created = await asyncio.to_thread(
                rt.db.set_ips, arp_hosts, True
            )
            if created:
                log.info(
                    "ARP: %d devices known but not seen on any switch "
                    "port", created,
                )
        await resolve_names()
    if observe:
        # ARP vouches for an address, so a newcomer it knows joins the
        # map in the same poll it first appeared in
        new_macs += await asyncio.to_thread(rt.db.confirm_hosts_with_ip)
        if new_macs:
            log.info("New MACs confirmed: %d", len(new_macs))
        # copies confirmed before v0.8.5 held them back, now that this
        # poll's sightings and addresses are written
        await hide_confirmed_copies(set(own))
        # an ARP answer never makes a router's interface a new device
        new_macs = [mac for mac in new_macs if mac not in own]
    unconfirmed = await asyncio.to_thread(rt.db.unconfirmed_macs)
    if unconfirmed and observe:
        log.info(
            "MACs awaiting confirmation (%d poll(s) each): %d",
            confirm_scans, len(unconfirmed),
        )
    db_rows = await asyncio.to_thread(rt.db.hosts_by_mac)
    if rt.config.filter_suspect_macs:
        # off the map whatever the database holds: a switch past its
        # budget is drawn from a reading nobody wrote down, and its
        # copies are in no table of unconfirmed addresses. Nor in
        # an offline group, behind a trunk or in the list beside
        # the map: _assemble_hosts leaves out what is in this set.
        unconfirmed |= suspect_macs | _marked_copies(db_rows)
    else:
        unconfirmed -= suspect_macs  # draw the damage instead
    hosts = [h for h in hosts if h["mac"] not in unconfirmed]
    rt.fdb_macs = {h["mac"] for h in hosts}
    if observe:
        await _release_unconfirmed_ips(db_rows)
    # One node per router of `routers:` (v0.8.11): one of its MACs, or
    # the LLDP bridge it is, and every other MAC of its drawn as that
    # node — on the same port, elsewhere, or only in the database
    placement = place_routers(
        identities, hosts, bridges,
        topo_info.get("other_devices", []) + topo_info.get("unidentified", []),
        _remembered_router_macs(
            own, db_rows, {sw.ip for sw in collected if sw.reachable},
            rt.fdb_macs | unconfirmed,
        ),
    )
    _merge_router_macs(placement, hosts, bridges)
    # a record on an empty node has no port to be drawn at
    hosts, offline_groups, unlocated, trunk_groups = _assemble_hosts(
        hosts, db_rows, pseudo_switches,
        {sw.ip for sw in collected if sw.reachable},
        unconfirmed,
        {
            **placement.merged,
            **{h["mac"]: h["merged_into"] for h in hosts if h.get("merged_into")},
        },
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
    # a card can say since when a device has been behind that port.
    # Only what a switch read in full this scan said: "last seen" of a
    # neighbour out of a reading is the same copy-as-sighting as a
    # device's would be.
    now = time.time()
    fresh = [sw for sw in collected if sw.map_state == STATE_FRESH]
    fresh_ips = {sw.ip for sw in fresh}
    if observe:
        await asyncio.to_thread(rt.db.upsert_lldp, _lldp_rows(fresh), now)
        await asyncio.to_thread(
            rt.db.upsert_bridges,
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
                for b in bridges if b["switch"] in fresh_ips
            ],
            now,
        )
        for ip, polled_at, data in readings:
            await asyncio.to_thread(
                rt.db.save_switch_reading, ip, polled_at, READING_FORMAT,
                __version__, data,
            )
    bridge_rows = await asyncio.to_thread(rt.db.bridges)
    for bridge in bridges:
        row = bridge_rows.get(bridge["chassis_id"], {})
        bridge["first_seen"] = row.get("first_seen", now)
        bridge["last_seen"] = row.get("last_seen", now)
        bridge["known"] = _is_known_bridge(bridge)
    _merge_bridge_identity(bridges, hosts, db_rows)
    # the address the operator reaches a router at, for the caption
    router_ips = _router_addresses(arp, identities)
    for host in hosts:
        if host["mac"] in router_ips:
            host["router_ip"] = router_ips[host["mac"]]
    for bridge in bridges:
        if bridge["chassis_id"] in router_ips:
            bridge["router_ip"] = router_ips[bridge["chassis_id"]]
    _mark_router_nodes(placement, hosts, bridges, db_rows)
    if observe:
        await _router_nodes_take_over(placement)
    # Ports that look like a way out but are not configured as one.
    # Addresses are only known once the DB has been merged in, so
    # this happens here rather than inside build_topology.
    rt.uplink_suspects.clear()
    rt.uplink_suspects.update(
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
    for (sw_ip, port), why in (
        sorted(rt.uplink_suspects.items()) if observe else ()
    ):
        log.info(
            "%s port %s looks like a way out of the network (%s). Add "
            "\"%s:%s\" to uplink_ports and what is behind it becomes "
            "one \"External network\" node instead of a crowd of "
            "devices, and raises no bridge alarms.",
            sw_ip, port, why["text"], sw_ip, port,
        )
    stp_report = _stp_report(collected, topo_info.get("trunk_names", {}))
    rt.state.update(
        switches, links, hosts, pseudo_switches, vlan_names, unlocated,
        offline_groups, bridges, stp_report,
        topo_info.get("external_networks", []),
        trunk_groups,
        scanned_at=None if observe else max(
            (sw.polled_at for sw in collected if sw.reachable), default=None
        ),
    )
    if not observe:
        log.info(
            "Map drawn from the saved readings: %d switches, %d links, %d "
            "hosts — nothing of it recorded until the first scan",
            len(switches), len(links), len(hosts),
        )
        return
    if rt.config.demo:
        await run_ping()  # set the switches' ping state right away

    # A switch that ran out of budget is not reported either way.
    # "Missed an SNMP poll" is a statement about the switch; this
    # one is a statement about us, and switch_down must not be
    # raised on the strength of it — nor cleared, which would be
    # just as much of an invention.
    # And it goes by the answer in this scan, never by what is drawn:
    # a switch on the map from its last reading may be silent now.
    await rt.alarm_engine.on_scan(
        {
            sw.ip: bool(sw.answered) for sw in collected
            if not sw.over_budget and sw.answered is not None
        },
        {sw.ip: sw.sys_name or sw.ip for sw in collected},
    )
    # Every single switch silent at once is not ten faults, it is
    # one. The day `community: public` went back into the config,
    # the map emptied and the log said "does not respond to SNMP"
    # ten times over — true, unhelpful, and identical to what a
    # power cut would have printed.
    answered = [sw for sw in collected if sw.answered]
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
    # Every switch over budget goes in, not only those past the
    # threshold: an alarm that is already up has its text brought up to
    # date on every scan — after a restart, on the first (v0.8.8).
    stale_switches = {
        sw.ip: {
            "scans": sw.over_budget_scans,
            "polled_at": sw.polled_at if sw.map_state != STATE_NEVER else 0.0,
            "source": "saved" if sw.ip in rt.readings_from_database else "run",
            "started_at": STARTED_AT,
            "answered": sw.answered,
        }
        for sw in collected
        if sw.over_budget
    }
    await rt.alarm_engine.on_stale_switches(
        stale_switches,
        {sw.ip: sw.sys_name or sw.ip for sw in collected},
        threshold=max(rt.config.stale_switch_scans, 1),
        # how each switch came out of this scan: the words a standing
        # switch_stale clears with depend on it (v0.8.9)
        states={
            sw.ip: {"state": sw.map_state, "answered": sw.answered}
            for sw in collected
        },
    )
    if over_budget:
        log.warning(
            "Scan finished without %d of %d switch(es): %s did not "
            "answer in full inside the budget. The map below is "
            "everything else, and their last readings are kept as "
            "they were.",
            len(over_budget), len(collected),
            ", ".join(over_budget),
        )
    await rt.alarm_engine.clear_suppressed(
        _suppressed_bridges(bridges, bridge_rows)
    )
    await rt.alarm_engine.on_bridges(bridges, _known_bridge_ids())
    await rt.alarm_engine.on_stp(stp_report)
    await rt.alarm_engine.on_corruption(
        {f"{sw_ip}:{port}": found
         for (sw_ip, port), found in suspects.items()},
        inbound_errors=_subject_inbound_errors,
        window_minutes=_copies_window() / 60,
        maker=lambda mac: rt.vendors.lookup(mac).name or "",
    )
    if new_macs and rt.first_scan_done:
        rows = await asyncio.to_thread(rt.db.hosts_by_mac)
        await rt.alarm_engine.on_new_macs(
            new_macs,
            {mac: _new_device_text(mac, rows[mac])
             for mac in new_macs if mac in rows},
        )
    rt.first_scan_done = True
    log.info(
        "Scan finished: %d switches, %d links, %d hosts",
        len(switches), len(links), len(hosts),
    )
    await _purge_layout_once()
    # A scan where no switch answered says nothing about where the
    # nodes are: it would count every note as one scan without its
    # node, and an outage of half an hour would empty the map of
    # them
    if answered:
        await _reconcile_notes()


async def collect_arp(collector: SnmpCollector) -> dict[str, str]:
    """The merged ARP table of all routers: MAC -> IP."""
    tables = await asyncio.gather(
        *(collector.collect_arp(ip) for ip in rt.config.routers)
    )
    merged: dict[str, str] = {}
    for table in tables:  # on conflict the last entry wins
        merged.update(table)
    return merged


async def collect_identities(collector: SnmpCollector) -> list[RouterIdentity]:
    """What each router of `routers:` says about itself (v0.8.11).

    A router that does not answer the tables has an empty identity, and
    its MACs are known through ARP alone, as before. That is said once
    at INFO, and so is the identity when it first arrives or changes —
    not on every scan.
    """
    identities = await asyncio.gather(
        *(collector.collect_router_identity(ip) for ip in rt.config.routers),
        return_exceptions=True,
    )
    result: list[RouterIdentity] = []
    for ip, identity in zip(rt.config.routers, identities):
        if isinstance(identity, BaseException):
            log.warning(
                "Router %s: reading its interfaces raised — its MACs are "
                "known through ARP alone in this scan", ip,
                exc_info=identity,
            )
            identity = RouterIdentity(address=ip)
        _report_identity(identity)
        result.append(identity)
    return result


def _report_identity(identity: RouterIdentity) -> None:
    """One INFO line when a router's identity appears, changes or goes."""
    macs = frozenset(identity.macs)
    if rt._identity_reported.get(identity.address) == macs:
        return
    rt._identity_reported[identity.address] = macs
    if not identity:
        log.info(
            "Router %s did not answer ifPhysAddress: its own MACs are known "
            "only through ARP, as before v0.8.11 (a router's own addresses "
            "are not in its ARP table, so those MACs have no IP and may be "
            "taken for damaged copies). Said once, until it answers.",
            identity.address,
        )
        return
    log.info(
        "Router %s: %d MAC(s) of its own — %s; %s",
        identity.address, len(macs), identity.describe(),
        f"{identity.address} is on {identity.name(identity.primary)}"
        if identity.primary else
        f"which interface {identity.address} is on is not known "
        "(no ipAdEntIfIndex)",
    )


async def run_ping() -> None:
    """One ping cycle: every host with an IP and every switch."""
    now = time.time()
    if rt.config.demo:
        # No real pings: the demo scenario drives the ping state
        # (one host stays silent, another recovers after a while)
        results_by_mac = await asyncio.to_thread(demo.ping_results, rt.db)
        for sw in rt.state.as_dict()["switches"]:
            # the switch the demo takes down does not ping either
            up = not demo.switch_down_now(sw["ip"])
            prev = rt.switch_ping.get(sw["ip"], {})
            rt.switch_ping[sw["ip"]] = {
                "ping_up": up,
                "last_ping_ok": now if up else prev.get("last_ping_ok", 0),
            }
    else:
        targets = await asyncio.to_thread(rt.db.hosts_with_ip)
        # One ping per unique address, the result applied to every host
        # holding it: IPs are unique in the DB since v0.5.3, but a
        # single probe per address is the right shape regardless
        unique_ips = list(dict.fromkeys(ip for _, ip in targets))
        ips = unique_ips + list(rt.config.switches)
        if not ips:
            return
        results = await pinger.ping_many(ips)
        results_by_mac = {mac: results.get(ip, False) for mac, ip in targets}
        await asyncio.to_thread(rt.db.update_ping, results_by_mac, now)
        for ip in rt.config.switches:
            prev = rt.switch_ping.get(ip, {})
            up = results.get(ip, False)
            rt.switch_ping[ip] = {
                "ping_up": up,
                "last_ping_ok": now if up else prev.get("last_ping_ok", 0),
            }
    if results_by_mac:
        # Only ping targets can affect this cycle's host alarms. Avoid
        # rebuilding metadata for the entire inventory every minute.
        meta = await asyncio.to_thread(rt.db.hosts_by_macs, results_by_mac)
        for mac, row in meta.items():
            row["monitored"] = _effective_monitored(row)
            # A located host missing from the latest FDB is not
            # confirmed present, so it raises no alarms. Hosts that
            # were never located (ARP-only) keep alerting normally.
            row["stale"] = bool(row["switch_ip"]) and mac not in rt.fdb_macs
        await rt.alarm_engine.on_ping(results_by_mac, meta)


def _lag_label(sw: SwitchData, members: list[int]) -> str:
    """Stable alarm label of a LAG, e.g. "lag[Slot0/1+Slot0/2]".

    Built from the member port names: a member going down changes only
    its oper status, never the composition, while the synthetic
    bridge-port number D-Link exposes DOES change on member flaps —
    keying alarms by it left them hanging active forever (v0.5.1 bug).
    """
    names = [port_name(sw, m) for m in sorted(members)]
    return "lag[" + "+".join(names) + "]"


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
    if not rt.switch_data:
        return  # port lists are unknown until the first scan
    oper_by_ip: dict[str, dict[int, bool]] = {}
    loop_by_ip: dict[str, object] = {}
    if rt.config.demo:
        # the demo flips port states directly in switch_data
        samples_by_ip = rt.demo_counters.sample(list(rt.switch_data.values()))
        rt.counter_columns.clear()
        rt.counter_columns.update(
            {ip: rt.demo_counters.columns(ip) for ip in samples_by_ip}
        )
        loop_by_ip = {
            ip: rt.demo_counters.loop_detection(rt.switch_data[ip])
            for ip in samples_by_ip if ip in rt.switch_data
        }
    else:
        collector = get_collector()
        ips = list(rt.config.switches)
        # One switch failing must not cost the others their counters:
        # a single malformed OID on sw1 emptied every column on all
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
        rt.counter_columns.clear()
        rt.counter_columns.update({ip: p[2] for ip, p in polls.items()})
        loop_by_ip = {
            ip: p[3] for ip, p in polls.items() if p[3] is not None
        }
    for ip, samples in samples_by_ip.items():
        if not samples:
            continue
        sw = rt.switch_data.get(ip)
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
        rates = rt.counter_store.update(ip, samples, speeds)
        if sw is not None:
            await rt.alarm_engine.on_counters(ip, _port_metrics(sw, rates))
            await _check_flaps(sw, oper, samples)
    for ip, loop in loop_by_ip.items():
        sw = rt.switch_data.get(ip)
        if sw is None or loop is None:
            continue
        sw.loop_detection = loop
        _log_loop_state(sw, loop)
        await rt.alarm_engine.on_loop_detection(
            ip, sw.sys_name or ip, loop,
            {i: p.name or str(i) for i, p in sw.ports.items()},
            _stp_operating(sw),
        )
    await rt.alarm_engine.janitor(_observed_subjects())


def _stp_operating(sw: SwitchData) -> bool | None:
    """Whether spanning tree runs on this switch, as the last scan judged
    it — judge_network() included, which is what confirms a root by its
    neighbours. None when its STP is not visible over SNMP at all."""
    stp = sw.stp
    if stp is None or not getattr(stp, "supported", False):
        return None
    return bool(stp.operating)


def _log_loop_state(sw: SwitchData, loop) -> None:
    """One line per switch, and only when the answer changes."""
    line = loopdetect.describe(loop)
    if rt._loop_logged.get(sw.ip) == line:
        return
    rt._loop_logged[sw.ip] = line
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
    flaps = rt.flap_tracker.update(
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
        rt.flap_by_port[(sw.ip, name)] = info
        entries.append(
            {"port": name, "flaps": info.count, "last": info.last}
        )
    if entries:
        await rt.alarm_engine.on_flaps(sw.ip, entries)


def _observed_subjects() -> set[tuple[str, str]]:
    """(type, subject) pairs that exist in the current switch data —
    the janitor auto-clears active alarms that fell out of this set."""
    observed: set[tuple[str, str]] = set()
    for ip, sw in rt.switch_data.items():
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
