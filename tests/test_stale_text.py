"""The text of switch_stale stays true while it stands (v0.8.8).

The message was put together once, at the raise, and `_raise` leaves
an active alarm alone. A switch whose alarm went up with "the data on
the map is from 2026-11-17" kept those words after a restart took that
reading off the map and left an empty node; "in N scans running" stood
still at the threshold. The same mistake as loop_detection_disabled in
v0.8.6.

Now the text has only what stays true — answered or not known, what
the map shows (this run's last full poll, the reading saved before the
restart, or an empty node since the start) and since when — and it is
worked out every scan and brought up to date in place: one row in
alarms, no second notification, no journal entry.

Run with:  python -m unittest discover -s tests
"""

import logging
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import service_fixture  # noqa: E402,F401  (a config for server)

from moonlan import server  # noqa: E402
from moonlan.alarms import AlarmEngine, stale_switch_message  # noqa: E402
from moonlan.config import Config  # noqa: E402
from moonlan.db import Database  # noqa: E402
from moonlan.snmp_collector import STATE_FRESH, SwitchData  # noqa: E402

IP = "10.0.0.26"
NAME = "annex-sw"
STARTED = time.mktime((2026, 11, 24, 9, 0, 0, 0, 0, -1))
POLLED = time.mktime((2026, 11, 17, 14, 5, 0, 0, 0, -1))


class Notifier:
    def __init__(self):
        self.sent = []

    async def notify(self, alarm_type, subject, severity, message, **_kwargs):
        self.sent.append((alarm_type, subject, message))

    def __getattr__(self, name):
        async def anything(*args, **kwargs):
            self.sent.append((name, args))
        return anything


def info(scans, polled_at=POLLED, source="run", answered=True):
    return {"scans": scans, "polled_at": polled_at, "source": source,
            "started_at": STARTED, "answered": answered}


class TextTest(unittest.TestCase):
    def test_this_runs_reading(self):
        text = stale_switch_message(NAME, info(5))
        self.assertEqual(
            text,
            f"{NAME} has answered but not finished a full poll since "
            "2026-11-17 14:05; the map shows that poll, made by this run "
            "of the service. It is reachable — this is a poll budget too "
            "small for it, not a switch that is down",
        )

    def test_the_reading_saved_before_the_restart(self):
        text = stale_switch_message(NAME, info(5, source="saved"))
        self.assertIn("since 2026-11-17 14:05; the map shows the reading of "
                      "that poll saved before the restart", text)

    def test_an_empty_node(self):
        text = stale_switch_message(NAME, info(5, polled_at=0))
        self.assertIn("since the service started at 2026-11-24 09:00; the "
                      "map shows an empty node", text)

    def test_not_known_whether_it_answers(self):
        text = stale_switch_message(NAME, info(5, polled_at=0, answered=None))
        self.assertTrue(text.startswith(f"{NAME} has not finished a full poll"))
        self.assertIn("whether it is reachable is not known", text)
        self.assertNotIn("It is reachable", text)

    def test_the_count_of_scans_is_not_in_it(self):
        self.assertEqual(stale_switch_message(NAME, info(5)),
                         stale_switch_message(NAME, info(41)))


class EngineTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cfg = Config()
        self.db = Database(":memory:")
        self.addCleanup(self.db.close)
        self.notifier = Notifier()
        self.engine = self.new_engine()

    def new_engine(self):
        return AlarmEngine(self.db, self.notifier, self.cfg.thresholds,
                           self.cfg.notifications)

    def rows(self):
        return [a for a in self.db.alarms(True, 50) if a["type"] == "switch_stale"]

    def raised_events(self):
        return [e for e in self.db.journal(50) if e["event"] == "alarm_raised"]

    async def stale(self, engine, entry, threshold=5):
        await engine.on_stale_switches({IP: entry}, {IP: NAME}, threshold=threshold)

    async def test_the_reading_goes_and_the_text_follows(self):
        with self.assertLogs("moonlan", logging.WARNING):
            await self.stale(self.engine, info(5))
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertIn("since 2026-11-17 14:05", self.rows()[0]["message"])

        with self.assertLogs("moonlan", logging.INFO) as logs:
            await self.stale(self.engine, info(6, polled_at=0))
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertIn("the map shows an empty node", rows[0]["message"])
        self.assertNotIn("2026-11-17", rows[0]["message"])
        # no second notification, and the journal keeps the raise as it was
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertEqual(len(self.raised_events()), 1)
        self.assertIn("since 2026-11-17 14:05", self.raised_events()[0]["details"])
        self.assertEqual(
            [r.getMessage() for r in logs.records],
            [f"Alarm text brought up to date: switch_stale {IP} — {rows[0]['message']}"],
        )

    async def test_more_scans_change_nothing(self):
        with self.assertLogs("moonlan", logging.WARNING):
            await self.stale(self.engine, info(5))
        message = self.rows()[0]["message"]
        logger = logging.getLogger("moonlan")
        with self.assertNoLogs(logger, logging.INFO):
            for scans in (6, 7, 30):
                await self.stale(self.engine, info(scans))
        self.assertEqual(self.rows()[0]["message"], message)

    async def test_below_the_threshold_nothing_is_raised(self):
        await self.stale(self.engine, info(4))
        self.assertEqual(self.rows(), [])

    async def test_after_a_restart_the_first_scan_brings_it_up_to_date(self):
        """The case the version was written for."""
        with self.assertLogs("moonlan", logging.WARNING):
            await self.stale(self.engine, info(41))
        # a restart: a new engine over the same database, the streak at 1
        engine = self.new_engine()
        with self.assertLogs("moonlan", logging.INFO):
            await engine.load()
        with self.assertLogs("moonlan", logging.INFO):
            await self.stale(engine, info(1, polled_at=0))
        self.assertIn("since the service started at 2026-11-24 09:00",
                      self.rows()[0]["message"])
        self.assertEqual(len(self.notifier.sent), 1)

        # …and a full poll after a restart clears it
        await engine.on_stale_switches({}, {IP: NAME}, threshold=5)
        self.assertEqual(self.rows(), [])
        cleared = [a for a in self.db.alarms(False, 50) if a["type"] == "switch_stale"]
        self.assertTrue(cleared and cleared[0]["ts_cleared"])


class ClearWordsTest(unittest.IsolatedAsyncioTestCase):
    """switch_stale goes with words that say what happened (v0.8.9)."""

    def setUp(self):
        cfg = Config()
        self.db = Database(":memory:")
        self.addCleanup(self.db.close)
        self.notifier = Notifier()
        self.engine = AlarmEngine(self.db, self.notifier, cfg.thresholds,
                                  cfg.notifications)

    async def clear_with(self, states):
        with self.assertLogs("moonlan", logging.WARNING):
            await self.engine.on_stale_switches({IP: info(5)}, {IP: NAME}, threshold=5)
        with self.assertLogs("moonlan", logging.INFO):
            await self.engine.on_stale_switches({}, {IP: NAME}, threshold=5, states=states)
        self.assertEqual([a for a in self.db.alarms(True, 50) if a["type"] == "switch_stale"], [])
        cleared = [e["details"] for e in self.db.journal(50) if e["event"] == "alarm_cleared"]
        self.assertEqual(len(cleared), 1)
        return cleared[0]

    async def test_a_full_poll(self):
        words = await self.clear_with({IP: {"state": "fresh", "answered": True}})
        self.assertEqual(words, "warning switch_stale: a full poll finished again")

    async def test_it_stopped_answering(self):
        words = await self.clear_with({IP: {"state": "unreachable", "answered": False}})
        self.assertEqual(
            words,
            "warning switch_stale: it stopped answering — switch_down follows "
            "it from the second missed poll",
        )

    async def test_never_read_and_silent(self):
        words = await self.clear_with({IP: {"state": "never", "answered": False}})
        self.assertIn("it stopped answering", words)

    async def test_no_longer_configured(self):
        words = await self.clear_with({})
        self.assertEqual(words, "warning switch_stale: no longer in switches:")
        cleared = [n for n in self.notifier.sent if n[0] == "switch_stale"]
        self.assertTrue(cleared)


class ProvenanceTest(unittest.TestCase):
    def tearDown(self):
        server.rt.readings_from_database.discard(IP)
        server.rt.switch_data.pop(IP, None)

    def test_a_full_poll_ends_the_saved_reading(self):
        server.rt.readings_from_database.add(IP)
        result = SwitchData(ip=IP, reachable=True)
        settled = server._settle(IP, result, time.time())
        self.assertEqual(settled.state, STATE_FRESH)
        self.assertNotIn(IP, server.rt.readings_from_database)


if __name__ == "__main__":
    unittest.main()
