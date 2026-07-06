"""SQLite storage: hosts and the event journal.

Plain sqlite3, no ORM. All methods are synchronous; call them from
async code via asyncio.to_thread. A single connection is shared between
threads (check_same_thread=False), access is serialized with a Lock.
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
    mac        TEXT PRIMARY KEY,          -- lowercase, colon-separated
    ip         TEXT DEFAULT '',
    name       TEXT DEFAULT '',           -- from reverse DNS
    switch_ip  TEXT DEFAULT '',
    port       TEXT DEFAULT '',
    first_seen REAL NOT NULL,             -- unix time
    last_seen  REAL NOT NULL,             -- last seen in FDB
    last_ping_ok REAL DEFAULT 0,          -- last successful ping
    ping_up    INTEGER DEFAULT 0,         -- 1 = replying right now
    vlan       INTEGER DEFAULT 0          -- PVID of the port the host is on
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
            self._migrate()

    def _migrate(self) -> None:
        """Brings an old database up to date: adds missing columns."""
        columns = {
            row[1] for row in self._conn.execute("PRAGMA table_info(hosts)")
        }
        if "vlan" not in columns:
            self._conn.execute(
                "ALTER TABLE hosts ADD COLUMN vlan INTEGER DEFAULT 0"
            )
            log.info("DB migration: added hosts.vlan column")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------- hosts ----------

    def upsert_hosts(self, hosts: list[dict]) -> list[str]:
        """Updates hosts after an FDB poll; returns MACs seen for the first time.

        Writes a new_mac journal event for every new MAC.
        """
        now = time.time()
        new_macs: list[str] = []
        with self._lock, self._conn:
            for h in hosts:
                cur = self._conn.execute(
                    "UPDATE hosts SET last_seen = ?, switch_ip = ?, port = ?, vlan = ? "
                    "WHERE mac = ?",
                    (now, h["switch"], h["port"], h.get("vlan", 0), h["mac"]),
                )
                if cur.rowcount == 0:
                    self._conn.execute(
                        "INSERT INTO hosts (mac, switch_ip, port, vlan, first_seen, last_seen) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (h["mac"], h["switch"], h["port"], h.get("vlan", 0), now, now),
                    )
                    self._conn.execute(
                        "INSERT INTO journal (ts, event, mac, details) VALUES (?, ?, ?, ?)",
                        (now, "new_mac", h["mac"], f"{h['switch']} / {h['port']}"),
                    )
                    new_macs.append(h["mac"])
        for mac in new_macs:
            log.info("New MAC address: %s", mac)
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
        """(mac, ip) pairs of all hosts with a known IP."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT mac, ip FROM hosts WHERE ip != ''"
            ).fetchall()
        return [(row["mac"], row["ip"]) for row in rows]

    def hosts_without_name(self) -> list[tuple[str, str]]:
        """(mac, ip) pairs of hosts with an IP but no name — reverse DNS candidates."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT mac, ip FROM hosts WHERE ip != '' AND name = ''"
            ).fetchall()
        return [(row["mac"], row["ip"]) for row in rows]

    # ---------- ping ----------

    def update_ping(self, results: dict[str, bool], ts: float) -> None:
        """Applies ping results (mac -> replied or not).

        Writes host_up/host_down journal events on state changes. The very
        first successful ping in a host's life is not an event — otherwise
        the journal would be flooded with host_up for every live host
        right after startup.
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
        """Sets the ping state directly, no journal event (demo mode)."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE hosts SET ping_up = ?, last_ping_ok = ? WHERE mac = ?",
                (int(up), last_ok, mac),
            )

    def touch_ping_ok(self, ts: float) -> None:
        """Refreshes last_ping_ok of live hosts (demo mode)."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE hosts SET last_ping_ok = ? WHERE ping_up = 1", (ts,)
            )

    # ---------- journal ----------

    def add_event(self, ts: float, event: str, mac: str, details: str = "") -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO journal (ts, event, mac, details) VALUES (?, ?, ?, ?)",
                (ts, event, mac, details),
            )

    def journal(self, limit: int = 100) -> list[dict]:
        """Latest events, newest first; each with the host's name and IP."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT j.id, j.ts, j.event, j.mac, j.details, h.name, h.ip "
                "FROM journal j LEFT JOIN hosts h ON h.mac = j.mac "
                "ORDER BY j.id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]
