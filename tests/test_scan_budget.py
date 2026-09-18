"""One slow agent delays itself, not the whole map.

`return_exceptions=True` (v0.6.6) protects a scan from a switch that
raises. Nothing protected it from a switch that answers, slowly: every
single request stayed inside `snmp.timeout`, the poll as a whole took
eight minutes, and `gather` waited for it. Worse, `run_scan` returns at
once while another scan is running — so the next cycle was skipped too,
and with two such devices in `switches:` the map stopped updating at
all.

The budget is what bounds the sum. A switch that runs past it is left
out of this one scan; it is not reported as unreachable, because it is
not — it answers, only too slowly, and a `switch_down` alarm would send
somebody to look for a dead device that is alive.

Run with:  python -m unittest discover -s tests
"""

import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path

# The service reads its configuration at import time, so the temporary
# one has to exist before moonlan.server is imported.
_TMP = tempfile.TemporaryDirectory()
_CONFIG = Path(_TMP.name) / "config.yaml"
_CONFIG.write_text(
    "db_path: " + str(Path(_TMP.name) / "test.db") + "\n"
    "scan_interval_minutes: 0\n"
    "snmp:\n"
    "  host_budget_seconds: 1\n"
    "switches:\n"
    + "".join(f"  - 10.0.0.{n}\n" for n in range(1, 9)),
    encoding="utf-8",
)
os.environ["MOONLAN_CONFIG"] = str(_CONFIG)

from moonlan import server  # noqa: E402
from moonlan.config import Config, HostSnmp  # noqa: E402
from moonlan.snmp_collector import SwitchData  # noqa: E402

SLOW = {"10.0.0.3", "10.0.0.7"}


class StubCollector:
    """Eight switches, two of which answer long after anyone is
    listening."""

    def __init__(self, delay: float = 30.0):
        self.delay = delay
        self.finished: list[str] = []
        self.cycles = 0

    def begin_scan_cycle(self) -> None:
        self.cycles += 1

    async def collect(self, host: str) -> SwitchData:
        if host in SLOW:
            await asyncio.sleep(self.delay)
        self.finished.append(host)
        return SwitchData(
            ip=host, reachable=True, sys_name=f"sw{host[-1]}",
            polled_at=time.time(),
        )


class ScanBudgetTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.collector = StubCollector()
        self._get_collector = server.get_collector
        server.get_collector = lambda: self.collector
        server.switch_data.clear()

    def tearDown(self):
        server.get_collector = self._get_collector
        server.state.scanning = False

    async def test_scan_finishes_on_budget_without_the_slow_two(self):
        started = time.monotonic()
        with self.assertLogs("moonlan", "WARNING") as logged:
            await server.run_scan()
        elapsed = time.monotonic() - started

        # the budget, not the slow switches, decides when a scan ends
        self.assertLess(elapsed, 10.0)
        self.assertEqual(sorted(self.collector.finished), [
            f"10.0.0.{n}" for n in (1, 2, 4, 5, 6, 8)
        ])
        # six switches on the map, and it is a fresh map
        drawn = {sw["ip"] for sw in server.state.as_dict()["switches"]}
        self.assertEqual(drawn, {f"10.0.0.{n}" for n in (1, 2, 4, 5, 6, 8)})
        # one line per switch that ran out of budget, naming it
        over = [
            line for line in logged.output
            if "did not finish its poll within" in line
        ]
        self.assertEqual(len(over), 2)
        self.assertTrue(any("10.0.0.3" in line for line in over))
        self.assertTrue(any("10.0.0.7" in line for line in over))
        # …and the interface can say the same thing without journalctl
        progress = server.state.scan_progress()
        self.assertFalse(progress["scanning"])
        self.assertEqual(progress["scan_total"], 8)
        self.assertEqual(progress["scan_done"], 8)
        self.assertEqual(
            sorted(progress["scan_over_budget"]), ["10.0.0.3", "10.0.0.7"]
        )

    async def test_over_budget_is_not_switch_down(self):
        """The alarm engine hears nothing about a switch we gave up on.

        Not "it answered" and not "it missed a poll": we stopped
        asking, and that is a fact about MoonLan, not about the switch.
        """
        seen: list[dict] = []

        async def record_scan(reachable, names):
            seen.append(dict(reachable))

        original = server.alarm_engine.on_scan
        server.alarm_engine.on_scan = record_scan
        try:
            with self.assertLogs("moonlan", "WARNING"):
                await server.run_scan()
        finally:
            server.alarm_engine.on_scan = original
        self.assertEqual(len(seen), 1)
        self.assertNotIn("10.0.0.3", seen[0])
        self.assertNotIn("10.0.0.7", seen[0])
        self.assertTrue(all(seen[0].values()))

    async def test_last_reading_of_a_slow_switch_is_kept_and_dated(self):
        """A switch that misses its budget keeps the map it last drew.

        Dropping it would take its whole branch off the map every
        cycle; keeping it silently would date the old reading as if it
        were new. It stays, marked, with the time it was taken.
        """
        self.collector.delay = 0.0
        with self.assertLogs("moonlan", "WARNING") as logged:
            await server.run_scan()
        self.assertFalse([
            line for line in logged.output
            if "did not finish its poll within" in line
        ])
        first = {
            sw["ip"]: sw for sw in server.state.as_dict()["switches"]
        }
        self.assertFalse(first["10.0.0.3"]["over_budget"])
        taken_at = first["10.0.0.3"]["polled_at"]
        self.assertGreater(taken_at, 0.0)

        self.collector.delay = 30.0
        with self.assertLogs("moonlan", "WARNING"):
            await server.run_scan()
        second = {
            sw["ip"]: sw for sw in server.state.as_dict()["switches"]
        }
        self.assertEqual(len(second), 8)
        self.assertTrue(second["10.0.0.3"]["over_budget"])
        self.assertEqual(second["10.0.0.3"]["polled_at"], taken_at)
        self.assertFalse(second["10.0.0.1"]["over_budget"])


