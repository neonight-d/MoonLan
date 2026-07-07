"""MoonLan web service: REST API and the static web UI."""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, counters, demo, pinger
from .alarms import AlarmEngine
from .config import Config, load_config
from .db import Database
from .notify import Notifier
from .snmp_collector import SnmpCollector, SwitchData
from .topology import FdbStability, TopologyState, build_topology, port_name

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
notifier = Notifier(config, demo=config.demo)
alarm_engine = AlarmEngine(db, notifier, config.thresholds)
demo_counters = demo.DemoCounters() if config.demo else None

# The first scan is the initial inventory: every MAC is "new" there,
# alerting on all of them would be pure noise
first_scan_done = False


async def run_scan() -> None:
    """One cycle of polling all switches and rebuilding the topology."""
    global first_scan_done
    if state.scanning:
        return
    state.scanning = True
    try:
        arp: dict[str, str] = {}
        if config.demo:
            collected = demo.demo_network()
        else:
            collector = SnmpCollector(
                community=config.snmp.community,
                timeout=config.snmp.timeout,
                retries=config.snmp.retries,
            )
            collected = list(
                await asyncio.gather(*(collector.collect(ip) for ip in config.switches))
            )
            if config.routers:
                arp = await collect_arp(collector)
            # The MAC of the switch's management IP is also its MAC:
            # neighbors see the switch under it in their FDB tables
            ip_to_mac = {ip: mac for mac, ip in arp.items()}
            for sw in collected:
                mac = ip_to_mac.get(sw.ip)
                if mac:
                    sw.own_macs.add(mac)
        for sw in collected:
            switch_data[sw.ip] = sw
        switches, links, hosts, pseudo_switches, vlan_names = build_topology(
            collected,
            config.unmanaged_threshold,
            fdb_stability=None if config.demo else fdb_stability,
        )
        new_macs = await asyncio.to_thread(db.upsert_hosts, hosts)
        if new_macs:
            log.info("New MACs: %d", len(new_macs))
        if config.demo:
            await asyncio.to_thread(demo.enrich_db, db, hosts)
        else:
            if arp:
                await asyncio.to_thread(db.set_ips, arp)
            await resolve_names()
        _merge_db_fields(hosts, await asyncio.to_thread(db.hosts_by_mac))
        state.update(switches, links, hosts, pseudo_switches, vlan_names)
        if config.demo:
            await run_ping()  # set the switches' ping state right away

        await alarm_engine.on_scan(
            {sw.ip: sw.reachable for sw in collected},
            {sw.ip: sw.sys_name or sw.ip for sw in collected},
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
    finally:
        state.scanning = False


def _merge_db_fields(hosts: list[dict], db_hosts: dict[str, dict]) -> None:
    """Enriches topology hosts with DB fields (IP, name, ping state)."""
    for h in hosts:
        row = db_hosts.get(h["mac"], {})
        h["ip"] = row.get("ip", "")
        h["name"] = row.get("name", "")
        h["ping_up"] = bool(row.get("ping_up", 0))
        h["last_ping_ok"] = row.get("last_ping_ok", 0)
        h["first_seen"] = row.get("first_seen", 0)


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
        except Exception:
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
        ips = [ip for _, ip in targets] + list(config.switches)
        if not ips:
            return
        results = await pinger.ping_many(ips)
        results_by_mac = {mac: results[ip] for mac, ip in targets}
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
            "errors_per_min": r.errors_per_min,
            "discards_per_min": r.discards_per_min,
        })
    for aggregate, members in groups.items():
        member_rates = [rates[m] for m in members if m in rates]
        if not member_rates:
            continue
        metrics.append({
            "port": port_name(sw, aggregate),
            "speed_mbps": sum(
                sw.ports[m].speed_mbps for m in members if m in sw.ports
            ),
            "in_mbps": sum(r.in_mbps for r in member_rates),
            "out_mbps": sum(r.out_mbps for r in member_rates),
            # member errors are already alarmed individually
            "errors_per_min": 0.0,
            "discards_per_min": 0.0,
        })
    return metrics


async def run_counters() -> None:
    """One light counters poll of every switch (no topology rebuild)."""
    if not switch_data:
        return  # port lists are unknown until the first scan
    if config.demo:
        samples_by_ip = demo_counters.sample(list(switch_data.values()))
    else:
        collector = SnmpCollector(
            community=config.snmp.community,
            timeout=config.snmp.timeout,
            retries=config.snmp.retries,
        )
        ips = list(config.switches)
        collected = await asyncio.gather(
            *(counters.collect_samples(collector, ip) for ip in ips)
        )
        samples_by_ip = dict(zip(ips, collected))
    for ip, samples in samples_by_ip.items():
        if not samples:
            continue
        rates = counter_store.update(ip, samples)
        sw = switch_data.get(ip)
        if sw is not None and rates:
            await alarm_engine.on_counters(ip, _port_metrics(sw, rates))


