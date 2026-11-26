"""MoonLan web service: REST API and the static web UI."""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import logging
import socket
import time
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import (
    __version__, about, corruption, counters, demo, housekeeping, https,
    loopdetect, menu, notes, notify, oui, pinger, probes, settings, signin,
    startup, stp, subscriptions,
)
from .alarms import AlarmEngine
from .config import Config, load_config, parse_uplink_ports
from .db import Database
from .housekeeping import (  # noqa: F401 (moved)
    CERT_ALARM_DAYS,
    CERT_CHECK_SECONDS,
    CLEANUP_SECONDS,
    RESOURCE_LOG_SECONDS,
)
from .notify import Notifier
from .routers import (
    RouterIdentity,
    card as router_card,
    own_macs as router_own_macs,
    place as place_routers,
)
from .runtime import rt
from .startup import CERT_WARN_DAYS  # noqa: F401 (moved)
from .snmp_collector import (
    READING_FORMAT,
    OverBudget,
    STATE_FRESH,
    STATE_NEVER,
    STATE_OVER_BUDGET,
    STATE_RESTORED,
    STATE_UNREACHABLE,
    SnmpCollector,
    SwitchData,
    is_random_mac,
)
from .topology import (
    FdbStability,
    TopologyState,
    build_topology,
    group_beyond_trunk,
    lldp_metadata,
    port_name,
    suspect_uplink_ports,
)

log = logging.getLogger("moonlan")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

rt.state = TopologyState()
rt.config: Config = load_config()
# listen.public_url, taken apart: the origin requests and passkeys are
# checked against, and the host a passkey is bound to
rt.public_url, rt.public_url_problems = https.parse_public_url(
    rt.config.listen_public_url
)
rt.trusted_proxies, rt.trusted_proxy_problems = https.parse_trusted_proxies(
    rt.config.listen_trusted_proxies
)
# listen.tls_cert, read once at startup: the certificate uvicorn loaded
# is the one this process serves until it is restarted
rt.certificate: https.CertInfo | None = None
# The IEEE register of MAC blocks: who made a device. Read once, at
# startup; without it every maker is "unknown" and nothing else changes
rt.vendors = oui.Registry.load(rt.config.oui_folder())
# In demo mode the DB lives in memory so the real one is not polluted
rt.db = Database(":memory:" if rt.config.demo else rt.config.db_path)
# Settings (v0.8.4): the database's values onto the config, and
# config.yaml's moved into the database where it has none. Before
# anything below reads the config; said in the log at startup.
rt.settings_loaded = settings.load(rt.config, rt.db)
# Accounts and sessions: the same database — except in demo mode, where
# they need a file python -m moonlan.users can reach (see
# Config.users_db_path)
rt.accounts = Database(rt.config.users_db_path()) if rt.config.demo else rt.db
# "tls", "proxy" or "http": said at startup, and what the warning about
# passwords in the clear depends on
rt.transport = https.transport_mode(
    rt.config.listen_tls_cert, rt.trusted_proxies, rt.public_url
)
rt.sign_in = signin.SignIn(rt.accounts, rt.db, rt.config.auth, rt.public_url, rt.transport)
# For About (v0.8.1): when this process started, and the commit it was
# started from — read once, as a pull after the start changes .git and
# not the code that is running
STARTED_AT = time.time()
BUILD = about.build_hash()
COPYRIGHT = about.copyright_line(about.license_text())

# Ping state of switches (they are not in the hosts table): ip -> {ping_up, last_ping_ok}
rt.switch_ping: dict[str, dict] = {}


async def _demo_probe(argv: list[str], timeout: float):
    """The menu's ping and traceroute in demo mode: nothing is sent.
    A device answers when the demo's own monitoring says it does."""
    ip = argv[-1]
    row = await asyncio.to_thread(rt.db.host_by_ip, ip)
    answers = bool(row and row["ping_up"]) or any(
        sw_ip == ip and ping.get("ping_up")
        for sw_ip, ping in rt.switch_ping.items()
    )
    return await demo.fake_probe(argv, answers)


# The node menu's ping and traceroute: which tools this machine has,
# and the requests running or recently finished
rt.tools = (
    {"ping": "simulated", "traceroute": ("traceroute", "simulated")}
    if rt.config.demo else probes.find_tools()
)
rt.jobs = probes.Jobs(rt.tools, _demo_probe if rt.config.demo else probes.run)

# FDB merged with previous polls: protects links from MAC table aging
rt.fdb_stability = FdbStability()

# Latest raw poll data per switch: the ports API, the counters loop and
# the link load labels all need port lists, speeds and LAG composition
rt.switch_data: dict[str, SwitchData] = {}
# ip -> where the time of its last poll went (v0.8.8), for /api/polling:
# "cut_at" — where the last poll that ran out of budget stopped, None
# once a full poll finishes; "slowest_walks" — the longest walks of the
# last poll, full or cut. A poll with no answer changes neither.
rt.poll_report: dict[str, dict] = {}
# switches whose drawn reading is the one restored from the database at
# the start, until their first full poll (v0.8.8): switch_stale says so
rt.readings_from_database: set[str] = set()

rt.counter_store = counters.CounterStore()
# Link-state transitions per port over a sliding window: a port can
# bounce four times between two counter polls, and oper status alone
# would show none of it
rt.flap_tracker = counters.FlapTracker(
    rt.config.thresholds.flap_window_minutes * 60
)
# (switch ip, port name) -> the flap count shown in the ports panel
rt.flap_by_port: dict[tuple[str, str], counters.PortFlaps] = {}
ZERO_FLAPS = counters.PortFlaps(count=0, last=0.0)

# switch ip -> which counter columns answered in the latest poll, so the
# ports panel can say "unknown" where it used to say 0.0
rt.counter_columns: dict[str, dict[str, counters.ColumnStatus]] = {}
rt.notifier = Notifier(rt.config, demo=rt.config.demo)
# One's own email and Telegram (v0.8.4): kept beside the accounts,
# journalled with everything else; the notifier asks it who chose what
rt.personal = subscriptions.Subscriptions(
    rt.accounts, rt.db, rt.config, rt.notifier, demo=rt.config.demo
)
rt.notifier.personal = rt.personal.recipients
rt.alarm_engine = AlarmEngine(
    rt.db, rt.notifier, rt.config.thresholds, rt.config.notifications
)
rt.demo_counters = demo.DemoCounters() if rt.config.demo else None

# The first scan is the initial inventory: every MAC is "new" there,
# alerting on all of them would be pure noise
rt.first_scan_done = False

# MACs present in the FDB of the latest scan. A host missing from it is
# "stale": still drawn at its last known port during the grace window,
# but never alarmed on — its presence is no longer confirmed.
rt.fdb_macs: set[str] = set()

# (switch ip, port) of the pseudo-switches drawn last time: a group
# that already exists must not vanish while any device is left on the
# port, or the nodes would jump around between scans
rt.prev_pseudo_ports: set[tuple[str, str]] = set()

# (switch ip, port) -> the distorted MACs of the latest scan, for the
# ports panel and the diagnostics
rt.suspect_by_port: dict[tuple[str, str], list[dict]] = {}

# (switch ip, port) -> why the port looks like a way out of the network
# and might belong in config.uplink_ports
rt.uplink_suspects: dict[tuple[str, str], dict] = {}

