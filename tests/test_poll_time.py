"""Where a poll's budget went (v0.8.8).

"did not finish its poll within the 120 s budget" was the whole story:
which table the time ran out on was not said anywhere, and finding the
walk an agent sent round in circles took a separate script. The
collector now times each walk of a full poll; the WARNING says where
the cut fell, OverBudget carries it, and /api/polling has `cut_at` and
the `slowest_walks` of the last poll.

The agent is a stub behind the real collect() and _walk.

Run with:  python -m unittest discover -s tests
"""

import asyncio
import logging
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from service_fixture import SWITCHES  # noqa: E402

from moonlan import diag, server  # noqa: E402
from moonlan import snmp_collector as module  # noqa: E402
from moonlan.lldp import OID_LLDP_REM_MAN_ADDR_IF_SUBTYPE  # noqa: E402
from moonlan.snmp_collector import (  # noqa: E402
    OID_BRIDGE_ADDRESS,
    OID_IF_DESCR,
    OID_IF_HIGH_SPEED,
    OID_IF_NAME,
    OID_IF_OPER_STATUS,
    OID_PORT_IFINDEX,
    OID_SYS_NAME,
    OverBudget,
    SnmpCollector,
    oid_name,
)

IP = SWITCHES[0]


def _budget_line(logs) -> str:
    return next(r.getMessage() for r in logs.records
                if "did not finish its poll" in r.getMessage())


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
    """Two ports. `pause[oid]` seconds before each row of that table;
    `stall` — the table that stops answering after `stall_after` rows.
    """

    def __init__(self):
        super().__init__(community="public")
        self.pause: dict[str, float] = {}
        self.stall = ""
        self.stall_after = 0
        self.opened = 0

    async def _get(self, host, oid):
        return "core-sw" if oid == OID_SYS_NAME else None

    async def _open_walk(self, host, start):
        self.opened += 1
        agent = self
        rows = {
            OID_IF_DESCR: ["Gi0/1", "Gi0/2", "Gi0/3", "Gi0/4"],
            OID_IF_NAME: ["Gi0/1", "Gi0/2", "Gi0/3", "Gi0/4"],
            OID_IF_OPER_STATUS: [1, 1, 2, 2],
            OID_IF_HIGH_SPEED: [1000, 1000, 100, 100],
        }.get(start, [])

        async def gen():
            for index, value in enumerate(rows, 1):
                if start == agent.stall and index > agent.stall_after:
                    await asyncio.sleep(30)
                if agent.pause.get(start):
                    await asyncio.sleep(agent.pause[start])
                yield None, None, None, [(_Oid(f"{start}.{index}"), value)]

        return _Closeable(gen())


class CutTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._report = dict(server.rt.poll_report)
        module.RESUME_PAUSE = 0

    def tearDown(self):
        server.rt.poll_report.clear()
        server.rt.poll_report.update(self._report)

    async def test_cut_in_a_walk_then_a_full_poll(self):
        agent = Agent()
        agent.stall, agent.stall_after = OID_IF_NAME, 3
        with self.assertLogs("moonlan", level="WARNING") as logs:
            result = await server._collect_within_budget(agent, IP, 0.4)
        self.assertIsInstance(result, OverBudget)
        self.assertTrue(result.answered)
        cut = result.cut
        self.assertEqual(
            {k: cut[k] for k in ("oid", "name", "rows", "resumes", "in_walk")},
            {"oid": OID_IF_NAME, "name": "IF_NAME", "rows": 3, "resumes": 0,
             "in_walk": True},
        )
        self.assertGreaterEqual(cut["seconds"], 0.3)
        self.assertLess(cut["seconds"], 1.0)
        message = _budget_line(logs)
        self.assertIn(
            f"cut in the walk of {OID_IF_NAME} (IF_NAME) after 3 row(s) and ",
            message,
        )
        # the cut walk is where the time went, and it is the slowest
        self.assertEqual(result.walks[0]["oid"], OID_IF_NAME)

        polling = await server.api_polling()
        row = next(r for r in polling["switches"] if r["ip"] == IP)
        self.assertEqual(row["cut_at"]["oid"], OID_IF_NAME)
        self.assertEqual(row["cut_at"]["rows"], 3)

        # the next poll finishes: no cut, three slowest walks, longest first
        agent.stall = ""
        agent.pause = {OID_IF_DESCR: 0.03, OID_IF_NAME: 0.02,
                       OID_IF_HIGH_SPEED: 0.01, OID_IF_OPER_STATUS: 0.005}
        data = await server._collect_within_budget(agent, IP, 10)
        self.assertTrue(data.reachable)
        polling = await server.api_polling()
        row = next(r for r in polling["switches"] if r["ip"] == IP)
        self.assertIsNone(row["cut_at"])
        walks = row["slowest_walks"]
        self.assertEqual(
            [w["oid"] for w in walks],
            [OID_IF_DESCR, OID_IF_NAME, OID_IF_HIGH_SPEED],
        )
        self.assertEqual(walks[0]["name"], "IF_DESCR")
        self.assertEqual(walks[0]["rows"], 4)
        seconds = [w["seconds"] for w in walks]
        self.assertEqual(seconds, sorted(seconds, reverse=True))
        self.assertEqual(seconds[0], round(seconds[0], 1))

    async def test_cut_between_walks(self):
        agent = Agent()
        # collect_stp asks with GET after the bridge-port walk

        async def slow(host, oid):
            if agent.opened and oid != OID_SYS_NAME:
                await asyncio.sleep(30)
            return "core-sw" if oid == OID_SYS_NAME else None
        agent._get = slow
        with self.assertLogs("moonlan", level="WARNING") as logs:
            result = await server._collect_within_budget(agent, IP, 0.3)
        self.assertFalse(result.cut["in_walk"])
        self.assertTrue(result.cut["oid"])
        self.assertIn("cut between walks; the last finished was ",
                      _budget_line(logs))

    async def test_cut_before_the_first_walk(self):
        agent = Agent()

        async def slow(host, oid):
            if oid == OID_BRIDGE_ADDRESS:
                await asyncio.sleep(30)
            return "core-sw" if oid == OID_SYS_NAME else None
        agent._get = slow
        with self.assertLogs("moonlan", level="WARNING") as logs:
            result = await server._collect_within_budget(agent, IP, 0.2)
        self.assertEqual(result.cut["oid"], "")
        self.assertIn("cut before its first walk", _budget_line(logs))

    async def test_a_poll_with_no_answer_changes_nothing(self):
        agent = Agent()
        server.rt.poll_report[IP] = {"cut_at": {"oid": "1.2.3"}, "slowest_walks": []}

        async def silent(host, oid):
            return None
        agent._get = silent

        async def nothing(*_args):
            return "no", "community or SNMP off"
        real = module.diagnose_silence
        module.diagnose_silence = nothing
        try:
            with self.assertLogs("moonlan.snmp_collector", level="WARNING"):
                data = await server._collect_within_budget(agent, IP, 5)
        finally:
            module.diagnose_silence = real
        self.assertFalse(data.reachable)
        self.assertEqual(server.rt.poll_report[IP]["cut_at"], {"oid": "1.2.3"})


class OutsideAPollTest(unittest.TestCase):
    def test_a_counters_walk_is_not_timed(self):
        agent = Agent()

        async def run():
            return [row async for row in agent._walk(IP, OID_IF_DESCR)]
        self.assertEqual(len(asyncio.run(run())), 4)
        self.assertEqual(agent.slowest_walks(IP), [])


class NameTest(unittest.TestCase):
    def test_names_come_from_the_constants(self):
        self.assertEqual(oid_name(OID_LLDP_REM_MAN_ADDR_IF_SUBTYPE),
                         "LLDP_REM_MAN_ADDR_IF_SUBTYPE")
        self.assertEqual(oid_name(OID_PORT_IFINDEX), "PORT_IFINDEX")
        self.assertEqual(oid_name("1.3.6.1.4.1.99999.1"), "")


class DiagTest(unittest.TestCase):
    def test_config_prints_the_cut_and_the_slowest_walk(self):
        lines = diag._poll_time_lines({
            "cut_at": {"oid": OID_LLDP_REM_MAN_ADDR_IF_SUBTYPE,
                       "name": "LLDP_REM_MAN_ADDR_IF_SUBTYPE", "rows": 0,
                       "seconds": 118.4, "resumes": 0, "in_walk": True},
            "slowest_walks": [{"oid": OID_IF_NAME, "name": "IF_NAME",
                               "rows": 28, "seconds": 1.2}],
        })
        self.assertEqual(lines, [
            f"last cut: in the walk of {OID_LLDP_REM_MAN_ADDR_IF_SUBTYPE} "
            "(LLDP_REM_MAN_ADDR_IF_SUBTYPE) after 0 row(s) and 118.4 s",
            f"slowest walk: {OID_IF_NAME} (IF_NAME) 1.2 s, 28 row(s)",
        ])
        self.assertEqual(diag._poll_time_lines({"cut_at": None,
                                                "slowest_walks": []}), [])


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
    unittest.main()
