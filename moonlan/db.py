"""Хранилище SQLite: хосты и журнал событий.

Стандартный sqlite3 без ORM. Методы синхронные; из асинхронного кода
их следует вызывать через asyncio.to_thread. Одно соединение делится
между потоками (check_same_thread=False), доступ сериализуется Lock'ом.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path("moonlan.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS hosts (
    mac        TEXT PRIMARY KEY,          -- нижний регистр, через двоеточие
    ip         TEXT DEFAULT '',
    name       TEXT DEFAULT '',           -- из обратного DNS
    switch_ip  TEXT DEFAULT '',
    port       TEXT DEFAULT '',
    first_seen REAL NOT NULL,             -- unix time
    last_seen  REAL NOT NULL,             -- последний раз виден в FDB
    last_ping_ok REAL DEFAULT 0,          -- последний успешный ping
    ping_up    INTEGER DEFAULT 0          -- 1 = отвечает сейчас
);
CREATE TABLE IF NOT EXISTS journal (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    event   TEXT NOT NULL,                -- 'new_mac' | 'host_down' | 'host_up'
    mac     TEXT NOT NULL,
    details TEXT DEFAULT ''
);
"""


class Database:
    def __init__(self, path: Path | str = DEFAULT_DB_PATH):
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------- хосты ----------

    def upsert_hosts(self, hosts: list[dict]) -> list[str]:
        """Обновляет хосты после опроса FDB; возвращает впервые увиденные MAC.

        Для новых MAC пишет событие new_mac в журнал.
        """
        now = time.time()
        new_macs: list[str] = []
        with self._lock, self._conn:
            for h in hosts:
                cur = self._conn.execute(
                    "UPDATE hosts SET last_seen = ?, switch_ip = ?, port = ? "
                    "WHERE mac = ?",
                    (now, h["switch"], h["port"], h["mac"]),
                )
                if cur.rowcount == 0:
                    self._conn.execute(
                        "INSERT INTO hosts (mac, switch_ip, port, first_seen, last_seen) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (h["mac"], h["switch"], h["port"], now, now),
                    )
                    self._conn.execute(
                        "INSERT INTO journal (ts, event, mac, details) VALUES (?, ?, ?, ?)",
                        (now, "new_mac", h["mac"], f"{h['switch']} / {h['port']}"),
                    )
                    new_macs.append(h["mac"])
        for mac in new_macs:
            log.info("Новый MAC-адрес: %s", mac)
        return new_macs

    def set_ips(self, mac_to_ip: dict[str, str]) -> None:
        with self._lock, self._conn:
            for mac, ip in mac_to_ip.items():
                self._conn.execute(
                    "UPDATE hosts SET ip = ? WHERE mac = ?", (ip, mac)
                )

    def set_name(self, mac: str, name: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE hosts SET name = ? WHERE mac = ?", (name, mac)
            )

    def hosts_by_mac(self) -> dict[str, dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM hosts").fetchall()
        return {row["mac"]: dict(row) for row in rows}

    def hosts_with_ip(self) -> list[tuple[str, str]]:
        """Пары (mac, ip) всех хостов с известным IP."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT mac, ip FROM hosts WHERE ip != ''"
            ).fetchall()
        return [(row["mac"], row["ip"]) for row in rows]

    def hosts_without_name(self) -> list[tuple[str, str]]:
        """Пары (mac, ip) хостов с IP, но без имени — кандидаты на обратный DNS."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT mac, ip FROM hosts WHERE ip != '' AND name = ''"
            ).fetchall()
        return [(row["mac"], row["ip"]) for row in rows]

    # ---------- ping ----------

    def update_ping(self, results: dict[str, bool], ts: float) -> None:
        """Применяет результаты ping (mac -> отвечает ли).

        При смене состояния пишет host_up/host_down в журнал. Первый в жизни
        хоста успешный ping событием не считается — иначе после запуска
        журнал засоряется host_up по каждому живому хосту.
        """
        with self._lock, self._conn:
            for mac, up in results.items():
                row = self._conn.execute(
                    "SELECT ping_up, last_ping_ok, ip, name FROM hosts WHERE mac = ?",
                    (mac,),
                ).fetchone()
                if row is None:
                    continue
                was_up = bool(row["ping_up"])
                if up:
                    self._conn.execute(
                        "UPDATE hosts SET ping_up = 1, last_ping_ok = ? WHERE mac = ?",
                        (ts, mac),
                    )
                else:
                    self._conn.execute(
                        "UPDATE hosts SET ping_up = 0 WHERE mac = ?", (mac,)
                    )
                first_ever = not was_up and row["last_ping_ok"] == 0
                if up != was_up and not (up and first_ever):
                    self._conn.execute(
                        "INSERT INTO journal (ts, event, mac, details) VALUES (?, ?, ?, ?)",
                        (ts, "host_up" if up else "host_down",
                         mac, row["name"] or row["ip"]),
                    )

    def set_ping_state(self, mac: str, up: bool, last_ok: float) -> None:
        """Прямо выставляет состояние ping без записи в журнал (демо-режим)."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE hosts SET ping_up = ?, last_ping_ok = ? WHERE mac = ?",
                (int(up), last_ok, mac),
            )

    def touch_ping_ok(self, ts: float) -> None:
        """Обновляет last_ping_ok у живых хостов (демо-режим)."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE hosts SET last_ping_ok = ? WHERE ping_up = 1", (ts,)
            )

    # ---------- журнал ----------

    def add_event(self, ts: float, event: str, mac: str, details: str = "") -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO journal (ts, event, mac, details) VALUES (?, ?, ?, ?)",
                (ts, event, mac, details),
            )

    def journal(self, limit: int = 100) -> list[dict]:
        """Последние события, новые сверху; к каждому — имя и IP хоста."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT j.id, j.ts, j.event, j.mac, j.details, h.name, h.ip "
                "FROM journal j LEFT JOIN hosts h ON h.mac = j.mac "
                "ORDER BY j.id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]