# Saved node positions are cleaned up once, after the first scan —
# not at startup proper. The rule is "old AND no longer on the map",
# and before the first scan there is no map: every position would look
# orphaned and the whole layout would be thrown away on a restart.
rt.layout_purged = False

# Loop-detection profiles: the built-in ones plus whatever
# config.yaml adds or overrides by name (see loopdetect.py)
rt._loop_profiles: list | None = None


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

# One SnmpEngine per process: a new engine per cycle leaks sockets and
# MIB state (OSError 24, MibNotFoundError, growing RSS). Recreate only
# if the SNMP config ever changes at runtime — it currently cannot.
rt._collector: SnmpCollector | None = None


# One poll per switch at a time. A DGS-1210 answers one request at a
# time, and the scan and the counters loop can otherwise land on the
# same agent together. Different switches stay parallel — this is a
# lock per IP, not a global one.
rt._host_locks: dict[str, asyncio.Lock] = {}


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


def configured_switches() -> list[str]:
    """The switches the map is about: `switches:` in config.yaml, or the
    demo's own network. Each of them is on the map whatever it answers
    (v0.8.7) — from this scan, from a reading, or as an empty node."""
    return list(demo.SWITCH_IPS) if rt.config.demo else list(rt.config.switches)


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


def _infrastructure() -> set[str]:
    """Addresses of kit that is ours: the switches and the routers.

    In demo mode these come from the demo network, not from the
    config.yaml that happens to be in the working directory.
    """
    if rt.config.demo:
        return set(rt.switch_data) | set(demo.ROUTERS)
    return set(rt.config.switches) | set(rt.config.routers)


def _router_addresses(
    arp: dict[str, str], identities: list[RouterIdentity] | None = None,
) -> dict[str, str]:
    """MAC -> the address config.routers actually reaches it at.

    A router announces a management address per VLAN interface — the
    MikroTik behind sw0 Slot0/21 sends 38 — and the first one the walk
    returns is as likely to be a segment gateway as the address anyone
    uses. The one in `routers:` is the one the operator types, so that
    is what goes under the node's name.

    Every MAC a router reports as its own is that router (v0.8.11). ARP
    is what is left when it reports nothing: a router's own addresses
    are not in its own ARP table, only in another router's.
    """
    found = {
        mac: identity.address
        for mac, identity in router_own_macs(identities or ()).items()
    }
    listed = set(rt.config.routers)
    for mac, ip in arp.items():
        if ip in listed:
            found.setdefault(mac, ip)
    if rt.config.demo:
        for mac, ip in demo.ROUTER_ADDRESSES.items():
            found.setdefault(mac, ip)
    return found


async def _release_router_macs(own: dict[str, RouterIdentity]) -> None:
    """A router's own MACs, out of what the damaged-copy rules did to
    them: the mark of a copy cleared, a record held back confirmed. One
    INFO line for what was undone; a record that was only new is
    confirmed without one."""
    released = await asyncio.to_thread(
        rt.db.vouch_for, set(own), max(rt.config.new_host_confirm_scans, 1)
    )
    undone = [r for r in released if r["was"] != "new"]
    if not undone:
        return
    log.info(
        "Own MACs of routers in routers: are not damaged copies, and are "
        "devices now: %s",
        "; ".join(
            f"{r['mac']} ({own[r['mac']].name(r['mac'])} of "
            f"{own[r['mac']].address}, "
            + (
                f"was marked a copy of {r['sample']}" if r["was"] == "copy"
                else "was held back as a possible copy"
            ) + ")"
            for r in undone
        ),
    )


def _remembered_router_macs(
    own: dict[str, RouterIdentity], db_rows: dict[str, dict],
    switch_ips: set[str], drawn: set[str],
) -> dict[str, dict]:
    """The own MACs of routers the map would draw out of the grace
    window — the records _assemble_hosts puts on the map grey — as
    mac -> {switch, port, last_seen}. `drawn` are the MACs this scan
    has already (and the unconfirmed ones)."""
    grace = rt.config.host_grace_hours * 3600
    now = time.time()
    remembered: dict[str, dict] = {}
    for mac in own:
        row = db_rows.get(mac)
        if row is None or mac in drawn:
            continue
        if row["switch_ip"] not in switch_ips or not row["port"]:
            continue
        if grace > 0 and now - row["last_seen"] < grace:
            remembered[mac] = {
                "switch": row["switch_ip"], "port": row["port"],
                "last_seen": row["last_seen"],
            }
    return remembered


def _merge_router_macs(placement, hosts: list[dict], bridges: list[dict]) -> None:
    """This scan's hosts that are a router's MAC other than its node:
    drawn as the node. A bridge that is the router no longer counts
    them among the devices behind it — they are the bridge."""
    by_id = {bridge["id"]: bridge for bridge in bridges}
    for host in hosts:
        target = placement.merged.get(host["mac"])
        if not target or host.get("merged_into"):
            continue
        host["merged_into"] = target
        bridge = by_id.get(target)
        if (
            bridge is not None and bridge.get("host_count")
            and host.get("via") == target
        ):
            bridge["host_count"] -= 1


def _mark_router_nodes(
    placement, hosts: list[dict], bridges: list[dict],
    db_rows: dict[str, dict],
) -> None:
    """The node of each router learns it is one: its address, its
    interfaces for the card, and — for a host — the LLDP neighbour that
    is the router on its port, whatever chassis ID that one sent."""
    by_mac = {host["mac"]: host for host in hosts}
    by_id = {bridge["id"]: bridge for bridge in bridges}
    for node in placement.nodes:
        identity = node.identity
        target = by_id.get(node.node_id) if node.is_bridge else by_mac.get(node.mac)
        if target is None:
            continue
        target["router_ip"] = identity.address
        target["router"] = router_card(
            identity, node.mac, node.seen, identity.macs & set(db_rows),
        )
        if node.lldp is not None and not node.is_bridge:
            target["lldp"] = lldp_metadata(node.lldp)


# who the journal says moved a note or a pin that followed a router's
# node: MoonLan itself, worded by the page like "@console"
MOONLAN_ACTOR = "@moonlan"


