"""The life of the service: its start, its loops, its end (v0.8.14).

lifespan — what the start does before the first request, in its order,
and what the end undoes — and the six loops it starts: the scan, ping,
the counters, the leak watch, the certificate and the daily cleanup,
each sleeping its interval, cut short when Settings change it. Moved
out of server.py word for word, the fourth stage of taking it apart
(docs/ARCHITECTURE.md); the state is read as rt.X when they run.
Nothing here imports moonlan.server or a module of routes.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import demo
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
from .mapping.upkeep import purge_layout
from .poller import restore_readings, run_counters, run_ping, run_scan
from .runtime import rt
from .startup import (
    _log_certificate,
    _log_config,
    configure_logging,
    start_redirect_listener,
)

log = logging.getLogger("moonlan")


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
