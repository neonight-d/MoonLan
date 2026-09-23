"""An aggregate ifIndex that is not an interface is not an aggregate.

An Edge-Core ES3528M answers dot3adAggPortAttachedAggID with
-402792706 on every one of its 28 ports. The old test — non-zero and
not the port itself — passed, so all 28 became members of one
aggregate carrying that number as its ifIndex: the ports panel showed
28 rows with the same ifIndex, the map grew a trunk that does not
exist, and a lag_degraded alarm sat on it for 66 hours.

Same rule the counters learned in v0.6.6, in a different place: a
number that is not in the interface table is not an interface.

Run with:  python -m unittest discover -s tests
"""

import asyncio
import unittest

from moonlan.snmp_collector import (
    OID_IF_DESCR,
    OID_IF_NAME,
    OID_IF_OPER_STATUS,
    OID_IF_TYPE,
    OID_LAG_ATTACHED_ID,
    OID_SYS_NAME,
    PortInfo,
    SnmpCollector,
    SwitchData,
    WalkStatus,
    sane_lag_members,
)

PORTS = 28
PHANTOM = -402792706
AGGREGATE = 1000


class StubAgent(SnmpCollector):
    """A switch with `PORTS` ports and a LAG column under test."""

    def __init__(self, lag_column: dict[int, int], extra_ports=()):
        self._engine = None
        self._community = None
        self._walk_status = {}
        self._retries_on_break = 0
        self._per_host = {}
        self._dead_oids = {}
        self._cycle = 0
        self._dead_oid_strikes = 0
        self._dead_oid_cooldown = 30
        self._communities = {}
        self._lag = lag_column
        self._extra = dict(extra_ports)

    async def _target(self, host):
        return None

    async def _get(self, host, oid):
        return "es3528m" if oid == OID_SYS_NAME else None

    async def _walk(self, host, oid):
        rows: dict[int, object] = {}
        if oid == OID_IF_DESCR:
            rows = {i: f"Port{i}" for i in range(1, PORTS + 1)}
            rows.update(self._extra)
        elif oid == OID_IF_NAME:
            rows = {i: f"Port{i}" for i in range(1, PORTS + 1)}
            rows.update(self._extra)
        elif oid == OID_IF_TYPE:
            rows = {i: 6 for i in range(1, PORTS + 1)}
            rows.update({i: 161 for i in self._extra})  # ieee8023adLag
        elif oid == OID_IF_OPER_STATUS:
            rows = {i: 1 for i in range(1, PORTS + 1)}
            rows.update({i: 1 for i in self._extra})
        elif oid == OID_LAG_ATTACHED_ID:
            rows = dict(self._lag)
        self._walk_status[(host, oid)] = WalkStatus(oid=oid, rows=len(rows))
        for index in sorted(rows):
            yield (index,), rows[index]

    def last_walk_status(self, host, oid):
        return self._walk_status.get((host, oid)) or WalkStatus(oid=oid)


class PhantomAggregateTest(unittest.TestCase):
    def test_a_whole_switch_in_one_aggregate_is_dropped(self):
        agent = StubAgent({i: PHANTOM for i in range(1, PORTS + 1)})
        with self.assertLogs("moonlan.snmp_collector", "WARNING") as logged:
            data = asyncio.run(agent.collect("10.3.6.5"))
        self.assertEqual(data.lag_members, {})
        self.assertEqual(data.lag_groups, {})
        self.assertEqual(len(data.ports), PORTS)
        # every port kept its own ifIndex
        self.assertEqual(
            sorted(data.ports), list(range(1, PORTS + 1))
        )
        self.assertIn("interface table", "\n".join(logged.output))

    def test_a_real_aggregate_still_works(self):
        agent = StubAgent(
            {1: AGGREGATE, 2: AGGREGATE},
            extra_ports={AGGREGATE: "Po1"},
        )
        data = asyncio.run(agent.collect("10.0.0.10"))
        self.assertEqual(data.lag_members, {1: AGGREGATE, 2: AGGREGATE})


class SaneLagMembersTest(unittest.TestCase):
    """The two rules, stated one at a time."""

    def _switch(self, count: int = 4) -> SwitchData:
        sw = SwitchData(ip="10.0.0.1")
        for i in range(1, count + 1):
            sw.ports[i] = PortInfo(if_index=i, name=f"Port{i}")
        return sw

    def test_an_aggregate_outside_the_interface_table_is_dropped(self):
        sw = self._switch()
        with self.assertLogs("moonlan.snmp_collector", "WARNING"):
            kept = sane_lag_members(sw, {1: PHANTOM, 2: PHANTOM}, sw.ip)
        self.assertEqual(kept, {})

    def test_an_aggregate_holding_every_physical_port_is_dropped(self):
        """A plausible ifIndex can still be the wrong reading."""
        sw = self._switch()
        sw.ports[AGGREGATE] = PortInfo(
            if_index=AGGREGATE, name="Po1", is_physical=False
        )
        claimed = {i: AGGREGATE for i in range(1, 5)}
        with self.assertLogs("moonlan.snmp_collector", "WARNING") as logged:
            kept = sane_lag_members(sw, claimed, sw.ip)
        self.assertEqual(kept, {})
        self.assertIn("all 4 physical port(s)", "\n".join(logged.output))

    def test_a_partial_aggregate_is_kept(self):
        sw = self._switch()
        sw.ports[AGGREGATE] = PortInfo(
            if_index=AGGREGATE, name="Po1", is_physical=False
        )
        kept = sane_lag_members(sw, {1: AGGREGATE, 2: AGGREGATE}, sw.ip)
        self.assertEqual(kept, {1: AGGREGATE, 2: AGGREGATE})

    def test_no_rows_no_complaint(self):
        self.assertEqual(sane_lag_members(self._switch(), {}, "10.0.0.1"), {})


if __name__ == "__main__":
    unittest.main()