async def _router_nodes_take_over(placement) -> None:
    """A note or a pin on a router's MAC now drawn as its node goes to
    the node, once (v0.8.11).

    The operator named and pinned the MAC that was on the map; the
    router is drawn as another one now. Only when the node has none of
    its own: a node with its own note keeps it, and the note of the
    merged MAC goes to "Notes without a node" after notes.MISSING_SCANS
    scans, as any note whose node is gone. Journalled as a note moved
    and a node pinned by hand are, by MoonLan.
    """
    if not placement.nodes:
        return
    layout = await asyncio.to_thread(rt.db.layout)
    stored = await asyncio.to_thread(rt.db.notes)
    for node in placement.nodes:
        sources = [
            "host:" + mac for mac, target in sorted(placement.merged.items())
            if target == node.node_id
        ]
        if node.node_id not in stored:
            noted = [key for key in sources if key in stored]
            if noted:
                # the one written last, if the operator wrote several
                source = max(noted, key=lambda key: stored[key]["updated_at"])
                await asyncio.to_thread(
                    rt.db.move_note, source, node.node_id, MOONLAN_ACTOR,
                    time.time(),
                )
                details = {"from": source, "to": node.node_id}
                await asyncio.to_thread(
                    rt.db.add_event, time.time(), "note_moved", node.node_id,
                    json.dumps(details), MOONLAN_ACTOR,
                )
                log.info(
                    "Note moved from %s to %s by MoonLan: router %s is "
                    "drawn as %s now", source, node.node_id,
                    node.identity.address, node.node_id,
                )
        if not (layout.get(node.node_id) or {}).get("pinned"):
            pinned = [key for key in sources if (layout.get(key) or {}).get("pinned")]
            if pinned:
                source = max(pinned, key=lambda key: layout[key]["updated_at"] or 0)
                if await asyncio.to_thread(rt.db.move_position, source, node.node_id):
                    await asyncio.to_thread(
                        rt.db.add_event, time.time(), "layout_pinned", "",
                        json.dumps({
                            "n": 1, "ids": [node.node_id], "from": source,
                        }),
                        MOONLAN_ACTOR,
                    )
                    log.info(
                        "Pinned position moved from %s to %s by MoonLan: "
                        "router %s is drawn as %s now", source,
                        node.node_id, node.identity.address, node.node_id,
                    )


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
    if rt.config.demo:
        return set(demo.UPLINK_PORTS)
    return parse_uplink_ports(rt.config.uplink_ports)


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
    known = set(rt.config.known_bridges)
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
    be: an access switch sat in `switches:`, answered every scan, and still
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
            "it is a router MoonLan polls" if ip in set(rt.config.routers)
            else "it is a switch MoonLan polls"
        )
    for ip, sw in rt.switch_data.items():
        why = f"it is {sw.sys_name or ip}, a switch MoonLan polls"
        for mac in sw.own_macs:
            ours[mac.lower()] = why
        if sw.bridge_mac:
            ours[sw.bridge_mac.lower()] = why
    return ours


def _known_bridge_ids() -> set[str]:
    """Chassis ids and addresses that must never raise a bridge alarm."""
    return set(rt.config.known_bridges) | set(_polled_identities())


def _is_known_bridge(bridge: dict) -> bool:
    """Known by configuration, or one of the devices we poll ourselves."""
    known = _known_bridge_ids()
    return (
        bridge["chassis_id"].lower() in known
        or bridge.get("mgmt_ip", "").lower() in known
    )


def _copy_counts(found: list[dict]) -> dict[str, int]:
    """The addresses a port's copies are of -> how many, most first"""
    counts = Counter(s["sample"] for s in found)
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def _copy_samples(counts: dict[str, int]) -> list[dict]:
    """[{mac, vendor, n}] for the page, which words the conclusion"""
    return [
        {"mac": mac, "vendor": rt.vendors.lookup(mac).name or "", "n": n}
        for mac, n in counts.items()
    ]


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


def _apply_suspects(
    hosts: list[dict], suspects: dict[tuple[str, str], list[dict]]
) -> None:
    """Marks the distorted MACs and remembers them for the ports panel."""
    rt.suspect_by_port.clear()
    rt.suspect_by_port.update(suspects)
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
    for sw in rt.switch_data.values():
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
    hours = rt.config.ip_confirm_hours
    if hours <= 0:
        return
    now = time.time()
    cutoff = now - hours * 3600
    released = []
    for mac, row in db_rows.items():
        if not row["ip"] or mac in rt.fdb_macs:
            continue
        if row["ip_confirmed"] >= cutoff:
            continue
        if await asyncio.to_thread(
            rt.db.release_ip, mac, now,
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
    grace = rt.config.host_grace_hours * 3600
    now = time.time()
    counts: Counter = Counter()
    for row in db_rows.values():
        if not row["confirmed"]:
            continue  # not a device yet: it must not push a port over
                      # the unmanaged-switch threshold
        if row.get("corrupt_of") and rt.config.filter_suspect_macs:
            continue  # a damaged copy, never one
        if row["switch_ip"] in switch_ips and row["port"]:
            if grace > 0 and now - row["last_seen"] < grace:
                counts[(row["switch_ip"], row["port"])] += 1
    return dict(counts)


def _mark_recent(
    unlocated: list[dict], db_rows: dict[str, dict], now: float
) -> None:
    """`recent` on every record off the map (v0.8.7): seen by anybody —
    a switch, ARP, a ping — inside host_grace_hours. "Not on map" is
    the recent ones, devices that are there now without a place; the
    rest is "Not seen for a while", an archive of addresses that left.
    Before, the two were one list of 480, and the two devices that
    mattered were lost in it.

    `seen_at` is that moment, and `forget_at` when purge_old_hosts
    deletes the record (None while host_retention_days is 0) — the
    page words both. Called again on every /api/topology: pings move
    `last_ping_ok` between scans.

    ARP is a sign of presence only while the record holds an address
    (v0.8.8). `last_arp` stays when the address goes — to another MAC
    (set_ips) or nowhere, unconfirmed (release_ip) — and kept 140 of
    142 records without an IP "recent" for host_grace_hours after the
    very ARP that showed the device was not there. `seen_at` is still
    the last sighting of any kind, and `seen_by` says which: "fdb",
    "arp" or "ping".

    A ping too, since v0.8.9: it goes to the address, and once the
    address is another device's, the answer is that device's."""
    grace = rt.config.host_grace_hours * 3600
    days = rt.config.host_retention_days
    for host in unlocated:
        row = db_rows.get(host["mac"], {})
        fdb = row.get("last_seen", 0) or 0
        arp = row.get("last_arp", 0) or 0
        ping = row.get("last_ping_ok", 0) or 0
        seen = max(fdb, arp, ping)
        host["seen_at"] = seen
        host["seen_by"] = (
            "" if not seen else "fdb" if seen == fdb
            else "arp" if seen == arp else "ping"
        )
        holds_address = bool(host.get("ip") or row.get("ip"))
        present = max(fdb, max(arp, ping) if holds_address else 0)
        # seen this very scan, on an uplink: recent by definition
        host["recent"] = bool(host.get("uplink_only")) or (
            grace > 0 and now - present < grace
        )
        kept_since = max(
            row.get("last_seen", 0) or 0, row.get("last_arp", 0) or 0
        )
        host["forget_at"] = kept_since + days * 86400 if days > 0 else None


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
    grace = rt.config.host_grace_hours * 3600
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
            host["off_map"] = "uplink_only"
            unlocated.append(host)
            continue
        # a record with a switch and no port has no place on that switch
        # (v0.8.6: the ports a poll has no name for are reset to '')
        located = row["switch_ip"] in switch_ips and bool(row["port"])
        if located and grace > 0 and now - row["last_seen"] < grace:
            via = pseudo_by_port.get((row["switch_ip"], row["port"]))
            if via:
                host["via"] = via
            on_map.append(host)
        else:
            host["unlocated"] = True
            # why it is not on the map: a place it had, until the grace
            # window ran out — or no place at all (ARP only, a port
            # reset, a switch that is not drawn from a reading)
            host["off_map"] = "expired" if located else "no_port"
            unlocated.append(host)
    _mark_recent(unlocated, db_rows, now)
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
        on_map, trunk_names or {}, rt.config.trunk_group_threshold,
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
    node rather than off the switch. Behind sw1 1/27 there were three
    neighbours of the switch at once: RouterOS-C with 22 live
    devices, a "switch without SNMP", and an "Offline · 19" of
    10.1.5.x addresses standing beside them — one segment drawn as
    three places, with the quiet half apparently somewhere else
    entirely.
    """
    threshold = rt.config.offline_group_threshold
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
    return rt.config.monitored_by_default or bool(row.get("monitored", 0))


def _new_device_text(mac: str, row: dict) -> str:
    """The text of new_mac — in the alarms, the journal, Telegram and the
    mail: who made the device and where it came in, so the administrator
    knows who joined the network without opening the map. "new device by
    GIGA-BYTE TECHNOLOGY CO.,LTD. on access-sw-1 (10.0.0.21) Gi0/9"."""
    maker = rt.vendors.lookup(mac)
    who = {
        "found": f"new device by {maker.name}",
        "unregistered": "new device, its maker not in the IEEE register,",
        "local": "new device with a random or hand-set address",
    }.get(maker.status, "new device")
    switch = row.get("switch_ip", "")
    if not switch:
        return who
    name = next((sw["name"] for sw in rt.state.as_dict()["switches"]
                 if sw["ip"] == switch), "")
    where = f"{name} ({switch})" if name and name != switch else switch
    return f"{who} on {where} {row.get('port', '')}".rstrip()


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
        # who made it: the name, or why there is none (vendor_status)
        h.update(rt.vendors.fields(h["mac"]))


async def collect_arp(collector: SnmpCollector) -> dict[str, str]:
    """The merged ARP table of all routers: MAC -> IP."""
    tables = await asyncio.gather(
        *(collector.collect_arp(ip) for ip in rt.config.routers)
    )
    merged: dict[str, str] = {}
    for table in tables:  # on conflict the last entry wins
        merged.update(table)
    return merged


# routers of `routers:` whose empty identity has been reported, and
# the MACs of the last identity of each — a line when it changes, not
# one per scan
rt._identity_reported: dict[str, frozenset[str]] = {}


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


# mac -> unix time of the last reverse DNS attempt
rt._dns_attempts: dict[str, float] = {}
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
        for mac, ip in await asyncio.to_thread(rt.db.hosts_without_name)
        if now - rt._dns_attempts.get(mac, 0) >= DNS_RETRY_SECONDS
    ]
    if not candidates:
        return
    for mac, _ in candidates:
        rt._dns_attempts[mac] = now
    names = await asyncio.gather(*(_reverse_dns(ip) for _, ip in candidates))
    resolved = 0
    for (mac, _), name in zip(candidates, names):
        if name:
            await asyncio.to_thread(rt.db.set_name, mac, name)
            resolved += 1
    if resolved:
        log.info("Reverse DNS: got %d names out of %d", resolved, len(candidates))


