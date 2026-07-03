"""Веб-сервис MoonLan: REST API и статический веб-интерфейс."""

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
from .topology import TopologyState, build_topology

log = logging.getLogger("moonlan")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

state = TopologyState()
config: Config = load_config()
# В демо-режиме БД держим в памяти, чтобы не засорять реальную
db = Database(":memory:" if config.demo else config.db_path)

# Состояние ping коммутаторов (они не в таблице hosts): ip -> {ping_up, last_ping_ok}
switch_ping: dict[str, dict] = {}


async def run_scan() -> None:
    """Один цикл опроса всех коммутаторов и пересборки топологии."""
    if state.scanning:
        return
    state.scanning = True
    try:
        collector: SnmpCollector | None = None
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
        switches, links, hosts = build_topology(collected)
        new_macs = await asyncio.to_thread(db.upsert_hosts, hosts)
        if new_macs:
            log.info("Новых MAC: %d", len(new_macs))
        if collector is not None and config.routers:
            await update_ips(collector)
        if not config.demo:
            await resolve_names()
        state.update(switches, links, hosts)
        log.info(
            "Опрос завершён: коммутаторов %d, связей %d, хостов %d",
            len(switches), len(links), len(hosts),
        )
    finally:
        state.scanning = False


async def update_ips(collector: SnmpCollector) -> None:
    """Опрашивает ARP-таблицы маршрутизаторов и проставляет хостам IP."""
    tables = await asyncio.gather(
        *(collector.collect_arp(ip) for ip in config.routers)
    )
    merged: dict[str, str] = {}
    for table in tables:  # при конфликте побеждает последняя запись
        merged.update(table)
    if merged:
        await asyncio.to_thread(db.set_ips, merged)


# mac -> unix time последней попытки обратного DNS
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
    """Обратный DNS для хостов с IP без имени, не чаще раза в час на хост."""
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
        log.info("Обратный DNS: имён получено %d из %d", resolved, len(candidates))


async def periodic_scan() -> None:
    while True:
        try:
            await run_scan()
        except Exception:
            log.exception("Ошибка при опросе сети")
        interval = config.scan_interval_minutes
        await asyncio.sleep(interval * 60 if interval > 0 else 3600)


async def run_ping() -> None:
    """Один цикл ping: все хосты с IP и все коммутаторы."""
    now = time.time()
    if config.demo:
        # Реальный ping не выполняем: обновляем время ответа живых хостов
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
            log.exception("Ошибка ping-мониторинга")
        await asyncio.sleep(max(config.ping_interval_seconds, 1))


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if config.demo:
        log.info("MoonLan запущен в ДЕМО-режиме (виртуальная сеть)")
    elif not config.switches:
        log.warning(
            "В config.yaml не указано ни одного коммутатора. "
            "Добавьте адреса в раздел switches или запустите с MOONLAN_DEMO=1."
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
    return JSONResponse(state.as_dict())


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


# Статика веб-интерфейса — в самом конце, чтобы не перекрывать /api/*
app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
