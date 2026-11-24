"""A walk the agent sends round in circles ends (v0.8.8).

An agent of RouterOS 6.x answered GETNEXT on lldpRemManAddrIfSubtype
with that very OID and noSuchObject — ten requests, ten identical
answers. RFC 3416 §4.2.2 forbids both: the answer is always past the
OID asked for, and the end of the MIB is endOfMibView, never a no-such.
pysnmp's walk_cmd does not check that the OID grows, and _walk skipped
no-such rows without counting them, so the same request went out
hundreds of times a second until the poll budget took the whole poll
down. Its full poll never finished once.

A repeat ends the walk — the OID asked for coming back, or a row this
walk has already had — and so does a long run of no-such rows. The rows
that came stay, the walk is not resumed, one WARNING per (host, OID),
and the rest of the poll goes on.

An answer that only goes back, to an OID not seen yet, is the agent's
own order and not a loop (the addendum to v0.8.8): an access switch
handed out its Q-BRIDGE table VLAN by VLAN out of numeric order, and
the rows after the step back exist nowhere else. Ending the walk there
lost them on every scan; such a table is read to its end.

The agents here are stubs behind the real _walk.

Run with:  python -m unittest discover -s tests
"""

import asyncio
import contextlib
import io
import logging
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import service_fixture  # noqa: E402,F401  (a config for server)

from moonlan import diag, server  # noqa: E402
from moonlan import snmp_collector as module  # noqa: E402
from moonlan.lldp import OID_LLDP_REM_MAN_ADDR_IF_SUBTYPE  # noqa: E402
from moonlan.snmp_collector import (  # noqa: E402
    FAULT_NO_SUCH_RUN,
    FAULT_REPEATED_OID,
    OID_IF_DESCR,
    OID_SYS_NAME,
    WALK_MAX_NO_SUCH,
    DeadOid,
    SnmpCollector,
)

HOST = "10.1.0.30"
TABLE = "1.3.6.1.2.1.31.1.1.1.18"
Q_FDB = "1.3.6.1.2.1.17.7.1.2.2.1.2"   # dot1qTpFdbPort.<fdbId>.<MAC>
LOGGER = "moonlan.snmp_collector"


class NoSuchObject:
    """Matched by class name, like pysnmp's (see snmpval.is_no_such)."""

    def __str__(self):
        return "No Such Object currently exists at this OID"


class NoSuchInstance(NoSuchObject):
    pass


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


class Agent(SnmpCollector):
    """The real _walk; `answers(oid, start)` is what the agent says.

    Built without __init__, like the stubs in test_walk_resume and
    test_dead_oid: the state v0.8.8 added must not need it.
    """

    def __init__(self, answers):
        self.answers = answers
        self.opened = 0
        self.served = 0
        self._walk_status = {}
        self._retries_on_break = 2
        self._per_host = {}
        self._dead_oids = {}
        self._cycle = 0
        self._dead_oid_strikes = 3
        self._dead_oid_cooldown = 30
        self._communities = {}
        self.answered_at = {}

    async def _get(self, host, oid):
        return "rb-annex" if oid == OID_SYS_NAME else None

    async def _open_walk(self, host, start):
        self.opened += 1
        agent = self

        async def rows():
            for name, value in agent.answers(start):
                agent.served += 1
                if name is None:   # the agent stops answering here
                    yield value, None, None, []
                    return
                yield None, None, None, [(_Oid(name), value)]

        return _Closeable(rows())


def loop_forever(oid):
    """GETNEXT on anything under `oid` comes back as itself, no-such."""
    def answers(start):
        while True:
            yield start, NoSuchObject()
    return answers


def in_its_order(base, rows, after=None, stop_after=None):
    """An agent that hands the rows out in the order given, whatever
    their OIDs; then `after`, if any (the next column: the table is
    over). `stop_after` rows into the first pass, it stops answering
    once, and a resumed pass goes on from the OID it is given."""
    stopped = {"yet": False}

    def answers(start):
        at = 0 if start == base else rows.index(start) + 1
        for served, name in enumerate(rows[at:]):
            if stop_after is not None and not stopped["yet"] and at + served == stop_after:
                stopped["yet"] = True
                yield None, "No SNMP response received before timeout"
                return
            yield name, 7
        if after:
            yield after, 1
    return answers


def fdb(vlan, last):
    return f"{Q_FDB}.{vlan}.0.238.0.0.{vlan}.{last}"