# A loop's sleep, cut short when its interval is changed in Settings
# (v0.8.4): without it a new interval held only after the sleep in
# progress — up to ten minutes for the scan.
rt.interval_changed = {
    key: asyncio.Event() for key in settings.INTERVALS
}


async def sleep_interval(key: str, seconds) -> None:
    """Sleeps until the next run is due: `seconds()` after the sleep
    began. A changed interval wakes it, and the due time is counted
    again from the same start — a shorter one may be due at once.
    `seconds()` None — only on request: until the interval changes."""
    began = time.monotonic()
    changed = rt.interval_changed[key]
    while True:
        changed.clear()
        interval = seconds()
        if interval is None:
            await changed.wait()
            continue
        left = began + interval - time.monotonic()
        if left <= 0:
            return
        try:
            await asyncio.wait_for(changed.wait(), left)
        except asyncio.TimeoutError:
            return


def _scan_seconds() -> float | None:
    # 0 — only on request (the Actions menu, /api/scan), as documented;
    # before v0.8.4 it meant once an hour
    interval = rt.config.scan_interval_minutes
    return interval * 60 if interval > 0 else None


async def periodic_scan() -> None:
    while True:
        try:
            await run_scan()
        except Exception as exc:
            # The map from the previous scan stays on screen; what
            # changes is that the header now says the last attempt
            # failed, and with what
            rt.state.scan_failed(exc)
            log.exception("Network scan failed")
        await sleep_interval("scan_interval_minutes", _scan_seconds)


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


async def periodic_ping() -> None:
    while True:
        try:
            await run_ping()
        except Exception:
            log.exception("Ping monitoring failed")
        await sleep_interval(
            "ping_interval_seconds", lambda: max(rt.config.ping_interval_seconds, 1)
        )


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


# What each switch's loop detection last said, so the same line is not
# repeated every minute for a network where nothing is happening
rt._loop_logged: dict[str, str] = {}


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


async def periodic_counters() -> None:
    while True:
        try:
            await run_counters()
        except Exception:
            log.exception("Counter polling failed")
        await sleep_interval(
            "counters_interval_seconds",
            lambda: max(rt.config.counters_interval_seconds, 5),
        )


def resource_usage() -> tuple[int, int]:
    """Moved to housekeeping.resource_usage."""
    return housekeeping.resource_usage()


async def periodic_cleanup() -> None:
    while True:
        await asyncio.sleep(CLEANUP_SECONDS)
        try:
            await purge_old_hosts()
            await purge_journal()
            await purge_copies()
            await rt.alarm_engine.clear_missing_hosts(
                set(await asyncio.to_thread(rt.db.hosts_by_mac))
            )
            if rt.layout_purged:
                # the map exists: a scan has run since the start
                await purge_layout()
        except Exception:
            log.exception("Cleanup failed")


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
    return max(rt.config.stale_rate_hide_minutes, 0.0) * 60


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
        sw = rt.switch_data.get(link[side])
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
        sw = rt.switch_data.get(link[side])
        if sw is None:
            continue
        rates = rt.counter_store.current(link[side], max_age=_rates_hide_age())
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


def _config_missing() -> dict | None:
    """Moved to startup._config_missing."""
    return startup._config_missing(config=rt.config)


def _log_config() -> None:
    """Moved to startup._log_config."""
    startup._log_config(
        config=rt.config, db=rt.db, tools=rt.tools, vendors=rt.vendors,
        settings_loaded=rt.settings_loaded, public_url=rt.public_url,
        public_url_problems=rt.public_url_problems,
        trusted_proxies=rt.trusted_proxies,
        trusted_proxy_problems=rt.trusted_proxy_problems, transport=rt.transport,
    )


def _setting_problem(error: dict) -> str:
    """Moved to startup._setting_problem."""
    return startup._setting_problem(error)


def _log_settings() -> None:
    """Moved to startup._log_settings."""
    startup._log_settings(settings_loaded=rt.settings_loaded, db=rt.db)


def _log_public_url() -> None:
    """Moved to startup._log_public_url."""
    startup._log_public_url(
        config=rt.config, public_url=rt.public_url,
        public_url_problems=rt.public_url_problems,
        trusted_proxies=rt.trusted_proxies,
        trusted_proxy_problems=rt.trusted_proxy_problems, transport=rt.transport,
    )


def _log_transport() -> None:
    """Moved to startup._log_transport."""
    startup._log_transport(
        config=rt.config, public_url=rt.public_url,
        trusted_proxies=rt.trusted_proxies, transport=rt.transport,
    )


def _log_certificate() -> None:
    """Moved to startup._log_certificate."""
    found = startup._log_certificate(config=rt.config, public_url=rt.public_url)
    if found is not None:
        rt.certificate = found