async def periodic_counters() -> None:
    while True:
        try:
            await run_counters()
        except Exception:
            log.exception("Counter polling failed")
        await asyncio.sleep(max(config.counters_interval_seconds, 5))


def _rates_max_age() -> float:
    return max(config.counters_interval_seconds, 5) * 3


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
        rates = counter_store.current(link[side], max_age=_rates_max_age())
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
        in_mbps = sum(r.in_mbps for r in found)
        out_mbps = sum(r.out_mbps for r in found)
        if flip:  # B's in is A's out and vice versa
            in_mbps, out_mbps = out_mbps, in_mbps
        return {"in_mbps": round(in_mbps, 1), "out_mbps": round(out_mbps, 1)}
    return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if config.demo:
        log.info("MoonLan started in DEMO mode (virtual network)")
    elif not config.switches:
        log.warning(
            "No switches are configured in config.yaml. Add addresses to "
            "the switches section or start with MOONLAN_DEMO=1."
        )
    await alarm_engine.load()
    tasks = [
        asyncio.create_task(periodic_scan()),
        asyncio.create_task(periodic_ping()),
        asyncio.create_task(periodic_counters()),
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
    topo["switches"] = [
        {
            **sw,
            "ping_up": switch_ping.get(sw["ip"], {}).get("ping_up", False),
            "last_ping_ok": switch_ping.get(sw["ip"], {}).get("last_ping_ok", 0),
        }
        for sw in topo["switches"]
    ]
    # Live trunk load on the edges; recomputed on every request so the
    # periodic UI refresh picks it up without a topology rebuild
    links = []
    for link in topo["links"]:
        link = dict(link)
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
    rates = counter_store.current(ip, max_age=_rates_max_age())
    host_counts: Counter = Counter()
    for h in state.as_dict()["hosts"]:
        if h["switch"] == ip:
            host_counts[h["port"]] += 1
    member_of: dict[int, str] = {}
    for aggregate, members in _lag_groups(sw).items():
        for m in members:
            member_of[m] = port_name(sw, aggregate)
    ports = []
    for p in sw.ports.values():
        r = rates.get(p.if_index)
        name = p.name or str(p.if_index)
        ports.append({
            "if_index": p.if_index,
            "name": name,
            "oper_up": p.oper_up,
            "is_physical": p.is_physical,
            "speed_mbps": p.speed_mbps,
            "pvid": sw.port_pvid.get(p.if_index, 0),
            "lag": member_of.get(p.if_index, ""),
            "in_mbps": round(r.in_mbps, 1) if r else None,
            "out_mbps": round(r.out_mbps, 1) if r else None,
            "errors_per_min": round(r.errors_per_min, 1) if r else None,
            "discards_per_min": round(r.discards_per_min, 1) if r else None,
            "hosts": host_counts.get(name, 0),
        })
    # active ports first, then by port number
    ports.sort(key=lambda p: (not p["oper_up"], abs(p["if_index"])))
    return {"switch": ip, "name": sw.sys_name or ip, "ports": ports}


def _alarm_display(row: dict, hosts: dict[str, dict], sw_names: dict[str, str]) -> str:
    """Human label of the alarm subject (device name when known)."""
    subject = row["subject"]
    if row["type"] in ("host_down", "new_mac"):
        h = hosts.get(subject, {})
        return h.get("name") or h.get("ip") or subject
    if row["type"] == "switch_down":
        return sw_names.get(subject, subject)
    ip, sep, port = subject.partition(":")
    if sep and ip in sw_names:
        return f"{sw_names[ip]} · {port}"
    return subject


@app.get("/api/alarms")
async def api_alarms(
    active: int = Query(default=1, ge=0, le=1),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict:
    rows = await asyncio.to_thread(db.alarms, bool(active), limit)
    db_hosts = await asyncio.to_thread(db.hosts_by_mac)
    sw_names = {sw["ip"]: sw["name"] for sw in state.as_dict()["switches"]}
    for row in rows:
        row["display"] = _alarm_display(row, db_hosts, sw_names)
    return {"alarms": rows}


@app.get("/api/journal")
async def api_journal(limit: int = Query(default=100, ge=1, le=1000)) -> dict:
    return {"events": await asyncio.to_thread(db.journal, limit)}


@app.post("/api/scan")
async def api_scan() -> dict:
    asyncio.create_task(run_scan())
    return {"status": "started"}


@app.get("/api/search")
async def api_search(q: str = Query(default="")) -> dict:
    return {"query": q, "results": state.search(q)}


@app.get("/api/status")
async def api_status() -> dict:
    return {
        "version": __version__,
        "demo": config.demo,
        "switch_count": len(state.switches),
        "host_count": len(state.hosts),
        "last_scan": state.last_scan,
        "scanning": state.scanning,
        "uptime_hint": time.time(),
    }


# The static web UI comes last so it does not shadow /api/*
app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