def walk(agent, oid=TABLE, times=1):
    async def run():
        rows = []
        for _ in range(times):
            rows = [(suffix, value) async for suffix, value in agent._walk(HOST, oid)]
        return rows
    module.RESUME_PAUSE = 0
    return asyncio.run(asyncio.wait_for(run(), 5))


class LoopTest(unittest.TestCase):
    def test_the_same_oid_back_ends_the_walk_at_once(self):
        agent = Agent(loop_forever(TABLE))
        rows = walk(agent)
        status = agent.last_walk_status(HOST, TABLE)
        self.assertEqual(rows, [])
        self.assertEqual(agent.opened, 1)   # not resumed
        self.assertEqual(agent.served, 1)   # one answer, one request
        self.assertEqual(status.rows, 0)
        self.assertEqual(status.fault, FAULT_REPEATED_OID)
        self.assertFalse(status.truncated)
        self.assertIn(TABLE, status.error)
        self.assertIn("NoSuchObject", status.error)
        self.assertTrue(status.error.endswith(", the OID asked for"), status.error)

    def test_one_warning_for_two_walks(self):
        agent = Agent(loop_forever(TABLE))
        with self.assertLogs(LOGGER, level="DEBUG") as logs:
            walk(agent, times=2)
        warnings = [r for r in logs.records if r.levelno == logging.WARNING]
        self.assertEqual(len(warnings), 1)
        message = warnings[0].getMessage()
        self.assertIn(f"{HOST}: walk of {TABLE} stopped", message)
        self.assertIn("RFC 3416", message)
        self.assertIn("this table is left out", message)
        self.assertIn("the rest of the poll goes on", message)
        self.assertTrue(any(r.levelno == logging.DEBUG for r in logs.records))

    def test_a_row_again_keeps_what_came(self):
        def answers(start):
            for index in (1, 2, 3, 4, 5, 3, 4, 5, 6):
                yield f"{TABLE}.{index}", index * 10
        agent = Agent(answers)
        rows = walk(agent)
        status = agent.last_walk_status(HOST, TABLE)
        self.assertEqual([suffix for suffix, _value in rows],
                         [(1,), (2,), (3,), (4,), (5,)])
        self.assertTrue(status.truncated)
        self.assertEqual(status.fault, FAULT_REPEATED_OID)
        self.assertEqual(status.last_oid, f"{TABLE}.5")
        self.assertEqual(agent.opened, 1)
        self.assertIn("which it had already returned in this walk", status.error)

    def test_a_run_of_no_such_rows_ends_it(self):
        def answers(start):
            index = 0
            while True:
                index += 1
                yield f"{TABLE}.{index}", NoSuchInstance()
        agent = Agent(answers)
        rows = walk(agent)
        status = agent.last_walk_status(HOST, TABLE)
        self.assertEqual(rows, [])
        self.assertEqual(agent.served, WALK_MAX_NO_SUCH)
        self.assertEqual(status.fault, FAULT_NO_SUCH_RUN)

    def test_one_no_such_row_in_a_table_is_skipped_as_before(self):
        def answers(start):
            for index in range(1, 11):
                value = NoSuchInstance() if index == 4 else index
                yield f"{TABLE}.{index}", value
        agent = Agent(answers)
        rows = walk(agent)
        status = agent.last_walk_status(HOST, TABLE)
        self.assertEqual(len(rows), 9)
        self.assertEqual(status.fault, "")
        self.assertTrue(status.complete)

    def test_a_value_resets_the_run(self):
        def answers(start):
            for index in range(1, 2 * WALK_MAX_NO_SUCH + 2):
                value = index if index == WALK_MAX_NO_SUCH else NoSuchInstance()
                yield f"{TABLE}.{index}", value
        agent = Agent(answers)
        rows = walk(agent)
        self.assertEqual(len(rows), 1)
        self.assertEqual(agent.last_walk_status(HOST, TABLE).fault, FAULT_NO_SUCH_RUN)


