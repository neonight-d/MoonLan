"""Regression: a walk that stops answering mid-table is picked back up.

On mb1 the ifHCInOctets walk answered the first rows and then went
quiet, so the ports at the end of ifTable — 1/25…1/28, the gigabit
uplinks — came back empty in three polls running. `_walk` used to
`return` on the first error and lose everything past that point.

The agent here is a stub: it hands out rows in order and refuses once,
at a chosen row, the way a switch that has run out of breath does.

Run with:  python -m unittest discover -s tests
"""

import asyncio
import unittest

from moonlan.snmp_collector import SnmpCollector

BASE = "1.3.6.1.2.1.31.1.1.1.6"
ROWS = 28


class FakeAgent:
    """A switch that runs out of breath while answering a walk.

    `break_after` is the recoverable kind: it stops after that many
    rows of a pass, `breaks` times in all, and answers normally
    afterwards. `wall` is the stubborn kind: it never serves an index
    past that point, however often it is asked.
    """

    def __init__(
        self,
        break_after: int | None = None,
        breaks: int = 1,
        wall: int | None = None,
        rows: int = ROWS,
    ):
        self.break_after = break_after
        self.breaks_left = breaks
        self.wall = wall
        self.rows = rows
        self.passes = 0

    def walk(self, start: str):
        """(oid, value) pairs from just after `start`, then maybe silence."""
        self.passes += 1
        first = 1
        if start != BASE:
            first = int(start.rsplit(".", 1)[1]) + 1
        served = 0
        for index in range(first, self.rows + 1):
            if self.wall is not None and index > self.wall:
                raise TimeoutError("No SNMP response received before timeout")
            if (
                self.break_after is not None
                and self.breaks_left > 0
                and served == self.break_after
            ):
                self.breaks_left -= 1
                raise TimeoutError("No SNMP response received before timeout")
            served += 1
            yield f"{BASE}.{index}", index * 1000


class _Oid(tuple):
    """Just enough of an OID object for tuple(name) to work."""

    def __new__(cls, text: str):
        return super().__new__(cls, (int(p) for p in text.split(".")))


class StubCollector(SnmpCollector):
    """The real _walk over a stub transport."""

    def __init__(self, agent: FakeAgent, retries_on_break: int = 2):
        self.agent = agent
        self._walk_status = {}
        self._retries_on_break = retries_on_break

    async def _open_walk(self, host, start):
        agent = self.agent

        async def rows():
            try:
                for oid, value in agent.walk(start):
                    yield None, None, None, [(_Oid(oid), value)]
            except TimeoutError as exc:
                yield str(exc), None, None, []

        return _Closeable(rows())


class _Closeable:
    """pysnmp's generator supports aclose(); a bare one does too."""

    def __init__(self, gen):
        self._gen = gen

    def __aiter__(self):
        return self._gen.__aiter__()

    async def aclose(self):
        await self._gen.aclose()


def _walk_all(collector):
    import moonlan.snmp_collector as module

    module.RESUME_PAUSE = 0  # no need to actually wait in a test

    async def run():
        return [
            (suffix, int(value))
            async for suffix, value in collector._walk("10.0.0.21", BASE)
        ]

    return asyncio.run(run())


class WalkResumeTest(unittest.TestCase):
    def test_complete_walk(self):
        agent = FakeAgent(break_after=None)
        collector = StubCollector(agent)
        rows = _walk_all(collector)
        self.assertEqual(len(rows), ROWS)
        status = collector.last_walk_status("10.0.0.21", BASE)
        self.assertTrue(status.complete)
        self.assertEqual(status.rows, ROWS)

    def test_break_midway_is_resumed(self):
        """The tail arrives on the second attempt, in one sequence."""
        agent = FakeAgent(break_after=24)
        collector = StubCollector(agent)
        rows = _walk_all(collector)
        self.assertEqual([r[0][0] for r in rows], list(range(1, ROWS + 1)))
        status = collector.last_walk_status("10.0.0.21", BASE)
        self.assertEqual(status.rows, ROWS)
        self.assertEqual(status.resumes, 1)
        self.assertFalse(status.truncated)

    def test_repeated_breaks_give_up_and_say_where(self):
        """Beyond retries_on_break: keep what arrived, record the point."""
        agent = FakeAgent(wall=10)
        collector = StubCollector(agent, retries_on_break=2)
        rows = _walk_all(collector)
        status = collector.last_walk_status("10.0.0.21", BASE)
        self.assertTrue(status.truncated)
        self.assertEqual(status.resumes, 2)
        self.assertEqual(len(rows), status.rows)
        self.assertGreater(status.rows, 0)
        self.assertTrue(status.last_oid.startswith(BASE + "."))
        self.assertIn("timeout", status.error)

    def test_no_answer_at_all(self):
        """Nothing arrived: not truncated, just unanswered."""
        agent = FakeAgent(wall=0)
        collector = StubCollector(agent)
        rows = _walk_all(collector)
        status = collector.last_walk_status("10.0.0.21", BASE)
        self.assertEqual(rows, [])
        self.assertEqual(status.rows, 0)
        self.assertFalse(status.truncated)
        self.assertIn("timeout", status.error)


if __name__ == "__main__":
    unittest.main()
