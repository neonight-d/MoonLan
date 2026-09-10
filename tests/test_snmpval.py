"""Regression: an SNMP integer must never be read as octets.

pysnmp's numeric types implement __index__, so `bytes(value)` quietly
takes the `bytes(int)` path and allocates a zero buffer as long as the
number. `bytes(Counter32(1486971185))` asks for 1.49 GB, which is what
the OOM killer ended `diag --walk` on, and the same call sat in the
LLDP and STP parsers where it would have taken the service down.

Run with:  python -m unittest discover -s tests
"""

import tracemalloc
import unittest

from pysnmp.proto.rfc1902 import (
    Counter32,
    Counter64,
    Gauge32,
    Integer,
    IpAddress,
    OctetString,
    TimeTicks,
)

from moonlan.snmpval import as_octets, is_octets

# The value that killed the process, straight off ifOutOctets.1 of mb0
HUGE_COUNTER = 1486971185


class AsOctetsTest(unittest.TestCase):
    def test_numbers_carry_no_octets(self):
        for value in (
            Integer(3),
            Counter32(HUGE_COUNTER),
            Counter64(2**40),
            Gauge32(1000000),
            TimeTicks(353574794),
        ):
            with self.subTest(value=type(value).__name__):
                self.assertEqual(as_octets(value), b"")
                self.assertFalse(is_octets(value))

    def test_small_integer_yields_no_false_hex(self):
        """Integer(3) used to come back as three zero bytes.

        That is where the "hex: 00 00 00" printed beside
        lldpLocPortIdSubtype = 3 came from: not an encoding of three,
        three bytes of nothing.
        """
        self.assertEqual(as_octets(Integer(3)), b"")

    def test_huge_counter_allocates_nothing(self):
        tracemalloc.start()
        try:
            tracemalloc.reset_peak()
            result = as_octets(Counter32(HUGE_COUNTER))
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(result, b"")
        # the old code asked for 1.49 GB here; anything in that region
        # means the integer path is back
        self.assertLess(peak, 1_000_000)

    def test_octet_strings_pass_through(self):
        self.assertEqual(
            as_octets(OctetString(hexValue="445bed5b3942")),
            b"\x44\x5b\xed\x5b\x39\x42",
        )
        self.assertEqual(as_octets(OctetString("Library")), b"Library")
        self.assertTrue(is_octets(OctetString("Library")))

    def test_ip_address_is_octets(self):
        """IpAddress derives from OctetString and carries four of them."""
        self.assertTrue(is_octets(IpAddress("10.0.0.1")))
        self.assertEqual(as_octets(IpAddress("10.0.0.1")), b"\x0a\x00\x00\x01")

    def test_plain_python_values(self):
        self.assertEqual(as_octets(b"\x01\x02"), b"\x01\x02")
        self.assertEqual(as_octets("abc"), b"abc")
        self.assertEqual(as_octets(None), b"")
        self.assertEqual(as_octets(7), b"")


if __name__ == "__main__":
    unittest.main()