class OrderTest(unittest.TestCase):
    """Only a repeat is a loop (the addendum to v0.8.8)."""

    def test_a_table_out_of_order_is_read_to_its_end(self):
        """An access switch's MAC table, VLAN by VLAN: 10, 40, 41, then
        three rows of VLAN 20 that are nowhere else."""
        rows = [fdb(10, 1), fdb(10, 2), fdb(40, 1), fdb(41, 1), fdb(41, 2),
                fdb(20, 1), fdb(20, 2), fdb(20, 3)]
        agent = Agent(in_its_order(Q_FDB, rows, after="1.3.6.1.2.1.17.7.1.2.2.1.3.10.1"))
        with self.assertLogs(LOGGER, level="INFO") as logs:
            read = walk(agent, Q_FDB, times=2)
        status = agent.last_walk_status(HOST, Q_FDB)
        self.assertEqual(len(read), 8)
        self.assertEqual(read[-1][0][0], 20)
        self.assertEqual(status.fault, "")
        self.assertTrue(status.complete)
        self.assertEqual(status.out_of_order, 1)
        self.assertEqual(agent.walk_faults(), [])
        # one INFO for the pair in the life of the process, two walks or not
        self.assertEqual(
            [r.getMessage() for r in logs.records if r.levelno == logging.INFO],
            [f"{HOST}: the agent returns {Q_FDB} out of order ({fdb(41, 2)} then "
             f"{fdb(20, 1)}); the table is read to its end anyway"],
        )
        self.assertFalse([r for r in logs.records if r.levelno >= logging.WARNING])

    def test_back_to_the_start_is_a_loop(self):
        rows = [f"{TABLE}.{index}" for index in (1, 2, 3, 4)]
        agent = Agent(in_its_order(TABLE, rows + [rows[0], rows[1], rows[2]]))
        with self.assertLogs(LOGGER, level="WARNING"):
            read = walk(agent)
        status = agent.last_walk_status(HOST, TABLE)
        self.assertEqual(len(read), 4)
        self.assertEqual(status.fault, FAULT_REPEATED_OID)
        self.assertEqual(agent.served, 5)       # ended on the first repeat
        self.assertTrue(status.truncated)
        self.assertIn(f"{TABLE}.1 (int), which it had already returned in this walk",
                      status.error)

    def test_a_row_from_the_middle_again(self):
        """Back to a row not seen yet is read on; to one seen, it ends —
        even when that OID is past the one asked for."""
        rows = [f"{TABLE}.{index}" for index in (1, 2, 5, 3, 4, 5, 6)]
        agent = Agent(in_its_order(TABLE, rows))
        with self.assertLogs(LOGGER, level="INFO"):
            read = walk(agent)
        status = agent.last_walk_status(HOST, TABLE)
        self.assertEqual([suffix for suffix, _value in read], [(1,), (2,), (5,), (3,), (4,)])
        self.assertEqual(status.fault, FAULT_REPEATED_OID)
        self.assertEqual(status.out_of_order, 1)
        self.assertEqual(status.last_oid, f"{TABLE}.4")

    def test_what_was_seen_outlives_a_resume(self):
        rows = [f"{TABLE}.{index}" for index in (1, 2, 3, 4, 2, 5)]
        agent = Agent(in_its_order(TABLE, rows, stop_after=3))
        with self.assertLogs(LOGGER, level="WARNING"):
            read = walk(agent)
        status = agent.last_walk_status(HOST, TABLE)
        self.assertEqual([suffix for suffix, _value in read], [(1,), (2,), (3,), (4,)])
        self.assertEqual(agent.opened, 2)
        self.assertEqual(status.resumes, 1)
        self.assertEqual(status.fault, FAULT_REPEATED_OID)

    def test_a_step_back_out_of_the_subtree_ends_the_table(self):
        rows = [f"{TABLE}.{index}" for index in (1, 2)]
        agent = Agent(in_its_order(TABLE, rows, after="1.3.6.1.2.1.31.1.1.1.17.9"))
        read = walk(agent)
        status = agent.last_walk_status(HOST, TABLE)
        self.assertEqual(len(read), 2)
        self.assertEqual(status.fault, "")
        self.assertEqual(status.out_of_order, 0)
        self.assertTrue(status.complete)


class OutcomeTest(unittest.TestCase):
    def test_not_a_dead_oid_strike(self):
        agent = Agent(loop_forever(TABLE))
        agent._dead_oids[(HOST, TABLE)] = DeadOid(strikes=2)
        walk(agent)
        # any outcome without a timeout resets the count
        self.assertNotIn((HOST, TABLE), agent._dead_oids)
        self.assertEqual(agent.paused_oids(), [])

    def test_listed_until_it_walks_normally_again(self):
        broken = {"yes": True}

        def answers(start):
            if broken["yes"]:
                yield start, NoSuchObject()
                return
            for index in (1, 2):
                yield f"{TABLE}.{index}", index
        agent = Agent(answers)
        walk(agent)
        faults = agent.walk_faults()
        self.assertEqual(len(faults), 1)
        self.assertEqual(
            {k: faults[0][k] for k in ("host", "oid", "fault", "rows", "last_oid")},
            {"host": HOST, "oid": TABLE, "fault": FAULT_REPEATED_OID,
             "rows": 0, "last_oid": ""},
        )
        self.assertLessEqual(faults[0]["first_at"], faults[0]["last_at"])
        broken["yes"] = False
        with self.assertLogs(LOGGER, level="INFO") as logs:
            walk(agent)
        self.assertEqual(
            [r.getMessage() for r in logs.records if r.levelno == logging.INFO],
            [f"{HOST}: walk of {TABLE} walks normally again (2 row(s))"],
        )
        self.assertEqual(agent.walk_faults(), [])


