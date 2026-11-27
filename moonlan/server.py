"""MoonLan web service: REST API and the static web UI."""

from __future__ import annotations

import asyncio
import inspect
import logging
import sys
import time
import types
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from . import (
    __version__, counters, demo, https, notes, oui, probes, routes, settings,
    signin, subscriptions,
)
from .alarms import AlarmEngine
from .config import Config, load_config
from .db import Database
from .derived import (
    _loop_report,
    _rates_hide_age,
    _round,
    _sum_known,
    configured_switches,
)
from .housekeeping import (
    CERT_CHECK_SECONDS,
    CLEANUP_SECONDS,
    RESOURCE_LOG_SECONDS,
    check_certificate_end,
    drop_unpinned_positions,
    hide_confirmed_copies,
    purge_copies,
    purge_invalid_macs,
    purge_journal,
    purge_old_hosts,
    resource_usage,
)
from .mapping.hosts import (
    _mark_recent,
    _merge_db_fields,
)
from .mapping.upkeep import (
    _stand_in,
    purge_layout,
)
from .notify import Notifier
from .poller import restore_readings, run_counters, run_ping, run_scan
# the modules of routes, included below as routes.<module>; notes and
# settings are moonlan's own modules here
from .routes import actions, alarms, layout, system, topology  # noqa: F401
from .routes import notes as notes_routes, settings as settings_routes  # noqa: F401
from .routes.actions import (  # noqa: F401 (moved)
    ActionBody,
    _node_directory,
    api_action_state,
    api_node_menu,
    api_start_action,
)
from .routes.alarms import api_journal  # noqa: F401 (moved)
from .routes.layout import (  # noqa: F401 (moved)
    LAYOUT_EVENT_IDS,
    LayoutBody,
    NodePosition,
    Offset,
    api_clear_layout,
    api_delete_node_position,
    api_layout,
    api_patch_positions,
    api_put_layout,
)
from .routes.notes import _mac_directory  # noqa: F401 (moved)
from .routes.system import (  # noqa: F401 (moved)
    VERSIONED_ASSETS,
    WEB_DIR,
    RevalidatedStaticFiles,
    api_about,
)
from .routes.topology import (  # noqa: F401 (moved)
    _lldp_dict,
    api_skipped_oids,
    api_status,
    api_switch_ports,
)
from .runtime import FIELDS, rt
from .startup import (
    _config_missing,
    _log_certificate,
    _log_config,
    configure_logging,
    start_redirect_listener,
)
from .snmp_collector import (
    STATE_NEVER,
    SnmpCollector,
    SwitchData,
)
from .topology import (
    FdbStability,
    TopologyState,
)

log = logging.getLogger("moonlan")


# The state of the process is in rt (runtime.py) since v0.8.13, and what
# server.py was taken apart into lives in its own modules. A name this
# module does not have fails loudly: server.db = fake or
# server.get_collector = stub would otherwise set an attribute nothing
# reads, and the test that did it would pass checking nothing — or poll
# switches for real. Reading is refused by __getattr__ (PEP 562), writing
# and deleting by the class of the module; the refusal says where the
# name lives. A write is never passed on, since patch.object deletes the
# attribute on the way out and would not put the value back. Assignments
# at the top of this file write into its namespace directly, past both.
def _moved_to_rt(name: str) -> AttributeError:
    return AttributeError(
        f"server.{name} moved to the runtime: use server.rt.{name}"
    )


def _moved(name: str) -> AttributeError | None:
    """Where a function or class of that name lives: the loaded MoonLan
    module that defines it. Looked up, not listed."""
    for module_name, module in sorted(sys.modules.items()):
        if module is None or (module_name != "moonlan"
                              and not module_name.startswith("moonlan.")):
            continue
        found = vars(module).get(name)
        if ((inspect.isfunction(found) or inspect.isclass(found))
                and found.__module__ == module_name):
            return AttributeError(
                f"server.{name} moved to {module_name}: "
                f"use {module_name}.{name}"
            )
    return None


def __getattr__(name: str):
    if name in FIELDS:
        raise _moved_to_rt(name)
    raise _moved(name) or AttributeError(
        f"module {__name__!r} has no attribute {name!r}"
    )


class _StrictModule(types.ModuleType):
    def __setattr__(self, name: str, value) -> None:
        self._refuse_unless_here(name)
        super().__setattr__(name, value)

    def __delattr__(self, name: str) -> None:
        self._refuse_unless_here(name)
        super().__delattr__(name)

    def _refuse_unless_here(self, name: str) -> None:
        if name in FIELDS:
            raise _moved_to_rt(name)
        if name not in vars(self):
            raise _moved(name) or AttributeError(
                f"server.py has no {name}: patch it in the module it lives in"
            )


sys.modules[__name__].__class__ = _StrictModule


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


# One SnmpEngine per process: a new engine per cycle leaks sockets and
# MIB state (OSError 24, MibNotFoundError, growing RSS). Recreate only
# if the SNMP config ever changes at runtime — it currently cannot.
rt._collector: SnmpCollector | None = None


# One poll per switch at a time. A DGS-1210 answers one request at a
# time, and the scan and the counters loop can otherwise land on the
# same agent together. Different switches stay parallel — this is a
# lock per IP, not a global one.
rt._host_locks: dict[str, asyncio.Lock] = {}


# routers of `routers:` whose empty identity has been reported, and
# the MACs of the last identity of each — a line when it changes, not
# one per scan
rt._identity_reported: dict[str, frozenset[str]] = {}


# mac -> unix time of the last reverse DNS attempt
rt._dns_attempts: dict[str, float] = {}


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


async def periodic_ping() -> None:
    while True:
        try:
            await run_ping()
        except Exception:
            log.exception("Ping monitoring failed")
        await sleep_interval(
            "ping_interval_seconds", lambda: max(rt.config.ping_interval_seconds, 1)
        )


# What each switch's loop detection last said, so the same line is not
# repeated every minute for a network where nothing is happening
rt._loop_logged: dict[str, str] = {}


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


async def periodic_certificate_check() -> None:
    while True:
        await asyncio.sleep(CERT_CHECK_SECONDS)
        try:
            await check_certificate_end()
        except Exception:
            log.exception("Certificate check failed")


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
# The routes by subject (routes/), in this order, each its own entry
# in app.routes (routes.include says why); the static page stays last
routes.include(app, routes.system.router)
routes.include(app, routes.settings.router)
routes.include(app, routes.notes.router)
routes.include(app, routes.layout.router)
routes.include(app, routes.actions.router)
routes.include(app, routes.alarms.router)
routes.include(app, routes.topology.router)


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


# The static web UI comes last so it does not shadow /api/*
app.mount(
    "/", RevalidatedStaticFiles(directory=str(WEB_DIR), html=True),
    name="web",
)