async def check_certificate_end() -> None:
    """Moved to housekeeping.check_certificate_end."""
    await housekeeping.check_certificate_end(
        certificate=rt.certificate, public_url=rt.public_url, config=rt.config,
        alarm_engine=rt.alarm_engine,
    )


async def periodic_certificate_check() -> None:
    while True:
        await asyncio.sleep(CERT_CHECK_SECONDS)
        try:
            await check_certificate_end()
        except Exception:
            log.exception("Certificate check failed")


async def start_redirect_listener():
    """Moved to startup.start_redirect_listener."""
    return await startup.start_redirect_listener(
        config=rt.config, public_url=rt.public_url
    )


async def drop_unpinned_positions() -> None:
    """Moved to housekeeping.drop_unpinned_positions."""
    await housekeeping.drop_unpinned_positions(db=rt.db)


async def purge_invalid_macs() -> None:
    """Moved to housekeeping.purge_invalid_macs."""
    await housekeeping.purge_invalid_macs(db=rt.db)


async def purge_old_hosts() -> None:
    """Moved to housekeeping.purge_old_hosts."""
    await housekeeping.purge_old_hosts(config=rt.config, db=rt.db)


async def purge_journal() -> None:
    """Moved to housekeeping.purge_journal."""
    await housekeeping.purge_journal(
        config=rt.config, db=rt.db, JOURNAL_GROUPS=JOURNAL_GROUPS
    )


def _marked_copies(db_rows: dict[str, dict]) -> set[str]:
    """Records marked as damaged copies (v0.8.5)."""
    return {mac for mac, row in db_rows.items() if row.get("corrupt_of")}


async def hide_confirmed_copies(vouched: set[str] | None = None) -> None:
    """Moved to housekeeping.hide_confirmed_copies."""
    await housekeeping.hide_confirmed_copies(vouched, config=rt.config, db=rt.db)


async def purge_copies() -> None:
    """Moved to housekeeping.purge_copies."""
    await housekeeping.purge_copies(config=rt.config, db=rt.db)


def configure_logging() -> None:
    """Moved to startup.configure_logging."""
    startup.configure_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    _log_config()
    if rt.config.demo:
        log.info("MoonLan started in DEMO mode (virtual network)")
        await asyncio.to_thread(demo.seed_notes, rt.db)
        await asyncio.to_thread(demo.seed_reading, rt.db)
        await asyncio.to_thread(demo.seed_stale_alarm, rt.db)
    elif not rt.config.switches:
        log.warning(
            "No switches are configured in config.yaml. Add addresses to "
            "the switches section or start with MOONLAN_DEMO=1."
        )
    await asyncio.to_thread(rt.sign_in.log_state)
    await asyncio.to_thread(
        rt.sign_in.write_console_token, rt.config.users_db_path() + ".console"
    )
    await purge_invalid_macs()
    await purge_old_hosts()
    await purge_journal()
    await purge_copies()
    await hide_confirmed_copies()
    await drop_unpinned_positions()
    await rt.alarm_engine.load()
    await rt.alarm_engine.clear_missing_hosts(
        set(await asyncio.to_thread(rt.db.hosts_by_mac))
    )
    # the map from the readings saved before the restart, at once
    try:
        await restore_readings()
    except Exception:
        log.exception("Restoring the saved switch readings failed")
    _log_certificate()
    await check_certificate_end()
    # a Telegram link made just before a restart still works
    if await asyncio.to_thread(rt.accounts.waiting_links, time.time()):
        rt.personal.ensure_polling()
    redirect = await start_redirect_listener()
    tasks = [
        asyncio.create_task(periodic_scan()),
        asyncio.create_task(periodic_ping()),
        asyncio.create_task(periodic_counters()),
        asyncio.create_task(periodic_resource_log()),
        asyncio.create_task(periodic_certificate_check()),
        asyncio.create_task(periodic_cleanup()),
    ]
    yield
    for task in tasks:
        task.cancel()
    if redirect is not None:
        redirect.close()
    rt.sign_in.remove_console_token()


# a key that looks copied raises an alarm like anything on the network
rt.sign_in.raise_alarm = lambda alarm_type, subject, message: (
    rt.alarm_engine.service_alarm(alarm_type, subject, True, message)
)

app = FastAPI(title="MoonLan", version=__version__, lifespan=lifespan)
# Every request passes the rights table in access.py before a handler
# sees it (a no-op while sign-in is off)
app.add_middleware(signin.Guard, signin=rt.sign_in, router=app.router)
# CSP, nosniff, Referrer-Policy on every answer, the guard's refusals too
app.add_middleware(https.SecurityHeaders)
if rt.config.listen_hsts_max_age:
    # outermost, so the refusals the guard answers carry it too
    app.add_middleware(https.Hsts, max_age=rt.config.listen_hsts_max_age)
signin.add_routes(app, rt.sign_in)


@app.get("/api/health")
async def api_health() -> dict:
    """For monitoring from outside, without signing in: is it up, and
    which version. Nothing about the network."""
    return {"status": "ok", "version": __version__}


@app.get("/api/about")
async def api_about() -> dict:
    """What MoonLan is, which build, what it runs on and with which
    versions, and the IEEE register. The server itself — Python, the
    system, uptime, sign-in, HTTPS — only to an administrator, or to
    everybody while sign-in is off, when everybody is one."""
    on = await asyncio.to_thread(rt.sign_in.sign_in_on)
    who = signin.principal()
    body = {
        "version": __version__,
        "build": BUILD,
        "copyright": COPYRIGHT,
        "components": about.components(),
        "ieee": {
            "loaded": rt.vendors.loaded,
            "date": rt.vendors.oldest(),
            "command": oui.UPDATE_COMMAND,
        },
    }
    if not on or (who is not None and who.role == "admin"):
        body["server"] = about.server(STARTED_AT, on, rt.transport)
    return body


@app.get("/api/about/license")
async def api_about_license() -> Response:
    """The LICENSE file, as text: MIT asks for it to go with every copy"""
    text = await asyncio.to_thread(about.license_text)
    if text is None:
        return _refuse("no_license", 404)
    return Response(text, media_type="text/plain; charset=utf-8")


# ---------- Settings (v0.8.4) ----------

def _may_change_settings(on: bool) -> bool:
    """An administrator — or everybody while sign-in is off, when
    everybody is one. The console reads only."""
    who = signin.principal()
    return not on or (who is not None and who.role == "admin"
                      and not who.console)


async def _settings_answer() -> dict:
    on = await asyncio.to_thread(rt.sign_in.sign_in_on)
    editor = _may_change_settings(on)
    rows = await asyncio.to_thread(rt.db.settings)
    return {
        "settings": settings.describe(rt.config, rows, personal=editor),
        "sections": list(settings.SECTIONS),
        "alarm_types": list(rt.config.alarm_notify),
        "channels": notify.channel_state(rt.config.notifications),
        "can_edit": editor,
        "sign_in": on,
    }


def _settings_applied(changes: list[dict], how: str) -> None:
    """After a save or a reset: what holds a value of its own is told,
    and the log says what changed, by whom, in one line."""
    keys = {settings.setting_of(c["key"]) for c in changes}
    for key in keys & set(settings.INTERVALS):
        rt.interval_changed[key].set()
    if "thresholds.flap_window_minutes" in keys:
        rt.flap_tracker.window_seconds = rt.config.thresholds.flap_window_minutes * 60
    log.info("Settings %s by %s: %s", how, signin.actor() or "(sign-in off)",
             settings.log_line(changes))


