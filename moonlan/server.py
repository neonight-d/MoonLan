"""MoonLan web service: REST API and the static web UI."""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, demo, pinger
from .config import Config, load_config
from .db import Database
from .snmp_collector import SnmpCollector
from .topology import FdbStability, TopologyState, build_topology

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


async def run_scan() -> None:
    """One cycle of polling all switches and rebuilding the topology."""
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
        # No real pings: just refresh the reply time of live hosts
        await asyncio.to_thread(db.touch_ping_ok, now)
        for sw in state.as_dict()["switches"]:
            switch_ping[sw["ip"]] = {"ping_up": True, "last_ping_ok": now}
        return

    targets = await asyncio.to_thread(db.hosts_with_ip)
    ips = [ip for _, ip in targets] + list(config.switches)
    if not ips:
        return
    results = await pinger.ping_many(ips)
    await asyncio.to_thread(
        db.update_ping, {mac: results[ip] for mac, ip in targets}, now
    )
    for ip in config.switches:
        prev = switch_ping.get(ip, {})
        up = results.get(ip, False)
        switch_ping[ip] = {
            "ping_up": up,
            "last_ping_ok": now if up else prev.get("last_ping_ok", 0),
        }


async def periodic_ping() -> None:
    while True:
        try:
            await run_ping()
        except Exception:
            log.exception("Ping monitoring failed")
        await asyncio.sleep(max(config.ping_interval_seconds, 1))


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
    tasks = [
        asyncio.create_task(periodic_scan()),
        asyncio.create_task(periodic_ping()),
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
    return JSONResponse(topo)


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
