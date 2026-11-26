"""The journal: a group of its own for addresses, and a term (v0.8.7).

Of 4315 events in "Devices" on the network this was written for, 2810
were ip_released: they buried new_mac and copies_removed. They are the
"Addresses" group now. And the journal was never cleaned — about two
thousand events a day, nine in ten of them pings. Pings go after
journal_ping_keep_days (30), the rest after journal_keep_days (365);
0 keeps for good, and a ping term of 0 is the general one. The cleanup
deletes in batches of 10 000 rows, a transaction each, writes one INFO
line and nothing to the journal, and never vacuums.

Run with:  python -m unittest discover -s tests
"""

import contextlib
import io
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from service_fixture import SWITCHES  # noqa: E402,F401

from moonlan import diag, server, settings  # noqa: E402
from moonlan.db import Database  # noqa: E402

DAY = 86400


class GroupTest(unittest.TestCase):
    def test_ip_released_is_an_address_event(self):
        self.assertEqual(server.JOURNAL_GROUPS["addresses"], ("ip_released",))
        self.assertEqual(server.EVENT_GROUP["ip_released"], "addresses")
        self.assertNotIn("ip_released", server.JOURNAL_GROUPS["devices"])
        # after "ping", in the order of the page's filters
        groups = list(server.JOURNAL_GROUPS)
        self.assertEqual(groups.index("addresses"), groups.index("ping") + 1)


class SettingsTest(unittest.TestCase):
    def test_two_terms_beside_host_retention_days(self):
        keys = [s.key for s in settings.SETTINGS]
        at = keys.index("host_retention_days")
        self.assertEqual(keys[at + 1:at + 3],
                         ["journal_keep_days", "journal_ping_keep_days"])
        for key, default in (("journal_keep_days", 365),
                             ("journal_ping_keep_days", 30)):
            entry = settings.BY_KEY[key]
            self.assertEqual(entry.default(), default)
            self.assertEqual((entry.minimum, entry.maximum), (0, 3650))
            self.assertEqual(entry.section, "map")
            self.assertEqual(entry.applies, "next_cleanup")


class CleanupTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._db = server.rt.db
        self._tmp = tempfile.TemporaryDirectory()
        server.rt.db = Database(Path(self._tmp.name) / "journal.db")
        self._keep = server.rt.config.journal_keep_days
        self._ping = server.rt.config.journal_ping_keep_days
        server.rt.config.journal_keep_days = 365
        server.rt.config.journal_ping_keep_days = 30

    def tearDown(self):
        server.rt.db.close()
        server.rt.db = self._db
        server.rt.config.journal_keep_days = self._keep
        server.rt.config.journal_ping_keep_days = self._ping
        self._tmp.cleanup()

    def fill(self, rows):
        """(event, age in days, how many)"""
        now = time.time()
        with server.rt.db._lock, server.rt.db._conn:
            server.rt.db._conn.executemany(
                "INSERT INTO journal (ts, event, mac, details) VALUES (?, ?, '', '')",
                [(now - age * DAY - i * 0.001, event, )
                 for event, age, count in rows for i in range(count)],
            )

    def count(self, event):
        return server.rt.db._conn.execute(
            "SELECT COUNT(*) FROM journal WHERE event = ?", (event,)
        ).fetchone()[0]

    async def test_old_pings_go_and_fresh_ones_stay(self):
        self.fill([
            ("host_down", 40, 5), ("host_up", 31, 5),   # past 30 days
            ("host_down", 20, 3), ("host_up", 1, 2),    # inside
            ("new_mac", 40, 4),                         # under 365
            ("new_mac", 400, 2), ("ip_released", 366, 1),
        ])
        with self.assertLogs("moonlan", "INFO") as logged:
            await server.purge_journal()
        self.assertEqual(self.count("host_down"), 3)
        self.assertEqual(self.count("host_up"), 2)
        self.assertEqual(self.count("new_mac"), 4)
        self.assertEqual(self.count("ip_released"), 0)
        lines = [line for line in logged.output if "Journal cleanup" in line]
        self.assertEqual(len(lines), 1)
        self.assertIn("10 ping event(s) older than 30 days", lines[0])
        self.assertIn("3 other(s) older than 365 days", lines[0])
        self.assertIn("the oldest from", lines[0])
        # the cleanup is not an event of its own
        self.assertEqual(
            server.rt.db._conn.execute("SELECT COUNT(*) FROM journal").fetchone()[0],
            9,
        )

    async def test_keep_for_good_still_cleans_pings(self):
        server.rt.config.journal_keep_days = 0
        self.fill([("host_down", 40, 5), ("host_up", 10, 1),
                   ("new_mac", 4000, 3), ("alarm_raised", 900, 2)])
        with self.assertLogs("moonlan", "INFO"):
            await server.purge_journal()
        self.assertEqual(self.count("host_down"), 0)
        self.assertEqual(self.count("host_up"), 1)
        self.assertEqual(self.count("new_mac"), 3)
        self.assertEqual(self.count("alarm_raised"), 2)

    async def test_a_ping_term_of_zero_is_the_general_one(self):
        server.rt.config.journal_ping_keep_days = 0
        self.fill([("host_down", 40, 5), ("host_down", 400, 2)])
        with self.assertLogs("moonlan", "INFO"):
            await server.purge_journal()
        self.assertEqual(self.count("host_down"), 5)

    async def test_both_zero_deletes_nothing(self):
        server.rt.config.journal_keep_days = 0
        server.rt.config.journal_ping_keep_days = 0
        self.fill([("host_down", 4000, 5), ("new_mac", 4000, 3)])
        with self.assertLogs("moonlan", "INFO"):
            await server.purge_journal()
        self.assertEqual(self.count("host_down"), 5)
        self.assertEqual(self.count("new_mac"), 3)

    def test_sixty_thousand_rows_are_six_transactions(self):
        self.fill([("host_down", 40, 60_000), ("host_up", 5, 7),
                   ("new_mac", 40, 11)])
        commits = []
        conn = server.rt.db._conn
        conn.set_trace_callback(
            lambda sql: commits.append(sql) if sql.startswith("DELETE") else None
        )
        try:
            removed, batches = server.rt.db.purge_journal(
                ["host_down", "host_up"], time.time() - 30 * DAY
            )
        finally:
            conn.set_trace_callback(None)
        self.assertEqual((removed, batches), (60_000, 6))
        # one statement a batch, each in its own `with` — its own commit
        self.assertEqual(len(commits), 6)
        self.assertEqual(self.count("host_down"), 0)
        self.assertEqual(self.count("host_up"), 7)
        self.assertEqual(self.count("new_mac"), 11)


class DiagTest(unittest.TestCase):
    def test_config_prints_the_terms_and_the_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "moonlan.db"
            db = Database(path)
            db.add_event(time.time() - 3 * DAY, "host_down", "aa:bb:cc:00:00:01", "")
            db.add_event(time.time(), "new_mac", "aa:bb:cc:00:00:02", "")
            db.close()
            cfg = server.rt.config
            saved = cfg.db_path
            cfg.db_path = str(path)
            out = io.StringIO()
            try:
                with contextlib.redirect_stdout(out):
                    diag._print_journal(cfg)
            finally:
                cfg.db_path = saved
        text = out.getvalue()
        self.assertIn(f"journal_keep_days      = {cfg.journal_keep_days:g}", text)
        self.assertIn(f"journal_ping_keep_days = {cfg.journal_ping_keep_days:g}", text)
        self.assertIn("2 event(s), the oldest from", text)
        self.assertIn(
            time.strftime("%Y-%m-%d", time.localtime(time.time() - 3 * DAY)), text
        )
        self.assertIn("_print_journal(cfg)", Path(diag.__file__).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