class StaleSwitchTest(unittest.IsolatedAsyncioTestCase):
    """A switch that answers and never finishes answering.

    10.3.7.10 was not polled in full once in six hours: thirty scans,
    thirty budget failures. On the map it looked alive — from its last
    complete reading — and the only way to learn otherwise was the
    journal. That is a third state beside "answering" and "down", and
    calling it either of those is wrong in a different direction each
    time.
    """

    def setUp(self):
        self.collector = StubCollector()
        self._get_collector = server.get_collector
        server.get_collector = lambda: self.collector
        server.switch_data.clear()
        self._scans = server.config.stale_switch_scans
        server.config.stale_switch_scans = 3
        self.raised: list[tuple[str, str, str]] = []
        self._raise = server.alarm_engine._raise
        self._clear = server.alarm_engine._clear

        # switch_stale only: every other alarm path runs too, and the
        # point here is what this one says
        async def record_raise(alarm_type, subject, message, **kwargs):
            if alarm_type == "switch_stale":
                self.raised.append(("raise", subject, message))

        async def record_clear(alarm_type, subject, message, note=""):
            if alarm_type == "switch_stale":
                self.raised.append(("clear", subject, message))

        server.alarm_engine._raise = record_raise
        server.alarm_engine._clear = record_clear
        server.alarm_engine._stale_switches.clear()

    def tearDown(self):
        server.get_collector = self._get_collector
        server.alarm_engine._raise = self._raise
        server.alarm_engine._clear = self._clear
        server.config.stale_switch_scans = self._scans
        server.state.scanning = False

    async def test_the_streak_is_counted_and_then_said_out_loud(self):
        self.collector.delay = 0.0
        with self.assertLogs("moonlan", "WARNING"):
            await server.run_scan()          # one good scan for a baseline
        self.collector.delay = 30.0
        for _ in range(2):
            with self.assertLogs("moonlan", "WARNING"):
                await server.run_scan()
        drawn = {sw["ip"]: sw for sw in server.state.as_dict()["switches"]}
        self.assertEqual(drawn["10.0.0.3"]["over_budget_scans"], 2)
        # …but two is under the threshold, so nothing has been claimed
        self.assertEqual(self.raised, [])

        with self.assertLogs("moonlan", "WARNING"):
            await server.run_scan()
        raised = [r for r in self.raised if r[0] == "raise"]
        self.assertEqual(
            sorted(r[1] for r in raised), ["10.0.0.3", "10.0.0.7"]
        )
        self.assertIn("3 scans running", raised[0][2])
        # it is answering: that must be said in the same breath
        self.assertIn("reachable", raised[0][2])

    async def test_one_full_poll_clears_it(self):
        self.collector.delay = 30.0
        for _ in range(3):
            with self.assertLogs("moonlan", "WARNING"):
                await server.run_scan()
        self.assertTrue([r for r in self.raised if r[0] == "raise"])
        self.raised.clear()

        self.collector.delay = 0.0
        with self.assertLogs("moonlan", "WARNING"):
            await server.run_scan()
        cleared = [r for r in self.raised if r[0] == "clear"]
        self.assertEqual(
            sorted(r[1] for r in cleared), ["10.0.0.3", "10.0.0.7"]
        )
        drawn = {sw["ip"]: sw for sw in server.state.as_dict()["switches"]}
        self.assertEqual(drawn["10.0.0.3"]["over_budget_scans"], 0)
        self.assertFalse(drawn["10.0.0.3"]["over_budget"])
        self.assertGreater(drawn["10.0.0.3"]["polled_at"], 0)


