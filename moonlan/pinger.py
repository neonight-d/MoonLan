"""Ping-мониторинг через системную утилиту ping.

Raw-сокеты не используются, поэтому root не нужен. Параллельность
ограничена семафором, чтобы не порождать сотни процессов разом.
"""

from __future__ import annotations

import asyncio
from typing import Iterable

MAX_CONCURRENT = 30

_semaphore = asyncio.Semaphore(MAX_CONCURRENT)


async def ping(ip: str) -> bool:
    """Один ICMP-запрос; True, если хост ответил в течение секунды."""
    async with _semaphore:
        proc = await asyncio.create_subprocess_exec(
            "ping", "-c", "1", "-W", "1", ip,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return await proc.wait() == 0


async def ping_many(ips: Iterable[str]) -> dict[str, bool]:
    """Пингует все адреса параллельно; IP -> отвечает ли."""
    unique = list(dict.fromkeys(ips))
    results = await asyncio.gather(*(ping(ip) for ip in unique))
    return dict(zip(unique, results))