@app.get("/api/settings")
async def api_settings() -> dict:
    """Every setting: its description, value, default, where it comes
    from and who changed it last; whether each channel is set up on the
    server (and nothing about how — no secret ever leaves config.yaml)."""
    return await _settings_answer()


@app.put("/api/settings")
async def api_settings_save(request: Request) -> JSONResponse:
    """{key: value, …}: all checked, then all written, or nothing.
    An unknown key is an error."""
    try:
        body = await request.json()
    except ValueError:
        return _refuse("bad_request", 400)
    changes, errors = await asyncio.to_thread(
        settings.save, rt.config, rt.db, body, signin.actor()
    )
    if errors:
        return JSONResponse({"errors": errors}, status_code=400)
    if changes:
        _settings_applied(changes, "changed")
    return JSONResponse({"changed": changes, **await _settings_answer()})


@app.delete("/api/settings/{key}")
async def api_settings_reset(key: str) -> JSONResponse:
    if key not in settings.BY_KEY:
        return _refuse("unknown_setting", 404)
    changes = await asyncio.to_thread(
        settings.reset, rt.config, rt.db, key, signin.actor()
    )
    if changes:
        _settings_applied(changes, "reset")
    return JSONResponse({"changed": changes, **await _settings_answer()})


@app.post("/api/settings/test/{channel}")
async def api_settings_test(channel: str) -> JSONResponse:
    """A test message to the shared recipients of one channel"""
    state = notify.channel_state(rt.config.notifications).get(channel)
    if state is None:
        return _refuse("unknown_channel", 404)
    if not state["ready"]:
        return _refuse("channel_off", 409, why=state["why"])
    recipients = rt.notifier.shared(channel)
    if not recipients:
        return _refuse("no_recipients", 409)
    text = notify.format_text(
        "test", "Settings", "info",
        f"a test message to the shared recipients, sent by "
        f"{signin.actor() or 'the page'}", False,
    )
    if rt.config.demo:
        log.info("NOTIFY (demo): %s (%s, %d recipient(s))", text, channel,
                 len(recipients))
        return JSONResponse({"sent": len(recipients), "failed": 0,
                             "demo": True})
    failed = await asyncio.to_thread(rt.notifier.deliver, channel, recipients,
                                     text)
    log.info("Test message via %s to the shared recipients, by %s: %d sent, "
             "%d failed", channel, signin.actor() or "(sign-in off)",
             len(recipients) - failed, failed)
    return JSONResponse({"sent": len(recipients) - failed, "failed": failed})


# ---------- one's own notifications (v0.8.4) ----------

class AddressBody(BaseModel):
    address: str = ""
    lang: str = "en"


class CodeBody(BaseModel):
    code: str = ""


class LangBody(BaseModel):
    lang: str = "en"


def _lang(value: str) -> str:
    return value if value in ("en", "ru") else "en"


async def _personal(call, *args) -> JSONResponse:
    """A call for the account of this session — never one named in the
    request — with its refusal as the page's code"""
    try:
        return JSONResponse(await asyncio.to_thread(call, *args))
    except subscriptions.Refused as refused:
        return _refuse(refused.error, refused.status, **refused.extra)


def _who() -> tuple[int, str]:
    who = signin.principal()
    return who.user_id, who.name


@app.get("/api/me/notifications")
async def api_my_notifications() -> JSONResponse:
    user_id, _ = _who()
    return await _personal(rt.personal.state, user_id)


@app.post("/api/me/notifications/email")
async def api_my_email(body: AddressBody) -> JSONResponse:
    """A code to the address, which is not anybody's until it comes back"""
    user_id, name = _who()
    return await _personal(rt.personal.send_code, user_id, name, body.address,
                           _lang(body.lang))


@app.post("/api/me/notifications/email/confirm")
async def api_my_email_confirm(body: CodeBody) -> JSONResponse:
    user_id, name = _who()
    return await _personal(rt.personal.confirm_code, user_id, name, body.code)


@app.post("/api/me/notifications/telegram")
async def api_my_telegram(body: LangBody) -> JSONResponse:
    """A link to the bot, good for ten minutes; the bot's updates are
    read while it waits"""
    user_id, name = _who()
    answer = await _personal(rt.personal.start_link, user_id, name,
                             _lang(body.lang))
    if answer.status_code == 200:
        rt.personal.ensure_polling()
    return answer


@app.get("/api/me/notifications/telegram")
async def api_my_telegram_state() -> JSONResponse:
    user_id, _ = _who()
    return await _personal(rt.personal.link_state, user_id)


@app.patch("/api/me/notifications/{channel}")
async def api_my_notifications_change(channel: str,
                                      request: Request) -> JSONResponse:
    """{enabled} and/or {alarm_types}: a list, or null — as the shared
    table"""
    try:
        body = await request.json()
    except ValueError:
        return _refuse("bad_request", 400)
    if not isinstance(body, dict):
        return _refuse("bad_request", 400)
    user_id, _ = _who()
    return await _personal(rt.personal.change, user_id, channel, body)


@app.post("/api/me/notifications/{channel}/test")
async def api_my_notifications_test(channel: str,
                                    body: LangBody) -> JSONResponse:
    user_id, name = _who()
    return await _personal(rt.personal.test, user_id, name, channel,
                           _lang(body.lang))


@app.delete("/api/me/notifications/{channel}")
async def api_my_notifications_remove(channel: str) -> JSONResponse:
    user_id, name = _who()
    return await _personal(rt.personal.remove, user_id, name, channel)


@app.delete("/api/users/{name}/notifications")
async def api_user_notifications_remove(name: str) -> JSONResponse:
    """Somebody's subscriptions, by an administrator: a person who left,
    whose account is not deleted yet"""
    row = await asyncio.to_thread(rt.sign_in.accounts.user, name)
    if row is None:
        return _refuse("unknown", 404, name=name)
    removed = await asyncio.to_thread(
        rt.personal.remove_all, row["id"], row["name"], signin.actor()
    )
    return JSONResponse({"status": "removed", "removed": removed})


