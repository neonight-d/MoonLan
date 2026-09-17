"""A request that does not really answer must not look like one.

Two ways an SNMP request fails without raising where anyone is looking:

- pyasn1 rejects a malformed OID while the request is being assembled,
  before anything reaches the network, so `error_ind` never gets the
  chance to report it — the exception simply leaves through the
  caller. One such OID, "1.3.6.1.2.1.2.2.1.10.-25", ended the counters
  cycle for all five switches every 105 seconds for as long as the
  service ran;
- an SNMPv2c agent answers a GET for an object it does not implement
  with a **successful** PDU whose varbind holds noSuchObject,
  noSuchInstance or endOfMibView. No error status, no error
  indication, and all three derive from OctetString, so the caller
  gets what looks like an empty string. That is how probing for a
  vendor's private branch succeeded on every device that speaks SNMP.

Run with:  python -m unittest discover -s tests
"""

import asyncio
import unittest

from pyasn1.error import PyAsn1Error
from pysnmp.proto.rfc1905 import EndOfMibView, NoSuchInstance, NoSuchObject
from pysnmp.proto.rfc1902 import Integer

import moonlan.snmp_collector as collector_module
from moonlan.snmp_collector import SnmpCollector


class StubCollector(SnmpCollector):
    """No engine, no transport — only the guards under test."""

    def __init__(self):
        self._engine = None
        self._community = None
        self._walk_status = {}
        self._retries_on_break = 0
        self._per_host = {}
        self._communities = {}

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

    def test_no_such_object_comes_back_as_no_answer(self):
        """The successful reply that means "I do not have that"."""

        class _Oid(tuple):
            def __new__(cls, text):
                return super().__new__(cls, (int(p) for p in text.split(".")))

        for sentinel in (NoSuchObject(""), NoSuchInstance(""), EndOfMibView("")):
            with self.subTest(kind=type(sentinel).__name__):
                async def answering(*args, **kwargs):
                    return None, None, None, [(_Oid("1.2.3.0"), sentinel)]

                collector_module.get_cmd = answering
                value = asyncio.run(
                    self.collector._get("10.0.0.17", "1.3.6.1.4.1.171.11.153.1000.17.1.0")
                )
                self.assertIsNone(value)

    def test_a_real_value_still_comes_through(self):
        class _Oid(tuple):
            def __new__(cls, text):
                return super().__new__(cls, (int(p) for p in text.split(".")))

        async def answering(*args, **kwargs):
            return None, None, None, [(_Oid("1.2.3.0"), Integer(1))]

        collector_module.get_cmd = answering
        value = asyncio.run(self.collector._get("10.0.0.21", "1.2.3.0"))
        self.assertEqual(int(value), 1)

    def test_a_walk_row_that_says_nothing_is_not_a_row(self):
        """Same sentinel, arriving inside a walk instead of a GET."""

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
                    raise StopAsyncIteration
                value = (
                    NoSuchInstance("") if self._served == 2
                    else Integer(self._served)
                )
                return (
                    None, None, None,
                    [(_Oid(f"1.2.3.{self._served}"), value)],
                )

            async def aclose(self):
                pass

        async def open_walk(host, start):
            return _Rows()

        self.collector._open_walk = open_walk

        async def drain():
            return [row async for row in self.collector._walk("10.0.0.21", "1.2.3")]

        rows = asyncio.run(drain())
        self.assertEqual([int(v) for _suffix, v in rows], [1, 3])
        self.assertEqual(
            self.collector.last_walk_status("10.0.0.21", "1.2.3").rows, 2
        )

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
