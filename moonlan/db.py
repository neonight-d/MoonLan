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
    vlan       INTEGER DEFAULT 0,         -- PVID of the port the host is on
    monitored  INTEGER DEFAULT 0,         -- 1 = host_down alarms wanted
    last_arp   REAL DEFAULT 0,            -- last seen in a router's ARP table
    ip_confirmed REAL DEFAULT 0,          -- when ARP last tied this IP to this MAC
    seen_count INTEGER DEFAULT 0,         -- polls this MAC was in an FDB
    confirmed  INTEGER DEFAULT 0          -- 1 = real enough to go on the map
);
CREATE TABLE IF NOT EXISTS journal (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    event   TEXT NOT NULL,                -- 'new_mac' | 'host_down' | 'host_up'
                                          -- | 'alarm_raised' | 'alarm_cleared'
    mac     TEXT NOT NULL,                -- alarm events store the subject here
    details TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS alarms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,        -- host_down|switch_down|port_errors|port_util|new_mac
    subject TEXT NOT NULL,     -- mac / switch_ip / switch_ip:port
    severity TEXT NOT NULL,    -- info|warning|critical
    message TEXT DEFAULT '',
    ts_raised REAL NOT NULL,
    ts_cleared REAL DEFAULT 0, -- 0 = active
    notified INTEGER DEFAULT 0
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
        if "monitored" not in columns:
            self._conn.execute(
                "ALTER TABLE hosts ADD COLUMN monitored INTEGER DEFAULT 0"
            )
            log.info("DB migration: added hosts.monitored column")
        if "last_arp" not in columns:
            self._conn.execute(
                "ALTER TABLE hosts ADD COLUMN last_arp REAL DEFAULT 0"
            )
            log.info("DB migration: added hosts.last_arp column")
        if "ip_confirmed" not in columns:
            self._conn.execute(
                "ALTER TABLE hosts ADD COLUMN ip_confirmed REAL DEFAULT 0"
            )
            log.info("DB migration: added hosts.ip_confirmed column")
        if "seen_count" not in columns:
            self._conn.execute(
                "ALTER TABLE hosts ADD COLUMN seen_count INTEGER DEFAULT 0"
            )
            # Everything already in the database has been seen at least
            # once; anything with an IP or an FDB sighting is a device
            # the operator has been looking at, not a fresh guess.
            self._conn.execute(
                "UPDATE hosts SET seen_count = 1 "
                "WHERE ip <> '' OR last_seen > 0"
            )
            log.info("DB migration: added hosts.seen_count column")
        if "confirmed" not in columns:
            self._conn.execute(
                "ALTER TABLE hosts ADD COLUMN confirmed INTEGER DEFAULT 0"
            )
            self._conn.execute(
                "UPDATE hosts SET confirmed = 1 WHERE ip <> '' OR last_seen > 0"
            )
            log.info("DB migration: added hosts.confirmed column")
        # An IP belongs to exactly one MAC. Stale ARP pairs (a device
        # changed its MAC, DHCP reassigned the address) used to leave
        # the same IP on several host rows, and then one unreachable
        # address tripped the mass-outage threshold all by itself.
        freed = self._dedupe_ips()
        if freed:
            log.info(
                "DB migration: cleared duplicate IPs on %d host records", freed
            )
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_hosts_ip "
            "ON hosts(ip) WHERE ip <> ''"
        )

    def _dedupe_ips(self) -> int:
        """Leaves each non-empty IP on its most recently seen host only."""
        cur = self._conn.execute(
            "UPDATE hosts SET ip = '' WHERE ip <> '' AND mac <> ("
            "  SELECT h2.mac FROM hosts h2 WHERE h2.ip = hosts.ip"
            "  ORDER BY MAX(h2.last_seen, COALESCE(h2.last_arp, 0)) DESC,"
            "           h2.mac LIMIT 1)"
        )
        return cur.rowcount

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------- hosts ----------

    def upsert_hosts(
        self, hosts: list[dict], confirm_scans: int = 1,
        hold: set[str] | None = None,
    ) -> list[str]:
        """Updates hosts after an FDB poll; returns the MACs CONFIRMED now.

        seen_count counts the polls a MAC was present in some MAC table
        — once per call, since one call is one poll. A MAC becomes a
        device (and gets its new_mac journal event) only after
        confirm_scans sightings, or at once if ARP already gave it an
        IP. A damaged frame invents an address that is gone by the next
        poll; making it wait costs nothing and keeps those out of the
        map, the journal and the alarms.

        hold are MACs that must not be confirmed by sightings however
        many they accumulate — the ones that look like damaged copies
        of a real address. An IP still confirms them: ARP answers come
        back only from a device that exists.
        """
        now = time.time()
        hold = hold or set()
        confirmed: list[str] = []
        with self._lock, self._conn:
            for h in hosts:
                cur = self._conn.execute(
                    "UPDATE hosts SET last_seen = ?, switch_ip = ?, port = ?, "
                    "vlan = ?, seen_count = seen_count + 1 WHERE mac = ?",
                    (now, h["switch"], h["port"], h.get("vlan", 0), h["mac"]),
                )
                if cur.rowcount == 0:
                    self._conn.execute(
                        "INSERT INTO hosts (mac, switch_ip, port, vlan, "
                        "first_seen, last_seen, seen_count) "
                        "VALUES (?, ?, ?, ?, ?, ?, 1)",
                        (h["mac"], h["switch"], h["port"], h.get("vlan", 0), now, now),
                    )
                row = self._conn.execute(
                    "SELECT seen_count, ip, confirmed FROM hosts WHERE mac = ?",
                    (h["mac"],),
                ).fetchone()
                if row["confirmed"]:
                    continue
                if row["ip"]:
                    pass  # ARP vouches for it whatever else is true
                elif h["mac"] in hold or row["seen_count"] < confirm_scans:
                    continue
                self._confirm(h["mac"], now, f"{h['switch']} / {h['port']}")
                confirmed.append(h["mac"])
        for mac in confirmed:
            log.info("New MAC address: %s", mac)
        return confirmed

    def _confirm(self, mac: str, ts: float, details: str) -> None:
        """Marks a MAC real and journals it (call inside the lock)."""
        self._conn.execute(
            "UPDATE hosts SET confirmed = 1 WHERE mac = ?", (mac,)
        )
        self._conn.execute(
            "INSERT INTO journal (ts, event, mac, details) VALUES (?, ?, ?, ?)",
            (ts, "new_mac", mac, details),
        )

    def confirm_hosts_with_ip(self) -> list[str]:
        """Confirms unconfirmed hosts ARP has given an IP.

        An address that answers ARP belongs to a device that exists —
        there is nothing to wait for. Called after the ARP pass, so a
        real newcomer reaches the map on its very first poll.
        """
        now = time.time()
        with self._lock, self._conn:
            rows = self._conn.execute(
                "SELECT mac, ip, switch_ip, port FROM hosts "
                "WHERE confirmed = 0 AND ip <> ''"
            ).fetchall()
            for row in rows:
                self._confirm(
                    row["mac"], now,
                    row["ip"] or f"{row['switch_ip']} / {row['port']}",
                )
        for row in rows:
            log.info("New MAC address: %s (%s, confirmed by ARP)",
                     row["mac"], row["ip"])
        return [row["mac"] for row in rows]

    def unconfirmed_macs(self) -> set[str]:
        """MACs stored but not yet accepted as devices."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT mac FROM hosts WHERE confirmed = 0"
            ).fetchall()
        return {row["mac"] for row in rows}

    def projected_unconfirmed(
        self, fdb_macs: set[str], confirm_scans: int
    ) -> set[str]:
        """Which of the MACs in this poll will still be unconfirmed
        after it — the same verdict upsert_hosts is about to reach.

        The topology needs the answer BEFORE the hosts are written, so
        that unconfirmed addresses take part in neither the map nor the
        per-port device counts.
        """
        if confirm_scans <= 1:
            return set()
        with self._lock:
            rows = {
                row["mac"]: row
                for row in self._conn.execute(
                    "SELECT mac, seen_count, ip, confirmed FROM hosts"
                ).fetchall()
            }
        pending: set[str] = set()
        for mac in fdb_macs:
            row = rows.get(mac)
            if row is None:
                pending.add(mac)  # brand new: seen_count becomes 1
            elif not row["confirmed"] and not row["ip"] and (
                row["seen_count"] + 1 < confirm_scans
            ):
                pending.add(mac)
        return pending

    def set_ips(
        self, mac_to_ip: dict[str, str], create_missing: bool = False
    ) -> int:
        """Applies ARP data (MAC -> IP); returns the number of hosts created.

        An IP ends up on exactly one MAC: the address is taken away
        from its previous owner first, so the later ARP entry wins
        (the merged ARP table is iterated in source order), and
        ip_confirmed records that ARP still ties the two together. With
        create_missing, MACs that are in ARP but on no switch port are
        stored with an empty switch_ip — the "not on map" inventory;
        their last_seen stays 0 (never seen in an FDB) and last_arp
        carries their liveness instead.
        """
        now = time.time()
        created = 0
        with self._lock, self._conn:
            for mac, ip in mac_to_ip.items():
                if not ip:
                    continue
                self._conn.execute(
                    "UPDATE hosts SET ip = '' WHERE ip = ? AND mac <> ?",
                    (ip, mac),
                )
                cur = self._conn.execute(
                    "UPDATE hosts SET ip = ?, last_arp = ?, ip_confirmed = ? "
                    "WHERE mac = ?",
                    (ip, now, now, mac),
                )
                if cur.rowcount == 0 and create_missing:
                    self._conn.execute(
                        "INSERT INTO hosts (mac, ip, switch_ip, port, "
                        "first_seen, last_seen, last_arp, ip_confirmed) "
                        "VALUES (?, ?, '', '', ?, 0, ?, ?)",
                        (mac, ip, now, now, now),
                    )
                    created += 1
        return created

    def set_ip_confirmed(self, mac: str, ts: float) -> None:
        """Overrides when ARP last confirmed the host's IP (demo seeding)."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE hosts SET ip_confirmed = ? WHERE mac = ?", (ts, mac)
            )

    def set_last_seen(self, mac: str, ts: float) -> None:
        """Overrides when the host was last seen in an FDB (demo seeding)."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE hosts SET last_seen = ? WHERE mac = ?", (ts, mac)
            )

    def release_ip(self, mac: str, ts: float, note: str = "") -> bool:
        """Takes the IP away from a host and logs it.

        A host whose MAC no longer appears in any MAC table and whose
        address ARP stopped confirming must not keep pinging that
        address: another device may hold it now, and its replies would
        make the dead record look alive.
        """
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT ip FROM hosts WHERE mac = ? AND ip != ''", (mac,)
            ).fetchone()
            if row is None:
                return False
            self._conn.execute(
                "UPDATE hosts SET ip = '', ip_confirmed = 0, ping_up = 0 "
                "WHERE mac = ?",
                (mac,),
            )
            self._conn.execute(
                "INSERT INTO journal (ts, event, mac, details) VALUES (?, ?, ?, ?)",
                (ts, "ip_released", mac, f"{row['ip']} {note}".strip()),
            )
        return True

    def delete_hosts(self, macs: list[str]) -> int:
        """Removes host records outright (invalid MACs left by old scans)."""
        if not macs:
            return 0
        with self._lock, self._conn:
            cur = self._conn.executemany(
                "DELETE FROM hosts WHERE mac = ?", [(m,) for m in macs]
            )
        return cur.rowcount

    def purge_old_hosts(self, cutoff: float) -> int:
        """Deletes hosts not seen — in any FDB or ARP table — since cutoff."""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM hosts WHERE "
                "MAX(last_seen, COALESCE(last_arp, 0)) < ?",
                (cutoff,),
            )
        return cur.rowcount

    def set_monitored(self, mac: str, monitored: bool) -> bool:
        """Sets the host_down alarm flag; False if the MAC is unknown."""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE hosts SET monitored = ? WHERE mac = ?",
                (int(monitored), mac),
            )
        return cur.rowcount > 0

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

    # ---------- alarms ----------

    def raise_alarm(
        self,
        alarm_type: str,
        subject: str,
        severity: str,
        message: str,
        ts: float,
        auto_clear: bool = False,
    ) -> bool:
        """Inserts an alarm; False if one is already active for (type, subject).

        auto_clear inserts an instantly cleared alarm (new_mac): a pure
        notification event that never stays active.
        """
        with self._lock, self._conn:
            if not auto_clear:
                row = self._conn.execute(
                    "SELECT id FROM alarms WHERE type = ? AND subject = ? "
                    "AND ts_cleared = 0",
                    (alarm_type, subject),
                ).fetchone()
                if row is not None:
                    return False
            self._conn.execute(
                "INSERT INTO alarms (type, subject, severity, message, "
                "ts_raised, ts_cleared, notified) VALUES (?, ?, ?, ?, ?, ?, 1)",
                (alarm_type, subject, severity, message, ts,
                 ts if auto_clear else 0),
            )
        return True

    def escalate_alarm(
        self, alarm_type: str, subject: str, severity: str, message: str
    ) -> bool:
        """Raises the severity of an alarm that is already active.

        Evidence can arrive after the alarm: frame corruption is
        suspected from the MAC table, then the port's error counters
        confirm it. Clearing and re-raising would send a misleading
        CLEARED, so the standing alarm is upgraded in place.
        """
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE alarms SET severity = ?, message = ? "
                "WHERE type = ? AND subject = ? AND ts_cleared = 0 "
                "AND severity <> ?",
                (severity, message, alarm_type, subject, severity),
            )
        return cur.rowcount > 0

    def clear_alarm(
        self, alarm_type: str, subject: str, ts: float, note: str = ""
    ) -> bool:
        """Closes the active alarm for (type, subject); False if none was.

        A non-empty note is appended to the alarm's message (used by
        the stale-alarm janitor and manual clears).
        """
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE alarms SET ts_cleared = ?, message = message || ? "
                "WHERE type = ? AND subject = ? AND ts_cleared = 0",
                (ts, f" — {note}" if note else "", alarm_type, subject),
            )
        return cur.rowcount > 0

    def clear_alarm_by_id(
        self, alarm_id: int, ts: float, note: str = ""
    ) -> dict | None:
        """Closes one active alarm by id; returns its row or None."""
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT * FROM alarms WHERE id = ? AND ts_cleared = 0",
                (alarm_id,),
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "UPDATE alarms SET ts_cleared = ?, message = message || ? "
                "WHERE id = ?",
                (ts, f" — {note}" if note else "", alarm_id),
            )
        return dict(row)

    def alarms(self, active: bool, limit: int = 50) -> list[dict]:
        """Active alarms (newest first) or the latest cleared ones."""
        with self._lock:
            if active:
                rows = self._conn.execute(
                    "SELECT * FROM alarms WHERE ts_cleared = 0 "
                    "ORDER BY ts_raised DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM alarms WHERE ts_cleared > 0 "
                    "ORDER BY ts_cleared DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [dict(row) for row in rows]

    # ---------- journal ----------

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
