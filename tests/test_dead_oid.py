"""An OID that answers nothing but timeouts stops being asked for.

`1.0.8802.1.1.2.1.4.2.1.3` — the LLDP management-address table — comes
back from both RouterOS boxes as zero rows after the full retry budget,
cycle after cycle. The other thirteen walks on the same device get
through, so this is not a broken link: the agent does not implement the
table and cannot say so. An agent that can say so answers noSuchObject,
which costs nothing.

Two things the rule must not do. It must not fire on anything other
than "no rows AND a timeout" — a partial answer is picked back up
(v0.6.5) and an honest noSuchObject is cheap. And it must not be quiet
about itself: a gap in the data because MoonLan stopped asking is a
different fact from a gap because the device has nothing, and only the
log can tell them apart.

Run with:  python -m unittest discover -s tests
"""

import asyncio
import unittest

from moonlan.snmp_collector import SnmpCollector

BASE = "1.0.8802.1.1.2.1.4.2.1.3"
STRIKES = 3
COOLDOWN = 30


class _Oid(tuple):
    def __new__(cls, text: str):
        return super().__new__(cls, (int(p) for p in text.split(".")))


class _Closeable:
    def __init__(self, gen):
        self._gen = gen

    def __aiter__(self):
        return self._gen.__aiter__()

    async def aclose(self):
        await self._gen.aclose()


class StubCollector(SnmpCollector):
    """The real _walk over an agent that can be told how to fail."""

    def __init__(self, strikes: int = STRIKES, cooldown: int = COOLDOWN):
        self._walk_status = {}
        self._retries_on_break = 0
        self._per_host = {}
        self._communities = {}
        self._dead_oids = {}
        self._cycle = 0
        self._dead_oid_strikes = strikes
        self._dead_oid_cooldown = cooldown
        self.asked: list[str] = []
        self.mode = "timeout"  # or "rows", "partial", "no_such"

    async def _open_walk(self, host, start):
        self.asked.append(start)
        mode = self.mode

        async def rows():
            if mode == "timeout":
                yield "No SNMP response received before timeout", None, None, []
                return
            if mode == "partial":
                yield None, None, None, [(_Oid(f"{BASE}.1"), 1)]
                yield "No SNMP response received before timeout", None, None, []
                return
            if mode == "rows":
                for index in (1, 2):
                    yield None, None, None, [(_Oid(f"{BASE}.{index}"), index)]
                return
            # a table that ends: the walk leaves the subtree
            yield None, None, None, [(_Oid("1.0.8802.1.1.2.1.5.1.0"), 0)]

        return _Closeable(rows())


def _walk(collector, host="10.3.6.2", oid=BASE):
    async def run():
        return [pair async for pair in collector._walk(host, oid)]

    return asyncio.run(run())


class DeadOidTest(unittest.TestCase):
    def setUp(self):
        self.collector = StubCollector()

    def test_three_strikes_then_the_oid_is_left_alone(self):
        with self.assertLogs("moonlan.snmp_collector", "WARNING") as logged:
            for _ in range(STRIKES):
                self.collector.begin_scan_cycle()
                self.assertEqual(_walk(self.collector), [])
        self.assertEqual(len(self.collector.asked), STRIKES)
        self.assertTrue(
            any("not asking for it again" in line for line in logged.output),
            logged.output,
        )

        # from here on nothing is sent, however many cycles run
        for _ in range(COOLDOWN - 1):
            self.collector.begin_scan_cycle()
            self.assertEqual(_walk(self.collector), [])
        self.assertEqual(len(self.collector.asked), STRIKES)

        # …and the caller is told "no answer", not "an empty table"
        status = self.collector.last_walk_status("10.3.6.2", BASE)
        self.assertFalse(status.complete)
        self.assertIn("not asked", status.error)

        # one cycle later the cooldown is over and it is tried again
        self.collector.begin_scan_cycle()
        self.collector.mode = "rows"
        with self.assertLogs("moonlan.snmp_collector", "INFO") as logged:
            self.assertEqual(len(_walk(self.collector)), 2)
        self.assertEqual(len(self.collector.asked), STRIKES + 1)
        self.assertTrue(
            any("asking for" in line for line in logged.output), logged.output
        )
        self.assertEqual(self.collector.paused_oids(), [])

    def test_paused_oids_counts_down(self):
        for _ in range(STRIKES):
            self.collector.begin_scan_cycle()
            with self.assertLogs("moonlan.snmp_collector", "WARNING"):
                _walk(self.collector)
        paused = self.collector.paused_oids()
        self.assertEqual(len(paused), 1)
        self.assertEqual(paused[0]["host"], "10.3.6.2")
        self.assertEqual(paused[0]["oid"], BASE)
        self.assertEqual(paused[0]["cycles_left"], COOLDOWN)
        self.collector.begin_scan_cycle()
        self.assertEqual(
            self.collector.paused_oids()[0]["cycles_left"], COOLDOWN - 1
        )

    def test_a_partial_answer_is_not_a_dead_oid(self):
        """Rows arrived. That is the case retries_on_break exists for."""
        self.collector.mode = "partial"
        for _ in range(STRIKES + 2):
            self.collector.begin_scan_cycle()
            with self.assertLogs("moonlan.snmp_collector", "WARNING"):
                self.assertEqual(len(_walk(self.collector)), 1)
        self.assertEqual(len(self.collector.asked), STRIKES + 2)
        self.assertEqual(self.collector.paused_oids(), [])

    def test_an_empty_table_without_a_timeout_is_not_a_dead_oid(self):
        """The agent answered. "Nothing here" is an answer, and cheap."""
        self.collector.mode = "no_such"
        for _ in range(STRIKES + 2):
            self.collector.begin_scan_cycle()
            self.assertEqual(_walk(self.collector), [])
        self.assertEqual(len(self.collector.asked), STRIKES + 2)
        self.assertEqual(self.collector.paused_oids(), [])

    def test_one_good_answer_resets_the_count(self):
        for _ in range(STRIKES - 1):
            self.collector.begin_scan_cycle()
            with self.assertLogs("moonlan.snmp_collector", "WARNING"):
                _walk(self.collector)
        self.collector.mode = "rows"
        self.collector.begin_scan_cycle()
        _walk(self.collector)
        self.collector.mode = "timeout"
        for _ in range(STRIKES - 1):
            self.collector.begin_scan_cycle()
            with self.assertLogs("moonlan.snmp_collector", "WARNING"):
                _walk(self.collector)
        self.assertEqual(self.collector.paused_oids(), [])

    def test_the_rule_can_be_switched_off(self):
        collector = StubCollector(strikes=0)
        for _ in range(10):
            collector.begin_scan_cycle()
            with self.assertLogs("moonlan.snmp_collector", "WARNING"):
                _walk(collector)
        self.assertEqual(len(collector.asked), 10)
        self.assertEqual(collector.paused_oids(), [])


if __name__ == "__main__":
    unittest.main()
