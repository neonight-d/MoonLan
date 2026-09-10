"""Counter columns: what came back, what did not, and what fills the gap.

Three behaviours the ports panel depends on:

- a column that answers in part is reported as such, not as silence;
- the ports a truncated walk never reached are fetched one GET at a
  time from the 32-bit column, marked as 32-bit so wraparound is
  handled;
- a packet column that answers for every port with zero while the
  octet counters are in the terabytes is firmware that implements the
  column without filling it, and the 32-bit column is read instead.
  That rule is for packets only: on mb0 a column of zeros for errors
  is the honest answer.

Run with:  python -m unittest discover -s tests
"""

import asyncio
import unittest

from moonlan import counters
from moonlan.counters import ColumnStatus, collect_samples
from moonlan.snmp_collector import WalkStatus

HC_IN = "1.3.6.1.2.1.31.1.1.1.6"
HC_OUT = "1.3.6.1.2.1.31.1.1.1.10"
IN32 = "1.3.6.1.2.1.2.2.1.10"
OUT32 = "1.3.6.1.2.1.2.2.1.16"
HC_IN_PKTS = "1.3.6.1.2.1.31.1.1.1.7"
IN32_PKTS = "1.3.6.1.2.1.2.2.1.11"


class FakeCollector:
    """Serves canned tables; records every GET it is asked for."""

    def __init__(self, tables: dict[str, dict[int, int]],
                 truncated: dict[str, int] | None = None):
        self.tables = tables
        self.truncated = truncated or {}
        self.gets: list[str] = []

    async def _walk(self, host, oid):
        for if_index, value in sorted(self.tables.get(oid, {}).items()):
            yield (if_index,), value

    def last_walk_status(self, host, oid):
        rows = len(self.tables.get(oid, {}))
        stop = self.truncated.get(oid)
        if stop is None:
            return WalkStatus(oid=oid, rows=rows)
        return WalkStatus(
            oid=oid, rows=rows, error="No SNMP response received before "
            "timeout", truncated=True, last_oid=f"{oid}.{stop}",
        )

    async def _get(self, host, oid):
        self.gets.append(oid)
        base, _, index = oid.rpartition(".")
        return self.tables.get(base, {}).get(int(index))


class ColumnVerdictTest(unittest.TestCase):
    def test_four_verdicts_read_differently(self):
        self.assertEqual(ColumnStatus(oid="x", rows=36).verdict(), "36 row(s)")
        self.assertEqual(
            ColumnStatus(oid="x").verdict(),
            "no rows — the agent does not implement this column",
        )
        self.assertTrue(
            ColumnStatus(oid="x", error="timeout").verdict()
            .startswith("NO ANSWER")
        )
        partial = ColumnStatus(
            oid="x", rows=24, truncated=True, last_oid="x.24", error="timeout"
        ).verdict()
        self.assertIn("24 row(s)", partial)
        self.assertIn("x.24", partial)
        self.assertNotIn("NO ANSWER", partial)


class GapFillTest(unittest.TestCase):
    def test_truncated_column_is_filled_per_port(self):
        """The four ports the walk never reached come from the 32-bit column."""
        collector = FakeCollector(
            tables={
                HC_IN: {i: i * 1000 for i in range(1, 25)},
                HC_OUT: {i: i * 2000 for i in range(1, 29)},
                IN32: {i: i * 7 for i in range(1, 29)},
            },
            truncated={HC_IN: 24},
        )
        samples, _oper, columns = asyncio.run(
            collect_samples(collector, "10.0.0.21", set(range(1, 29)))
        )
        self.assertEqual(columns["in_octets"].gaps_filled, 4)
        self.assertEqual(columns["in_octets"].filled_by, IN32)
        self.assertEqual(
            collector.gets, [f"{IN32}.{i}" for i in range(25, 29)]
        )
        # the rescued ports carry 32-bit values and are marked as such
        for if_index in range(25, 29):
            self.assertEqual(samples[if_index].in_octets, if_index * 7)
            self.assertFalse(samples[if_index].hc_in)
        # and the ports the walk did reach are untouched 64-bit values
        self.assertEqual(samples[1].in_octets, 1000)
        self.assertTrue(samples[1].hc_in)

    def test_complete_column_asks_for_nothing(self):
        collector = FakeCollector(
            tables={HC_IN: {i: i for i in range(1, 29)},
                    HC_OUT: {i: i for i in range(1, 29)}}
        )
        asyncio.run(
            collect_samples(collector, "10.0.0.21", set(range(1, 29)))
        )
        self.assertEqual(collector.gets, [])


class UnfilledPacketColumnTest(unittest.TestCase):
    """mb0 answers ifHCInUcastPkts for 26 ports, every value zero."""

    def _poll(self, packets, octets=None):
        # the real numbers from mb0 Slot0/21: terabytes of octets next
        # to a packet counter that never moved
        busy = {1: 9_137_720_039_106}
        collector = FakeCollector(tables={
            HC_IN: busy if octets is None else octets,
            HC_OUT: busy if octets is None else octets,
            HC_IN_PKTS: packets,
            IN32_PKTS: {1: 4242},
        })
        samples, _oper, _columns = asyncio.run(
            collect_samples(collector, "10.0.0.10", {1})
        )
        return samples[1]

    def test_zero_packets_beside_terabytes_are_read_again(self):
        self.assertEqual(self._poll({1: 0}).in_pkts, 4242)

    def test_a_quiet_switch_is_left_alone(self):
        """No octets either: the zeros are the truth, not a gap."""
        self.assertEqual(self._poll({1: 0}, octets={1: 0}).in_pkts, 0)

    def test_a_filled_column_is_left_alone(self):
        self.assertEqual(self._poll({1: 99}).in_pkts, 99)


if __name__ == "__main__":
    unittest.main()
