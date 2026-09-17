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


if __name__ == "__main__":
    unittest.main()
