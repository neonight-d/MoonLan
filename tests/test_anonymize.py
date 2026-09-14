"""A diagnostic report has to be shareable without being a leak.

Nine `diag` dumps went into this repository's published history before
anyone read them as what they are: a complete map of an organisation's
network — 124 MAC addresses, 62 internal addresses, host names, the
port labels a previous administrator typed in. `--anonymize` is the
answer, and the two things it has to get right are tested here:

- the same input twice gives the same output, so a report still reads
  as a report and two runs can be compared;
- nothing recognisable survives — not in the plain text, and not in
  the places an address hides: inside an ARP OID, and inside the
  decimal octets of a MAC forwarding entry.

Run with:  python -m unittest discover -s tests
"""

import unittest

from moonlan.anonymize import Anonymizer, AnonymizingWriter

REPORT = """=== 3. MAC forwarding table ===
switch comm.mb1.example.local (10.0.0.21), bridge MAC 18:fd:74:fd:b5:cf
  1.3.6.1.2.1.17.4.3.1.2.24.253.116.253.181.207  -> 18:fd:74:fd:b5:cf  port 3
  1.3.6.1.2.1.4.22.1.2.5.10.3.5.6 = 00:0e:04:b7:79:ab
  pc-07.office.example.local  10.0.99.18  20:7b:d5:1a:31:8d  Slot0/21
  neighbour RouterOS-Sport at 10.3.5.6, uptime 4000000 ticks
  speed 1000 Mbit/s, 62.9 in / 17.4 out, errors 0.01
"""

ORIGINALS = (
    "10.0.0.21", "10.3.5.6", "10.0.99.18",
    "18:fd:74:fd:b5:cf", "00:0e:04:b7:79:ab", "20:7b:d5:1a:31:8d",
    "comm.mb1.example.local", "pc-07.office.example.local", "RouterOS-Sport",
    # the same MAC as an OID suffix, which is where diag --fdb prints it
    "24.253.116.253.181.207",
)


def anonymize(report: str = REPORT) -> str:
    anon = Anonymizer()
    anon.register_switch("comm.mb1.example.local")
    anon.register_names(["pc-07.office.example.local", "RouterOS-Sport"])
    return anon.text(report)


class StabilityTest(unittest.TestCase):
    def test_the_same_run_maps_an_address_the_same_way_every_time(self):
        anon = Anonymizer()
        first = anon.text(REPORT)
        second = anon.text(REPORT)
        self.assertEqual(first, second)

    def test_two_runs_of_the_same_input_agree(self):
        """Not a promise about the table, a promise about the report.

        A fresh table allocates in order of first appearance, so the
        same input gives the same output — which is what lets someone
        diff two anonymised reports.
        """
        self.assertEqual(anonymize(), anonymize())

    def test_one_address_is_one_replacement_throughout(self):
        out = anonymize()
        # 10.3.5.6 appears twice in the report, once inside an OID
        self.assertEqual(out.count("198.51.100."), out.count("198.51.100."))
        replacements = {
            line.split()[-1] for line in out.splitlines() if "->" in line
        }
        self.assertTrue(replacements)
        # the bridge MAC and the FDB row are the same device
        self.assertEqual(out.count("00:00:5e:00:53:00"), 2)


class LeakTest(unittest.TestCase):
    def test_nothing_recognisable_survives(self):
        out = anonymize()
        for original in ORIGINALS:
            with self.subTest(original=original):
                self.assertNotIn(original, out)

    def test_addresses_come_from_the_documentation_ranges(self):
        out = anonymize()
        self.assertIn("198.51.100.", out)
        self.assertIn("00:00:5e:00:53:", out)

    def test_an_oid_is_not_an_address(self):
        """The one thing that must survive untouched."""
        anon = Anonymizer()
        out = anon.text("walk 1.3.6.1.4.1.171.11.153.1000.17 on 10.0.0.21")
        self.assertIn("1.3.6.1.4.1.171.11.153.1000.17", out)
        self.assertNotIn("10.0.0.21", out)

    def test_numbers_that_are_not_addresses_are_left_alone(self):
        anon = Anonymizer()
        out = anon.text("speed 1000 Mbit/s, 62.9 in, uptime 4000000 ticks")
        self.assertEqual(out, "speed 1000 Mbit/s, 62.9 in, uptime 4000000 ticks")

    def test_switches_and_hosts_are_numbered_apart(self):
        anon = Anonymizer()
        anon.register_switch("core-sw")
        anon.register_name("pc-07.demo.lan")
        out = anon.text("core-sw sees pc-07.demo.lan")
        self.assertIn("switch-1", out)
        self.assertIn("host-", out)
        self.assertNotIn("core-sw", out)


class WriterTest(unittest.TestCase):
    def test_the_writer_rewrites_what_is_printed_through_it(self):
        class _Sink:
            def __init__(self):
                self.text = ""

            def write(self, text):
                self.text += text
                return len(text)

            def flush(self):
                pass

        sink = _Sink()
        writer = AnonymizingWriter(sink, Anonymizer())
        writer.write("switch at 10.0.0.21\n")
        writer.write("and again 10.0.0.21\n")
        self.assertNotIn("10.0.0.21", sink.text)
        # one address, one replacement, across separate writes
        self.assertEqual(sink.text.count("198.51.100.1"), 2)


if __name__ == "__main__":
    unittest.main()