@app.get("/api/topology")
async def api_topology() -> JSONResponse:
    topo = rt.state.as_dict()
    # Fresh DB data: ping is updated more often than SNMP polls happen
    db_hosts = await asyncio.to_thread(rt.db.hosts_by_mac)
    hosts = [dict(h) for h in topo["hosts"]]
    _merge_db_fields(hosts, db_hosts)
    topo["hosts"] = hosts
    unlocated = [dict(h) for h in topo.get("unlocated", [])]
    _merge_db_fields(unlocated, db_hosts)
    _mark_recent(unlocated, db_hosts, time.time())
    topo["unlocated"] = unlocated
    # a bridge node answers "is it up?" like any other device
    bridges = []
    for bridge in topo.get("bridges", []):
        bridge = dict(bridge)
        if oui.normalize(bridge["chassis_id"]):
            # its maker by the chassis MAC; a chassis id that is a name
            # or a local address says nothing about who made it
            bridge.update(rt.vendors.fields(bridge["chassis_id"]))
        row = db_hosts.get(bridge["chassis_id"], {})
        if row:
            bridge["ip"] = row.get("ip", "") or bridge.get("mgmt_ip", "")
            bridge["ping_up"] = bool(row.get("ping_up", 0))
            bridge["last_ping_ok"] = row.get("last_ping_ok", 0)
        bridges.append(bridge)
    topo["bridges"] = bridges
    # no config.yaml: the empty map says where it goes (v0.8.3)
    missing = _config_missing()
    if missing:
        topo["config_missing"] = missing
    topo["switches"] = [
        {
            **sw,
            **(rt.vendors.fields(sw["mac"]) if sw.get("mac") else {}),
            "ping_up": rt.switch_ping.get(sw["ip"], {}).get("ping_up", False),
            "last_ping_ok": rt.switch_ping.get(sw["ip"], {}).get("last_ping_ok", 0),
            # loop detection is polled at the counters cadence, not by
            # the scan the rest of this map comes from, so it is
            # attached per request rather than baked into the topology
            "loop": _loop_report(rt.switch_data.get(sw["ip"])),
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
    _attach_notes(topo, await asyncio.to_thread(rt.db.notes))
    return JSONResponse(topo)


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


def _stand_in(offline_id: str) -> str:
    """The switch without SNMP an offline group stands in for: the same
    switch and port"""
    return "pseudo:" + offline_id[len("offline:"):]


@app.get("/api/switch/{ip}/ports")
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


@app.get("/api/stp")
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


@app.get("/api/alarms")
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


@app.post("/api/alarms/{alarm_id}/clear")
async def api_clear_alarm(alarm_id: int):
    row = await rt.alarm_engine.manual_clear(alarm_id, user=signin.actor())
    if row is None:
        return JSONResponse({"error": "no such active alarm"}, status_code=404)
    return {"id": alarm_id, "cleared": True}


# What the journal's filters on the page choose from (v0.8.6), in one
# place. Every event the code writes belongs to one of these, or to
# "other" by being in none of them — tests/test_journal.py reads the
# code for every add_event and holds this to it.
JOURNAL_GROUPS: dict[str, tuple[str, ...]] = {
    "alarms": ("alarm_raised", "alarm_cleared"),
    "ping": ("host_down", "host_up"),
    # an address taken off a record ARP no longer confirms (v0.8.7):
    # two in three device events, and they buried the new devices
    "addresses": ("ip_released",),
    "devices": (
        "new_mac", "hosts_purged", "copies_removed", "monitor_on",
        "monitor_off",
    ),
    "map": (
        "layout_pinned", "layout_released", "layout_saved",
        "layout_cleared", "note_changed", "note_moved", "notes_imported",
        "link_dropped",
    ),
    # sign-ins, accounts, keys, one's own notifications: ACCOUNT_EVENTS
    # of the page
    "users": (
        "login", "logout", "account_locked", "user_added", "user_role",
        "user_password", "user_disabled", "user_enabled",
        "user_totp_reset", "user_totp_enabled", "user_unlocked",
        "user_sessions_closed", "user_deleted", "user_passkey_added",
        "user_passkey_removed", "user_passkeys_reset",
        "passkey_clone_suspected", "subscription_added",
        "subscription_removed", "user_notifications_removed",
    ),
    "settings": ("settings_changed", "settings_moved"),
}
JOURNAL_OTHER = "other"
EVENT_GROUP: dict[str, str] = {
    event: group for group, events in JOURNAL_GROUPS.items()
    for event in events
}


@app.get("/api/journal")
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


class HostPatch(BaseModel):
    monitored: bool


@app.patch("/api/host/{mac}")
async def api_patch_host(mac: str, body: HostPatch):
    mac = mac.lower()
    ok = await asyncio.to_thread(rt.db.set_monitored, mac, body.monitored)
    if not ok:
        return JSONResponse({"error": "unknown host"}, status_code=404)
    # who asked for alarms about this device, or stopped them
    await asyncio.to_thread(
        rt.db.add_event, time.time(),
        "monitor_on" if body.monitored else "monitor_off", mac, "",
        signin.actor(),
    )
    return {"mac": mac, "monitored": body.monitored}


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


def _node_directory() -> dict[str, dict]:
    """Every node on the map with what the node menu needs to know.

    The page names a node by its id and nothing else. The address a
    ping goes to, the MAC a link is filled with, all come from here —
    MoonLan's own topology and database — and never from the request.
    `switch` is the address of the switch the node hangs off (a
    switch's own, for a switch), `port` the port there.
    """
    topo = rt.state.as_dict()
    db_hosts = rt.db.hosts_by_mac()
    nodes: dict[str, dict] = {}

    def entry(kind, ip="", mac="", name="", switch="", port="", row=None):
        row = row or {}
        return {
            "kind": kind, "ip": ip or "", "mac": mac or "",
            "name": name or "", "switch": switch or "", "port": port or "",
            # what the continuous ping knows, shown beside a one-off
            # ping as the second source it is
            "ping_up": bool(row["ping_up"]) if "ping_up" in row else None,
            "last_ping_ok": row.get("last_ping_ok", 0) or 0,
        }

    for sw in topo["switches"]:
        nodes["sw:" + sw["ip"]] = entry(
            "switch", sw["ip"], sw.get("mac"), sw.get("name"), sw["ip"],
            row=rt.switch_ping.get(sw["ip"]),
        )
    for host in topo["hosts"]:
        if host.get("merged_into"):
            continue  # drawn as its bridge node
        row = db_hosts.get(host["mac"], {})
        nodes["host:" + host["mac"]] = entry(
            "host",
            row.get("ip") or host.get("router_ip"),
            host["mac"],
            row.get("name") or (host.get("lldp") or {}).get("sys_name"),
            host.get("switch"), host.get("port"), row,
        )
    for bridge in topo.get("bridges") or ():
        row = db_hosts.get(bridge.get("chassis_id", ""), {})
        nodes[bridge["id"]] = entry(
            "switch",
            bridge.get("router_ip") or row.get("ip") or bridge.get("mgmt_ip"),
            bridge.get("chassis_id"), bridge.get("name"),
            bridge.get("switch"), bridge.get("port"), row,
        )
    for key in ("pseudo_switches", "trunk_groups", "offline_groups",
                "external_networks"):
        for group in topo.get(key) or ():
            nodes[group["id"]] = entry(
                "group", switch=group.get("switch"), port=group.get("port"),
            )
    return nodes


def _tool_state() -> dict:
    """What the menu can run, for greying out what it cannot."""
    trace = rt.tools.get("traceroute")
    return {
        "ping": bool(rt.tools.get("ping")),
        "traceroute": trace[0] if trace else None,
        "simulated": rt.config.demo,
        "max_targets": rt.config.context_menu.max_targets,
    }


def _menu_links(node_id: str, facts: dict) -> tuple[list[dict], list[dict]]:
    """(built-in links, the operator's links) for one node, filled in.

    A link that lacks a value is still listed, with the field it lacks:
    the page shows it greyed out with the reason, the way "no answer"
    is never shown as zero.
    """
    kind = facts["kind"]
    builtin: list[dict] = []
    if kind in ("switch", "host"):
        if node_id.startswith("sw:"):
            scheme = rt.config.web_scheme(facts["ip"])
        elif kind == "switch":
            scheme = rt.config.context_menu.web_scheme
        else:
            scheme = "http"
        for key, template in (("web", scheme + "://{ip}"),
                              ("ssh", "ssh://{ip}")):
            url, missing = menu.expand(template, facts)
            builtin.append({"key": key, "url": url, "missing": missing})
    custom: list[dict] = []
    for link in rt.config.context_menu.links:
        if not link.applies(kind):
            continue
        url, missing = menu.expand(link.url, facts)
        custom.append({"label": link.label, "url": url, "missing": missing})
    return builtin, custom


@app.get("/api/node-menu")
async def api_node_menu(id: str = Query(...)):
    """What the right-click menu of one node can offer."""
    nodes = await asyncio.to_thread(_node_directory)
    facts = nodes.get(id)
    if facts is None:
        return JSONResponse(
            {"error": "unknown_node", "node": id}, status_code=404
        )
    builtin, custom = _menu_links(id, facts)
    return {
        "node": {"id": id, **facts},
        "links": builtin,
        "custom": custom,
        "tools": _tool_state(),
    }


class ActionBody(BaseModel):
    action: str
    nodes: list[str]


def _refuse(error: str, status: int, **extra) -> JSONResponse:
    return JSONResponse({"error": error, **extra}, status_code=status)


@app.get("/api/actions")
async def api_actions_state() -> dict:
    return {**_tool_state(), "running": rt.jobs.running(),
            "max_running": rt.config.context_menu.max_running}


@app.post("/api/actions")
async def api_start_action(body: ActionBody, request: Request):
    """Starts ping or traceroute from this machine; returns the job.

    Node ids in, never addresses: an id MoonLan does not know is
    refused, and so is an address sent in place of one. Whatever the
    tool is run against was found by MoonLan itself.
    """
    if body.action not in probes.ACTIONS:
        return _refuse("unknown_action", 400, action=body.action)
    ids = list(dict.fromkeys(body.nodes))
    if not ids:
        return _refuse("no_targets", 400)
    limit = rt.config.context_menu.max_targets
    if len(ids) > limit:
        # refused, not trimmed: a silently shortened list reads as
        # "these are all of them"
        return _refuse("too_many_targets", 400, limit=limit, count=len(ids))
    if body.action == "traceroute" and len(ids) > 1:
        return _refuse("traceroute_one", 400)
    if body.action == "ping" and not rt.tools.get("ping"):
        return _refuse("tool_missing", 503, tool="ping")
    if body.action == "traceroute" and not rt.tools.get("traceroute"):
        return _refuse("tool_missing", 503, tool="traceroute")
    nodes = await asyncio.to_thread(_node_directory)
    unknown = [node_id for node_id in ids if node_id not in nodes]
    if unknown:
        return _refuse(
            "unknown_nodes", 404, nodes=unknown[:10],
            # the one mistake worth naming: an address is not a node id
            addresses=[n for n in unknown if probes.checked_address(n)],
        )
    targets = [
        probes.Target(
            node=node_id, kind=nodes[node_id]["kind"],
            name=nodes[node_id]["name"] or nodes[node_id]["ip"] or node_id,
            ip=nodes[node_id]["ip"],
            monitor={
                "ping_up": nodes[node_id]["ping_up"],
                "last_ping_ok": nodes[node_id]["last_ping_ok"],
            },
        )
        for node_id in ids
    ]
    try:
        job = rt.jobs.start(
            body.action, targets, rt.config.context_menu.max_running,
            owner=signin.actor(),
        )
    except probes.Busy:
        return _refuse("busy", 429, limit=rt.config.context_menu.max_running)
    client = request.client.host if request.client else "?"
    shown = ", ".join(
        f"{t.node} ({t.ip or 'no address'})" for t in targets[:5]
    ) + (f" and {len(targets) - 5} more" if len(targets) > 5 else "")
    log.info(
        "Menu: %s of %s requested from %s%s (job %s)",
        body.action, shown, client,
        f" by {signin.actor()}" if signin.actor() else "", job.id,
    )
    return job.as_dict()


@app.get("/api/actions/{job_id}")
async def api_action_state(job_id: str):
    """A ping's result is for whoever started it, and for an
    administrator. Somebody else's job answers as if it did not exist."""
    job = rt.jobs.get(job_id)
    who = signin.principal()
    if job is None or (
        who is not None and who.role != "admin" and job.owner != who.name
    ):
        return _refuse("unknown_job", 404)
    return job.as_dict()


@app.get("/api/layout")
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


@app.patch("/api/layout")
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


@app.patch("/api/layout/{node_id:path}")
async def api_patch_node_position(node_id: str, body: NodePosition) -> dict:
    """One node, moved by hand — pinned unless told otherwise."""
    await _store_hand_placed({node_id: body})
    pinned = True if body.pinned is None else body.pinned
    return {"node_id": node_id, "x": body.x, "y": body.y, "pinned": pinned}


@app.delete("/api/layout/{node_id:path}")
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


@app.delete("/api/layout")
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


# ---------- names and notes on nodes (v0.8.0) ----------


class NoteBody(BaseModel):
    label: str = ""
    note: str = ""


class NoteMove(BaseModel):
    to: str
    # the target has a note of its own: replace it (the page asks first)
    replace: bool = False


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


@app.get("/api/notes")
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


@app.post("/api/notes/import")
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


@app.get("/api/notes/export")
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


@app.put("/api/notes/{node_id:path}")
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


@app.delete("/api/notes/{node_id:path}")
async def api_delete_note(node_id: str):
    try:
        key = await _existing_note_key(node_id)
    except notes.NoteError as exc:
        return _note_refused(exc)
    if not await asyncio.to_thread(rt.db.delete_note, key):
        return _refuse("no_note", 404, text="this node has no note")
    await _journal_note("note_changed", key, {"deleted": True})
    return {"node_id": key, "deleted": True}


@app.post("/api/notes/{node_id:path}/move")
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


async def _scan_once() -> None:
    """A manual scan, recording a failure the same way the loop does."""
    try:
        await run_scan()
    except Exception as exc:
        rt.state.scan_failed(exc)
        log.exception("Network scan failed")


@app.post("/api/scan")
async def api_scan() -> dict:
    if signin.actor():
        log.info("Network scan requested by %s", signin.actor())
    asyncio.create_task(_scan_once())
    return {"status": "started"}


@app.get("/api/search")
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


@app.get("/api/skipped-oids")
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


@app.get("/api/polling")
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


@app.get("/api/status")
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


# The page's own files, addressed in index.html by what is in them
VERSIONED_ASSETS = ("app.js", "i18n.js", "style.css")


def _index_html() -> str:
    """index.html with each of the page's files named by its content.

    `no-cache` below only helps a browser that has been told it: a
    copy cached before this header existed carries no such instruction
    and is still "fresh" by the browser's own reckoning. After the
    upgrade that introduced the header, a page went on running with a
    new app.js and an old i18n.js — and showed translation keys where
    the journal should have said "Placed by hand". A file whose content
    changes gets a new address, and no cache has a copy of that.
    """
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    for name in VERSIONED_ASSETS:
        digest = hashlib.sha1((WEB_DIR / name).read_bytes()).hexdigest()[:12]
        html = html.replace(f'"{name}"', f'"{name}?v={digest}"')
    return html


@app.get("/", include_in_schema=False)
@app.get("/index.html", include_in_schema=False)
async def index_page() -> HTMLResponse:
    return HTMLResponse(
        await asyncio.to_thread(_index_html),
        headers={"Cache-Control": "no-cache"},
    )


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
