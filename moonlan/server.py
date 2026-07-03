"""Веб-сервис MoonLan: REST API и статический веб-интерфейс."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, demo
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


async def run_scan() -> None:
    """Один цикл опроса всех коммутаторов и пересборки топологии."""
    if state.scanning:
        return
    state.scanning = True
    try:
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
        state.update(switches, links, hosts)
        log.info(
            "Опрос завершён: коммутаторов %d, связей %d, хостов %d",
            len(switches), len(links), len(hosts),
        )
    finally:
        state.scanning = False


async def periodic_scan() -> None:
    while True:
        try:
            await run_scan()
        except Exception:
            log.exception("Ошибка при опросе сети")
        interval = config.scan_interval_minutes
        await asyncio.sleep(interval * 60 if interval > 0 else 3600)


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
    task = asyncio.create_task(periodic_scan())
    yield
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
