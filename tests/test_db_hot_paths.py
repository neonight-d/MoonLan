"""Regression tests for SQLite inventory hot paths."""

import tempfile
import unittest
from pathlib import Path

from moonlan.db import Database


class HostQueryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "moonlan.db")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_hosts_by_macs_returns_only_requested_rows(self):
        rows = [
            ("02:00:00:00:00:01", "10.0.0.1"),
            ("02:00:00:00:00:02", "10.0.0.2"),
            ("02:00:00:00:00:03", "10.0.0.3"),
        ]
        with self.db._lock, self.db._conn:
            for mac, ip in rows:
                self.db._conn.execute(
                    "INSERT INTO hosts (mac, ip, first_seen, last_seen) "
                    "VALUES (?, ?, 1, 1)",
                    (mac, ip),
                )

        result = self.db.hosts_by_macs({
            "02:00:00:00:00:01",
            "02:00:00:00:00:03",
        })

        self.assertEqual(set(result), {
            "02:00:00:00:00:01",
            "02:00:00:00:00:03",
        })
        self.assertNotIn("02:00:00:00:00:02", result)

    def test_hosts_by_macs_handles_more_than_one_sqlite_batch(self):
        macs = [
            f"02:00:00:{i // 65536:02x}:{(i // 256) % 256:02x}:{i % 256:02x}"
            for i in range(501)
        ]
        with self.db._lock, self.db._conn:
            self.db._conn.executemany(
                "INSERT INTO hosts (mac, first_seen, last_seen) VALUES (?, 1, 1)",
                [(mac,) for mac in macs],
            )

        result = self.db.hosts_by_macs(macs)

        self.assertEqual(len(result), 501)

    def test_host_by_ip_returns_one_current_owner(self):
        mac = "02:00:00:00:00:10"
        with self.db._lock, self.db._conn:
            self.db._conn.execute(
                "INSERT INTO hosts (mac, ip, first_seen, last_seen) "
                "VALUES (?, ?, 1, 1)",
                (mac, "10.0.0.10"),
            )

        self.assertEqual(
            self.db.host_by_ip("10.0.0.10")["mac"], mac
        )
        self.assertIsNone(self.db.host_by_ip("10.0.0.99"))


class ProjectedUnconfirmedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "moonlan.db")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_projection_does_not_need_the_whole_inventory(self):
        # One unrelated confirmed record proves the query is scoped to
        # the FDB addresses supplied by the caller.
        with self.db._lock, self.db._conn:
            self.db._conn.execute(
                "INSERT INTO hosts "
                "(mac, first_seen, last_seen, seen_count, confirmed) "
                "VALUES (?, 1, 1, 99, 1)",
                ("02:00:00:00:00:99",),
            )
            self.db._conn.execute(
                "INSERT INTO hosts "
                "(mac, first_seen, last_seen, seen_count, confirmed) "
                "VALUES (?, 1, 1, 1, 0)",
                ("02:00:00:00:00:01",),
            )

        self.assertEqual(
            self.db.projected_unconfirmed(
                {"02:00:00:00:00:01"}, 3
            ),
            {"02:00:00:00:00:01"},
        )
        self.assertEqual(
            self.db.projected_unconfirmed(
                {"02:00:00:00:00:99"}, 3
            ),
            set(),
        )


if __name__ == "__main__":
    unittest.main()
