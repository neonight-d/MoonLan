"""loop_detection_disabled looks at spanning tree (v0.8.6).

On mb0 the alarm was raised on 16.09 at 17:02 with "…this network runs
no spanning tree". RSTP was switched on soon after, the neighbours
confirmed mb0 as its root, and the alarm went on saying the same thing
for two months. The words about spanning tree were in the text and
nowhere in the condition.

Now: Loop Detection off and spanning tree running on that switch — no
alarm, and an active one is cleared; off and not running, or not
visible — the alarm, about that switch.

Run with:  python -m unittest discover -s tests
"""

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from service_fixture import SWITCHES  # noqa: E402

from moonlan import server  # noqa: E402
from moonlan.alarms import AlarmEngine  # noqa: E402
from moonlan.config import Config  # noqa: E402
from moonlan.db import Database  # noqa: E402
from moonlan.loopdetect import LoopDetectionData  # noqa: E402
from moonlan.notify import Notifier  # noqa: E402
from moonlan.snmp_collector import PortInfo, SwitchData  # noqa: E402
from moonlan.stp import StpData  # noqa: E402

MB0 = "10.0.0.10"


def lbd(enabled: bool) -> LoopDetectionData:
    return LoopDetectionData(supported=True, profile="dlink", enabled=enabled)


class EngineTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        cfg = Config()
        self.db = Database(":memory:")
        self.addCleanup(self.db.close)
        self.engine = AlarmEngine(self.db, Notifier(cfg), cfg.thresholds,
                                  cfg.notifications)

    def active(self) -> list[dict]:
        return [a for a in self.db.alarms(True, 10)
                if a["type"] == "loop_detection_disabled"]

    def events(self, event: str) -> list[dict]:
        return [e for e in self.db.journal(50) if e["event"] == event]

    async def test_stp_running_raises_nothing(self):
        await self.engine.on_loop_detection(
            MB0, "comm-mb0", lbd(False), {}, stp_operating=True
        )
        self.assertEqual(self.active(), [])

    async def test_stp_not_running_raises_it_about_this_switch(self):
        await self.engine.on_loop_detection(
            MB0, "comm-mb0", lbd(False), {}, stp_operating=False
        )
        (alarm,) = self.active()
        self.assertIn("spanning tree is not running on this switch",
                      alarm["message"])
        self.assertNotIn("this network", alarm["message"])

    async def test_stp_not_visible_says_so(self):
        await self.engine.on_loop_detection(
            MB0, "comm-mb0", lbd(False), {}, stp_operating=None
        )
        (alarm,) = self.active()
        self.assertIn("not visible over SNMP", alarm["message"])

    async def test_lbd_on_clears_it_as_before(self):
        await self.engine.on_loop_detection(
            MB0, "comm-mb0", lbd(False), {}, stp_operating=False
        )
        await self.engine.on_loop_detection(
            MB0, "comm-mb0", lbd(True), {}, stp_operating=False
        )
        self.assertEqual(self.active(), [])
        (cleared,) = self.events("alarm_cleared")
        self.assertIn("switched on again", cleared["details"])

    async def test_stp_started_clears_it_once(self):
        await self.engine.on_loop_detection(
            MB0, "comm-mb0", lbd(False), {}, stp_operating=False
        )
        self.assertEqual(len(self.active()), 1)
        for _ in range(3):
            await self.engine.on_loop_detection(
                MB0, "comm-mb0", lbd(False), {}, stp_operating=True
            )
        self.assertEqual(self.active(), [])
        cleared = self.events("alarm_cleared")
        self.assertEqual(len(cleared), 1)
        self.assertIn("spanning tree is running on this switch",
                      cleared[0]["details"])


class CountersCycleTest(unittest.IsolatedAsyncioTestCase):
    """The service hands the switch's verdict to the engine: the first
    counters cycle after an update clears the alarm on mb0."""

    def setUp(self):
        self.ip = SWITCHES[0]
        server.switch_data.clear()
        sw = SwitchData(ip=self.ip, reachable=True, sys_name="comm-mb0")
        sw.ports[1] = PortInfo(if_index=1, name="Slot0/1", oper_up=True)
        sw.stp = StpData(supported=True, operating=True, confirmed_root=True)
        sw.loop_detection = lbd(False)
        server.switch_data[self.ip] = sw
        self.sw = sw
        server.db._conn.execute("DELETE FROM alarms")
        server.db._conn.execute("DELETE FROM journal")
        server.db._conn.commit()
        self._collector = server.get_collector
        server.get_collector = lambda: _Silent()
        self._counters = server._counters_locked

        async def counters(collector, ip):
            # nothing measured, the loop state as the switch reports it
            return {}, {}, {}, server.switch_data[ip].loop_detection \
                if ip in server.switch_data else None

        server._counters_locked = counters

    def tearDown(self):
        server.get_collector = self._collector
        server._counters_locked = self._counters
        server.switch_data.clear()
        server.alarm_engine._active.discard(
            ("loop_detection_disabled", self.ip)
        )

    async def test_the_first_cycle_clears_it(self):
        # the alarm as an older version left it, active since September
        server.db.raise_alarm(
            "loop_detection_disabled", self.ip, "info",
            "comm-mb0: Loop Detection is switched off globally, and this "
            "network runs no spanning tree — nothing is watching for loops "
            "on this switch", time.time() - 60 * 86400,
        )
        server.alarm_engine._active.add(("loop_detection_disabled", self.ip))
        await server.run_counters()
        active = [a for a in server.db.alarms(True, 50)
                  if a["type"] == "loop_detection_disabled"]
        self.assertEqual(active, [])
        cleared = [e for e in server.db.journal(50)
                   if e["event"] == "alarm_cleared"]
        self.assertEqual(len(cleared), 1)
        # …and the next cycle raises nothing new while the tree runs
        await server.run_counters()
        self.assertEqual(
            [a for a in server.db.alarms(True, 50)
             if a["type"] == "loop_detection_disabled"], [],
        )

    async def test_stp_stopped_raises_it(self):
        self.sw.stp.operating = False
        await server.run_counters()
        (alarm,) = [a for a in server.db.alarms(True, 50)
                    if a["type"] == "loop_detection_disabled"]
        self.assertIn("not running on this switch", alarm["message"])


class _Silent:
    def begin_scan_cycle(self) -> None:
        pass


if __name__ == "__main__":
    unittest.main()
