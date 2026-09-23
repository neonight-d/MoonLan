"""Silence has three causes, and they need three different people.

SNMPv2c does not answer a wrong community string at all. From outside,
that is indistinguishable from an agent that does not implement the
object — and MoonLan used to print exactly that: "the subtree is empty,
or the agent does not implement it". Both halves of it were wrong on a
switch that answers ping in 3 ms, implements sysName and works
perfectly; somebody had put `community: public` back into the config
and all ten switches went dark at once.

An answer that is merely incomplete costs time. That one pointed away
from the cause, which costs an hour.

Run with:  python -m unittest discover -s tests
"""

import asyncio
import unittest

from moonlan import pinger, snmp_collector
from moonlan.snmp_collector import (
    OID_SYS_DESCR,
    OID_SYS_NAME,
    SILENCE_COMMUNITY,
    SILENCE_OID,
    SILENCE_UNREACHABLE,
    diagnose_silence,
)

PRIVATE_OID = "1.3.6.1.4.1.9.9.999"


class StubCollector:
    """Answers sysDescr or does not; records what it was asked."""

    def __init__(self, sys_descr: str | None):
        self.sys_descr = sys_descr
        self.asked: list[str] = []

    async def _get(self, host: str, oid: str):
        self.asked.append(oid)
        if oid == OID_SYS_DESCR:
            return self.sys_descr
        return None


class SilenceVerdictTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._ping = pinger.ping

    def tearDown(self):
        pinger.ping = self._ping
        snmp_collector.pinger.ping = self._ping

    def _ping_answers(self, alive: bool):
        async def fake_ping(ip: str) -> bool:
            return alive

        snmp_collector.pinger.ping = fake_ping

    async def test_pings_but_says_nothing_means_the_community(self):
        """The case that cost an hour."""
        self._ping_answers(True)
        collector = StubCollector(None)
        verdict, why = await diagnose_silence(
            collector, "10.0.0.10", PRIVATE_OID
        )
        self.assertEqual(verdict, SILENCE_COMMUNITY)
        self.assertIn("community", why)
        # and it says so BEFORE offering anything else
        self.assertLess(why.index("community"), why.index("does not"))

    async def test_a_live_agent_silent_on_one_oid_is_the_old_answer(self):
        """Here "not implemented" is true, and can be said firmly."""
        self._ping_answers(True)
        collector = StubCollector("WS6-DGS-1210-26/F1 6.10.007")
        verdict, why = await diagnose_silence(
            collector, "10.0.0.10", PRIVATE_OID
        )
        self.assertEqual(verdict, SILENCE_OID)
        self.assertIn("alive", why)
        self.assertIn("DGS-1210", why)

    async def test_no_snmp_and_no_ping_is_a_network_question(self):
        self._ping_answers(False)
        collector = StubCollector(None)
        verdict, why = await diagnose_silence(
            collector, "10.0.0.253", PRIVATE_OID
        )
        self.assertEqual(verdict, SILENCE_UNREACHABLE)
        self.assertIn("ping", why)

    async def test_a_mandatory_object_is_not_probed_twice(self):
        """sysName already failed. Asking sysDescr proves nothing more.

        Both are mandatory, so one silence is the other's; the second
        probe would only spend another full retry budget per switch per
        scan on a device already known to be silent.
        """
        self._ping_answers(True)
        collector = StubCollector(None)
        verdict, _why = await diagnose_silence(
            collector, "10.0.0.10", OID_SYS_NAME
        )
        self.assertEqual(verdict, SILENCE_COMMUNITY)
        self.assertEqual(collector.asked, [])


if __name__ == "__main__":
    unittest.main()