class CountersStarvationTest(unittest.IsolatedAsyncioTestCase):
    """A scan holding a switch must not cost it every counters cycle.

    The counters cycle used to give up the instant it found the lock
    taken. A scan holds a host for as long as its budget allows, so any
    budget of two intervals or more guaranteed that host missed cycles
    — and before v0.6.13 a missed cycle meant its whole panel went to
    dashes. The operator walked straight into it: budget 180 against an
    interval of 60.
    """

    def setUp(self):
        self._interval = server.config.counters_interval_seconds
        server.config.counters_interval_seconds = 4  # wait budget: 2 s

    def tearDown(self):
        server.config.counters_interval_seconds = self._interval

    async def test_it_waits_for_a_scan_that_is_nearly_done(self):
        polled: list[str] = []

        async def fake_samples(collector, ip, expected):
            polled.append(ip)
            return {}, {}, {}

        original = server.counters.collect_samples
        server.counters.collect_samples = fake_samples
        lock = server.host_lock("10.0.0.5")

        async def hold():
            async with lock:
                await asyncio.sleep(0.3)

        try:
            holder = asyncio.create_task(hold())
            await asyncio.sleep(0)  # let it take the lock
            self.assertTrue(lock.locked())
            await server._counters_locked(None, "10.0.0.5")
            await holder
        finally:
            server.counters.collect_samples = original
        self.assertEqual(polled, ["10.0.0.5"])

    async def test_it_gives_up_rather_than_queueing(self):
        lock = server.host_lock("10.0.0.6")

        async def hold():
            async with lock:
                await asyncio.sleep(5)

        holder = asyncio.create_task(hold())
        await asyncio.sleep(0)
        started = time.monotonic()
        with self.assertLogs("moonlan", "INFO") as logged:
            result = await server._counters_locked(None, "10.0.0.6")
        elapsed = time.monotonic() - started
        holder.cancel()
        self.assertEqual(result, ({}, {}, {}, None))
        self.assertLess(elapsed, 4.0)
        self.assertGreater(elapsed, 1.0)  # it did wait
        self.assertTrue(
            any("still busy with the topology scan" in line
                for line in logged.output),
            logged.output,
        )

    def test_a_budget_that_outlasts_two_cycles_is_reported(self):
        cfg = Config()
        cfg.counters_interval_seconds = 60
        cfg.switches = ["10.0.0.10", "10.3.6.2"]
        cfg.switch_snmp = {
            "10.0.0.10": HostSnmp(
                community="c", timeout=5, retries=2, retries_on_break=2,
                host_budget_seconds=90,
            ),
            "10.3.6.2": HostSnmp(
                community="c", timeout=2, retries=1, retries_on_break=2,
                host_budget_seconds=180,
                explicit=frozenset({"host_budget_seconds"}),
            ),
        }
        self.assertEqual(cfg.starved_counters(), [("10.3.6.2", 180)])
        cfg.counters_interval_seconds = 0  # counters cycle switched off
        self.assertEqual(cfg.starved_counters(), [])


if __name__ == "__main__":
    unittest.main()