class CollectTest(unittest.TestCase):
    def test_the_poll_finishes_when_the_lldp_address_table_loops(self):
        """The case the version was written for."""
        def answers(start):
            if start.startswith(OID_LLDP_REM_MAN_ADDR_IF_SUBTYPE):
                while True:
                    yield start, NoSuchObject()
            if start == OID_IF_DESCR:
                for index in (1, 2):
                    yield f"{OID_IF_DESCR}.{index}", f"ether{index}"
            return
        agent = Agent(answers)
        module.RESUME_PAUSE = 0
        with self.assertLogs(LOGGER, level="WARNING") as logs:
            data = asyncio.run(asyncio.wait_for(agent.collect(HOST), 5))
        self.assertTrue(data.reachable)
        self.assertEqual(sorted(data.ports), [1, 2])
        self.assertEqual(
            agent.last_walk_status(HOST, OID_LLDP_REM_MAN_ADDR_IF_SUBTYPE).fault,
            FAULT_REPEATED_OID,
        )
        self.assertEqual(
            sum(OID_LLDP_REM_MAN_ADDR_IF_SUBTYPE in r.getMessage()
                for r in logs.records),
            1,
        )


class ServiceTest(unittest.IsolatedAsyncioTestCase):
    async def test_skipped_oids_lists_the_faults(self):
        agent = Agent(loop_forever(TABLE))
        async for _row in agent._walk(HOST, TABLE):
            pass
        with mock.patch.object(server, "_collector", agent):
            data = await server.api_skipped_oids()
        self.assertEqual(data["paused"], [])
        self.assertEqual([f["oid"] for f in data["walk_faults"]], [TABLE])

    def test_diag_skipped_prints_them_apart(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            diag._print_walk_faults([{
                "host": HOST, "oid": TABLE, "fault": FAULT_REPEATED_OID,
                "rows": 0, "last_oid": "", "error": "the agent answered…",
                "first_at": 1_700_000_000, "last_at": 1_700_000_600,
            }])
        text = out.getvalue()
        self.assertIn("Walks the agent breaks", text)
        self.assertIn(f"{HOST}", text)
        self.assertIn(FAULT_REPEATED_OID, text)
        self.assertIn("the agent answered…", text)


class DiagWalkTest(unittest.TestCase):
    def test_diag_walk_ends_and_says_why(self):
        agent = Agent(loop_forever(OID_LLDP_REM_MAN_ADDR_IF_SUBTYPE))

        async def no_silence(*_args, **_kwargs):
            raise AssertionError("the agent answered: not a silence to diagnose")

        out = io.StringIO()
        with mock.patch.object(diag, "_make_collector", lambda *_a: agent), \
                mock.patch.object(diag, "diagnose_silence", no_silence), \
                contextlib.redirect_stdout(out), \
                self.assertLogs(LOGGER, level="WARNING"):
            asyncio.run(asyncio.wait_for(diag.run_walk(
                HOST, OID_LLDP_REM_MAN_ADDR_IF_SUBTYPE, 500, "public", 2
            ), 5))
        text = out.getvalue()
        self.assertIn("stopped after 0 row(s)", text)
        self.assertIn("the OID asked for", text)
        self.assertIn("RFC 3416", text)

    def test_diag_walk_reads_a_table_out_of_order_and_says_so(self):
        rows = [fdb(10, 1), fdb(40, 1), fdb(20, 1), fdb(20, 2)]
        agent = Agent(in_its_order(Q_FDB, rows))
        out = io.StringIO()
        with mock.patch.object(diag, "_make_collector", lambda *_a: agent), \
                contextlib.redirect_stdout(out), \
                self.assertLogs(LOGGER, level="INFO"):
            asyncio.run(asyncio.wait_for(diag.run_walk(HOST, Q_FDB, 500, "public", 2), 5))
        text = out.getvalue()
        self.assertEqual(text.count(f"{Q_FDB}."), 4)
        self.assertIn("the agent returned this table out of order (1 step(s) back); "
                      "MoonLan reads it to its end", text)
        self.assertIn("4 row(s)", text)


if __name__ == "__main__":
    unittest.main()
