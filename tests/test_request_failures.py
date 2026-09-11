"""A request that cannot even be built must not stop the collection.

pyasn1 rejects a malformed OID while the request is being assembled,
before anything reaches the network, so `error_ind` never gets a chance
to report it — the exception simply leaves through the caller. One such
OID, "1.3.6.1.2.1.2.2.1.10.-25", ended the counters cycle for all five
switches every 105 seconds for as long as the service ran.

Run with:  python -m unittest discover -s tests
"""

import asyncio
import unittest

from pyasn1.error import PyAsn1Error

import moonlan.snmp_collector as collector_module
from moonlan.snmp_collector import SnmpCollector


class StubCollector(SnmpCollector):
    """No engine, no transport — only the guards under test."""

    def __init__(self):
        self._engine = None
        self._community = None
        self._walk_status = {}
        self._retries_on_break = 0

    async def _target(self, host):
        return None


class RequestBuildFailureTest(unittest.TestCase):
    def setUp(self):
        self.collector = StubCollector()
        self._get_cmd = collector_module.get_cmd

    def tearDown(self):
        collector_module.get_cmd = self._get_cmd

    def test_get_returns_none_instead_of_raising(self):
        async def exploding_get(*args, **kwargs):
            raise PyAsn1Error(
                "Malformed Object ID 1.3.6.1.2.1.2.2.1.10.-25 at ObjectName"
            )

        collector_module.get_cmd = exploding_get
        with self.assertLogs("moonlan.snmp_collector", "WARNING") as logged:
            value = asyncio.run(
                self.collector._get("10.0.0.21", "1.3.6.1.2.1.2.2.1.10.-25")
            )
        self.assertIsNone(value)
        self.assertIn("Malformed Object ID", "\n".join(logged.output))

    def test_walk_that_cannot_start_yields_nothing(self):
        async def exploding_open(host, start):
            raise PyAsn1Error("Malformed Object ID at ObjectName")

        self.collector._open_walk = exploding_open

        async def drain():
            return [row async for row in self.collector._walk("10.0.0.21", "1.2.3")]

        with self.assertLogs("moonlan.snmp_collector", "WARNING"):
            rows = asyncio.run(drain())
        self.assertEqual(rows, [])
        status = self.collector.last_walk_status("10.0.0.21", "1.2.3")
        self.assertEqual(status.rows, 0)
        self.assertIn("Malformed", status.error)

    def test_walk_that_raises_midway_keeps_what_arrived(self):
        """An exception from the transport is a break, not a crash."""

        class _Oid(tuple):
            def __new__(cls, text):
                return super().__new__(cls, (int(p) for p in text.split(".")))

        class _Rows:
            def __init__(self):
                self._served = 0

            def __aiter__(self):
                return self

            async def __anext__(self):
                self._served += 1
                if self._served > 3:
                    raise RuntimeError("transport went away")
                return (
                    None, None, None,
                    [(_Oid(f"1.2.3.{self._served}"), self._served)],
                )

            async def aclose(self):
                pass

        async def open_walk(host, start):
            return _Rows()

        self.collector._open_walk = open_walk

        async def drain():
            return [row async for row in self.collector._walk("10.0.0.21", "1.2.3")]

        with self.assertLogs("moonlan.snmp_collector", "WARNING"):
            rows = asyncio.run(drain())
        self.assertEqual([int(v) for _suffix, v in rows], [1, 2, 3])
        status = self.collector.last_walk_status("10.0.0.21", "1.2.3")
        self.assertTrue(status.truncated)
        self.assertIn("transport went away", status.error)


if __name__ == "__main__":
    unittest.main()
