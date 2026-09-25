"""The ping cycle reads the metadata of the pinged hosts only (PR #2,
ItsWanheda) — and the alarms come out the same as when it read the
whole inventory. Also: two host rows never share an IP, which is what
lets the demo's probe look a host up by address."""

import asyncio
import sqlite3
import unittest
from unittest import mock

import service_fixture  # noqa: F401  (sets MOONLAN_CONFIG first)
from moonlan import server
from moonlan.alarms import AlarmEngine
from moonlan.db import Database
from moonlan.notify import Notifier

PORT = ("10.0.0.1", "Gi0/1")

# Four monitored devices with addresses on one port — enough for
# port_hosts_down — and three on the same port and one elsewhere that
# have no address: in the inventory, never pinged, so never in a cycle's
# results. Those are the rows the narrowed query no longer reads.
PINGED = {
    f"02:00:00:00:00:0{n}": f"192.0.2.{n}" for n in range(1, 5)
}
NOT_PINGED = ["02:00:00:00:01:01", "02:00:00:00:01:02", "02:00:00:00:01:03",
              "02:00:00:00:02:01"]


def inventory() -> Database:
    db = Database(":memory:")
    with db._lock, db._conn:
        for mac, ip in PINGED.items():
            db._conn.execute(
                "INSERT INTO hosts (mac, ip, name, switch_ip, port, "
                "first_seen, last_seen, monitored, confirmed, ping_up) "
                "VALUES (?, ?, ?, ?, ?, 1, 1, 1, 1, 1)",
                (mac, ip, "pc-" + ip.rsplit(".", 1)[1], *PORT),
            )
        for i, mac in enumerate(NOT_PINGED):
            where = PORT if i < 3 else ("10.0.0.2", "Gi0/5")
            db._conn.execute(
                "INSERT INTO hosts (mac, ip, switch_ip, port, first_seen, "
                "last_seen, monitored, confirmed) "
                "VALUES (?, '', ?, ?, 1, 1, 1, 1)",
                (mac, *where),
            )
    return db


class PingMetaTest(unittest.TestCase):
    def run_cycles(self, db, engine, answers):
        async def ping_many(ips):
            return {ip: answers.get(ip, True) for ip in ips}

        with mock.patch.object(server, "db", db), \
                mock.patch.object(server, "alarm_engine", engine), \
                mock.patch.object(server, "fdb_macs",
                                  set(PINGED) | set(NOT_PINGED)), \
                mock.patch.object(server.pinger, "ping_many", ping_many):
            asyncio.run(server.run_ping())

    def scenario(self, whole_inventory: bool):
        db = inventory()
        if whole_inventory:
            # what the cycle did before: every row of the inventory
            db.hosts_by_macs = lambda macs: db.hosts_by_mac()
        notifier = Notifier(server.config)
        engine = AlarmEngine(db, notifier, server.config.thresholds,
                             server.config.notifications)
        up = {ip: True for ip in PINGED.values()}
        down = {ip: False for ip in PINGED.values()}
        for answers in (up, up, down, down, down):
            self.run_cycles(db, engine, answers)
        # a restart while the port is down: the active port_hosts_down
        # has to rebuild its set of devices from the metadata
        engine = AlarmEngine(db, notifier, server.config.thresholds,
                             server.config.notifications)
        asyncio.run(engine.load())
        for answers in (down, {**down, "192.0.2.1": True, "192.0.2.2": True},
                        up):
            self.run_cycles(db, engine, answers)
        alarms = sorted(
            (row["type"], row["subject"], row["severity"], row["message"],
             row["ts_cleared"] == 0)
            for row in db.alarms(True, 100) + db.alarms(False, 100)
        )
        events = [(e["event"], e["mac"]) for e in reversed(db.journal(100))]
        db.close()
        return alarms, events

    def test_the_same_alarms_as_with_the_whole_inventory(self):
        narrowed = self.scenario(whole_inventory=False)
        whole = self.scenario(whole_inventory=True)
        self.assertEqual(narrowed, whole)
        # …and the scenario did exercise what it was meant to
        kinds = {alarm[0] for alarm in narrowed[0]}
        self.assertIn("host_down", kinds)
        self.assertIn("port_hosts_down", kinds)
        self.assertTrue(all(not alarm[4] for alarm in narrowed[0]),
                        "everything cleared once the hosts answered")

    def test_only_the_pinged_rows_are_read(self):
        db = inventory()
        self.addCleanup(db.close)
        rows = db.hosts_by_macs(set(PINGED))
        self.assertEqual(set(rows), set(PINGED))


class OneAddressOneHostTest(unittest.TestCase):
    """_demo_probe looks a host up by its address (host_by_ip), where it
    used to scan every row with that address. The same answer, because
    two rows cannot hold one address — a unique index in the schema,
    not only a rule in the code (v0.5.3)."""

    def test_the_schema_refuses_a_second_row_with_an_address(self):
        db = Database(":memory:")
        self.addCleanup(db.close)
        indexes = {
            row[1]: row[2] for row in db._conn.execute(
                "PRAGMA index_list(hosts)"
            )
        }
        self.assertEqual(indexes.get("idx_hosts_ip"), 1, "unique")
        with db._lock, db._conn:
            db._conn.execute(
                "INSERT INTO hosts (mac, ip, first_seen, last_seen) "
                "VALUES ('02:00:00:00:00:01', '192.0.2.1', 1, 1)"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            with db._lock, db._conn:
                db._conn.execute(
                    "INSERT INTO hosts (mac, ip, first_seen, last_seen) "
                    "VALUES ('02:00:00:00:00:02', '192.0.2.1', 1, 1)"
                )
        # no address is not an address: any number of rows may lack one
        with db._lock, db._conn:
            db._conn.executemany(
                "INSERT INTO hosts (mac, ip, first_seen, last_seen) "
                "VALUES (?, '', 1, 1)",
                [("02:00:00:00:00:03",), ("02:00:00:00:00:04",)],
            )
        self.assertEqual(db.host_by_ip("192.0.2.1")["mac"], "02:00:00:00:00:01")
        self.assertIsNone(db.host_by_ip(""))


if __name__ == "__main__":
    unittest.main()
