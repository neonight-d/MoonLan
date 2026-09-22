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
CREATE TABLE IF NOT EXISTS lldp_neighbors (
    switch_ip  TEXT NOT NULL,             -- the polled switch
    local_port TEXT NOT NULL,             -- its port, by name
    chassis_id TEXT NOT NULL,             -- normalized MAC where possible
    port_id    TEXT DEFAULT '',           -- the neighbour's own port id
    port_desc  TEXT DEFAULT '',
    sys_name   TEXT DEFAULT '',
    sys_desc   TEXT DEFAULT '',
    capabilities TEXT DEFAULT '',         -- comma separated, '' = no TLV
    mgmt_ip    TEXT DEFAULT '',
    first_seen REAL NOT NULL,
    last_seen  REAL NOT NULL,
    PRIMARY KEY (switch_ip, local_port, chassis_id)
);
CREATE TABLE IF NOT EXISTS bridges (
    chassis_id TEXT PRIMARY KEY,          -- a switch nobody polls
    sys_name   TEXT DEFAULT '',
    sys_desc   TEXT DEFAULT '',
    mgmt_ip    TEXT DEFAULT '',
    switch_ip  TEXT DEFAULT '',           -- where it is currently seen
    port       TEXT DEFAULT '',
    capabilities TEXT DEFAULT '',
    first_seen REAL NOT NULL,
    last_seen  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS layout (
    -- Where a node sits on the map. The key is the node id the
    -- topology already builds out of what the device IS — sw:<ip>,
    -- host:<mac>, bridge:<chassis id> — and not out of anything a
    -- person typed.
    --
    -- Four of those ids carry a port NAME (pseudo:, trunk:, offline:,
    -- external:), so renaming a port in the switch's firmware orphans
    -- that row. It breaks safely: the node simply comes back without a
    -- saved position, and the old row is cleaned up by age. Worth
    -- knowing before somebody goes looking for the bug.
    node_id    TEXT PRIMARY KEY,
    x          REAL NOT NULL,
    y          REAL NOT NULL,
    pinned     INTEGER DEFAULT 0,          -- 1 = placed by hand, physics off
    updated_at REAL NOT NULL
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
-- Every open map asks "when was the layout last reset?" every thirty
-- seconds, and the journal only grows
CREATE INDEX IF NOT EXISTS journal_event_ts ON journal (event, ts);
"""


class Database:
    """SQLite with room for a second process.

    The service runs out of the project directory and development
    happens in the same one, so a `diag` run, a test or a demo start
    regularly opens the same file. With the default rollback journal a
    reader blocks the writer and a five-second wait fails outright, so
    an experiment on the side would hand the running service
    `database is locked`. WAL lets readers and the writer coexist, and
    a ten-second busy timeout turns a genuine collision into a pause
    rather than an error.
    """

    def __init__(self, path: Path | str = DEFAULT_DB_PATH):
        self.path = str(path)
        self._conn = sqlite3.connect(
            self.path, check_same_thread=False, timeout=10
        )
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock, self._conn:
            if self.path != ":memory:":
                # WAL is a property of the file and survives; setting it
                # on an in-memory database is meaningless
                mode = self._conn.execute(
                    "PRAGMA journal_mode=WAL"
                ).fetchone()[0]
                if mode.lower() != "wal":
                    log.warning(
                        "Could not switch %s to WAL (journal_mode=%s): a "
                        "second process opening this database may block "
                        "the service", self.path, mode,
                    )
            self._conn.execute("PRAGMA busy_timeout=5000")
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

        A host marked `approximate` was placed on a trunk by guesswork
        because nothing better was available. Its sighting is recorded,
        but it does NOT overwrite a location the database already
        holds: doing so replaced "mb2 port 1/3", which was true, with
        "mb0 Slot0/3", which was a guess — and the guess then became
        what the next scan remembered.
        """
        now = time.time()
        hold = hold or set()
        confirmed: list[str] = []
        with self._lock, self._conn:
            for h in hosts:
                if h.get("approximate"):
                    cur = self._conn.execute(
                        "UPDATE hosts SET last_seen = ?, "
                        "seen_count = seen_count + 1, "
                        "switch_ip = CASE WHEN switch_ip = '' THEN ? "
                        "ELSE switch_ip END, "
                        "port = CASE WHEN port = '' THEN ? ELSE port END "
                        "WHERE mac = ?",
                        (now, h["switch"], h["port"], h["mac"]),
                    )
                else:
                    cur = self._conn.execute(
                        "UPDATE hosts SET last_seen = ?, switch_ip = ?, "
                        "port = ?, vlan = ?, seen_count = seen_count + 1 "
                        "WHERE mac = ?",
                        (now, h["switch"], h["port"], h.get("vlan", 0),
                         h["mac"]),
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

    # ---------- map layout ----------

    def layout(self) -> dict[str, dict]:
        """node id -> {x, y, pinned, updated_at}.

        The map is a shared object, not a personal setting. Two people
        looking at one network have to see one picture, or "the switch
        at the bottom left" stops meaning anything — which is why this
        lives here and not in somebody's localStorage.
        """
        with self._lock:
            rows = self._conn.execute("SELECT * FROM layout").fetchall()
        return {
            row["node_id"]: {
                "x": row["x"], "y": row["y"],
                "pinned": bool(row["pinned"]),
                "updated_at": row["updated_at"],
            }
            for row in rows
        }

    def save_layout(self, positions: dict[str, dict]) -> int:
        """Writes a whole snapshot; returns how many nodes were stored.

        A node already pinned stays pinned unless the caller says
        otherwise: a full save records where everything is, and does
        not quietly un-place what somebody put by hand.
        """
        now = time.time()
        with self._lock, self._conn:
            for node_id, pos in positions.items():
                pinned = pos.get("pinned")
                if pinned is None:
                    row = self._conn.execute(
                        "SELECT pinned FROM layout WHERE node_id = ?",
                        (node_id,),
                    ).fetchone()
                    pinned = row["pinned"] if row else 0
                self._conn.execute(
                    "INSERT INTO layout (node_id, x, y, pinned, updated_at) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(node_id) DO UPDATE SET "
                    "x = excluded.x, y = excluded.y, "
                    "pinned = excluded.pinned, "
                    "updated_at = excluded.updated_at",
                    (node_id, float(pos["x"]), float(pos["y"]),
                     int(bool(pinned)), now),
                )
        return len(positions)

    def add_new_positions(
        self, positions: dict[str, dict]
    ) -> tuple[list[str], dict[str, dict]]:
        """Stores positions only for nodes that have none yet.

        Returns (the ids stored, the rows that were already there). A
        page that has just drawn a node for the first time writes where
        it ended up — but "for the first time" is that page's view,
        taken when it was opened. Another page may have placed and
        pinned the same node since, and an ordinary save would put the
        pin back to false and the node back where this page's physics
        left it. Deciding "is there a row?" here, inside one
        transaction, closes that race instead of narrowing it.
        """
        now = time.time()
        stored: list[str] = []
        existing: dict[str, dict] = {}
        with self._lock, self._conn:
            for node_id, pos in positions.items():
                cur = self._conn.execute(
                    "INSERT INTO layout (node_id, x, y, pinned, updated_at) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(node_id) DO NOTHING",
                    (node_id, float(pos["x"]), float(pos["y"]),
                     int(bool(pos.get("pinned"))), now),
                )
                if cur.rowcount:
                    stored.append(node_id)
                    continue
                row = self._conn.execute(
                    "SELECT * FROM layout WHERE node_id = ?", (node_id,)
                ).fetchone()
                existing[node_id] = {
                    "x": row["x"], "y": row["y"],
                    "pinned": bool(row["pinned"]),
                    "updated_at": row["updated_at"],
                }
        return stored, existing

    def set_node_position(
        self, node_id: str, x: float, y: float, pinned: bool = True
    ) -> None:
        """One node, moved by hand."""
        self.save_layout({node_id: {"x": x, "y": y, "pinned": pinned}})

    def forget_node_position(self, node_id: str) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM layout WHERE node_id = ?", (node_id,)
            )
        return cur.rowcount > 0

    def clear_layout(self) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM layout")
        return cur.rowcount

    def layout_saved_at(self) -> float:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(updated_at) AS ts FROM layout"
            ).fetchone()
        return row["ts"] or 0.0

    def layout_cleared_at(self) -> float:
        """When the whole layout was last reset; 0.0 if it never was.

        Read from the journal, which already records every reset. An
        open page cannot tell a reset from the rows it sees — rows also
        go when a node is released by an older page or forgotten by
        age — so it is told outright.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(ts) AS ts FROM journal "
                "WHERE event = 'layout_cleared'"
            ).fetchone()
        return row["ts"] or 0.0

    def purge_layout(self, keep_days: float, present: set[str]) -> list[str]:
        """Drops positions of nodes that are old AND gone.

        A device switched off for the night is not a device that was
        taken away: coming back, it belongs where it was. Only age
        decides, and only for a node the current topology no longer
        has — a row for something still on the map is never touched,
        however long ago it was placed.
        """
        if keep_days <= 0:
            return []
        cutoff = time.time() - keep_days * 86400
        with self._lock, self._conn:
            rows = self._conn.execute(
                "SELECT node_id FROM layout WHERE updated_at < ?", (cutoff,)
            ).fetchall()
            gone = [
                row["node_id"] for row in rows if row["node_id"] not in present
            ]
            for node_id in gone:
                self._conn.execute(
                    "DELETE FROM layout WHERE node_id = ?", (node_id,)
                )
        return gone

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

    # ---------- LLDP neighbours and unmanaged bridges ----------

    def upsert_lldp(self, rows: list[dict], ts: float) -> None:
        """Records this poll's LLDP neighbours, keeping first_seen.

        The table is history, not state: the map is drawn from the
        current poll. What it adds is "since when" — the first and last
        time a neighbour was seen behind a port, which is what turns a
        newly appeared bridge into a dated fact.
        """
        with self._lock, self._conn:
            for row in rows:
                key = (row["switch_ip"], row["local_port"], row["chassis_id"])
                cur = self._conn.execute(
                    "UPDATE lldp_neighbors SET port_id = ?, port_desc = ?, "
                    "sys_name = ?, sys_desc = ?, capabilities = ?, "
                    "mgmt_ip = ?, last_seen = ? "
                    "WHERE switch_ip = ? AND local_port = ? AND chassis_id = ?",
                    (
                        row.get("port_id", ""), row.get("port_desc", ""),
                        row.get("sys_name", ""), row.get("sys_desc", ""),
                        row.get("capabilities", ""), row.get("mgmt_ip", ""),
                        ts, *key,
                    ),
                )
                if cur.rowcount == 0:
                    self._conn.execute(
                        "INSERT INTO lldp_neighbors (switch_ip, local_port, "
                        "chassis_id, port_id, port_desc, sys_name, sys_desc, "
                        "capabilities, mgmt_ip, first_seen, last_seen) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            *key, row.get("port_id", ""),
                            row.get("port_desc", ""), row.get("sys_name", ""),
                            row.get("sys_desc", ""),
                            row.get("capabilities", ""), row.get("mgmt_ip", ""),
                            ts, ts,
                        ),
                    )

    def upsert_bridges(self, rows: list[dict], ts: float) -> list[str]:
        """Stores the bridges LLDP found; returns the ones seen first now."""
        fresh: list[str] = []
        with self._lock, self._conn:
            for row in rows:
                cur = self._conn.execute(
                    "UPDATE bridges SET sys_name = ?, sys_desc = ?, "
                    "mgmt_ip = ?, switch_ip = ?, port = ?, capabilities = ?, "
                    "last_seen = ? WHERE chassis_id = ?",
                    (
                        row.get("sys_name", ""), row.get("sys_desc", ""),
                        row.get("mgmt_ip", ""), row.get("switch_ip", ""),
                        row.get("port", ""), row.get("capabilities", ""),
                        ts, row["chassis_id"],
                    ),
                )
                if cur.rowcount == 0:
                    self._conn.execute(
                        "INSERT INTO bridges (chassis_id, sys_name, sys_desc, "
                        "mgmt_ip, switch_ip, port, capabilities, first_seen, "
                        "last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            row["chassis_id"], row.get("sys_name", ""),
                            row.get("sys_desc", ""), row.get("mgmt_ip", ""),
                            row.get("switch_ip", ""), row.get("port", ""),
                            row.get("capabilities", ""), ts, ts,
                        ),
                    )
                    fresh.append(row["chassis_id"])
        for chassis_id in fresh:
            log.info("LLDP: a bridge we do not poll appeared — %s", chassis_id)
        return fresh

    def bridges(self) -> dict[str, dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM bridges").fetchall()
        return {row["chassis_id"]: dict(row) for row in rows}

    def lldp_neighbors(self, switch_ip: str = "") -> list[dict]:
        with self._lock:
            if switch_ip:
                rows = self._conn.execute(
                    "SELECT * FROM lldp_neighbors WHERE switch_ip = ? "
                    "ORDER BY local_port", (switch_ip,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM lldp_neighbors ORDER BY switch_ip, local_port"
                ).fetchall()
        return [dict(row) for row in rows]

    def purge_lldp(self, cutoff: float) -> int:
        """Drops LLDP rows nothing has seen since cutoff."""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM lldp_neighbors WHERE last_seen < ?", (cutoff,)
            )
        return cur.rowcount

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
