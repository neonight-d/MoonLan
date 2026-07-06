"""Ping monitoring via the system ping utility.

No raw sockets are used, so root is not required. Concurrency is
limited by a semaphore to avoid spawning hundreds of processes at once.
"""

from __future__ import annotations

import asyncio
from typing import Iterable

MAX_CONCURRENT = 30

_semaphore = asyncio.Semaphore(MAX_CONCURRENT)


async def ping(ip: str) -> bool:
    """One ICMP request; True if the host replied within a second."""
    async with _semaphore:
        proc = await asyncio.create_subprocess_exec(
            "ping", "-c", "1", "-W", "1", ip,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return await proc.wait() == 0


async def ping_many(ips: Iterable[str]) -> dict[str, bool]:
    """Pings all addresses in parallel; IP -> whether it replied."""
    unique = list(dict.fromkeys(ips))
    results = await asyncio.gather(*(ping(ip) for ip in unique))
    return dict(zip(unique, results))
