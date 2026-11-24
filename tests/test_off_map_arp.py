""""Not on map": ARP is a sign of presence while the address is held (v0.8.8).

Of 142 records "Not on map" on the network this was written for, 140
had no IP. `_mark_recent` counted `last_arp` toward "recent", and
`last_arp` stays when the address goes — to another MAC (set_ips) or
nowhere, unconfirmed (release_ip) — so a record whose address had
moved on stayed "recent" for host_grace_hours after the very ARP that
showed the device was gone. ARP now counts only for a record that holds
an address; the switches' tables and the pings count as before.
`seen_at` stays the last sighting of any kind, and `seen_by` says
which; in "Not seen for a while" such a row says it was in ARP and no
longer holds an address, or its fresh date would look like a mistake.

Run with:  python -m unittest discover -s tests
"""

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import service_fixture  # noqa: E402,F401  (a config for server)

from moonlan import server  # noqa: E402
from web_source import NODE, run_page  # noqa: E402

HOUR = 3600
MAC = "aa:bb:cc:00:02:01"


def mark(*, ip="", last_seen=0.0, last_arp=0.0, last_ping_ok=0.0, uplink_only=None):
    now = time.time()
    row = {"mac": MAC, "ip": ip, "last_seen": last_seen and now - last_seen,
           "last_arp": last_arp and now - last_arp,
           "last_ping_ok": last_ping_ok and now - last_ping_ok}
    host = {"mac": MAC, "ip": ip}
    if uplink_only:
        host["uplink_only"] = uplink_only
    server._mark_recent([host], {MAC: row}, now)
    return host, now


class RecentTest(unittest.TestCase):
    def setUp(self):
        self._grace = server.config.host_grace_hours
        server.config.host_grace_hours = 72

    def tearDown(self):
        server.config.host_grace_hours = self._grace

    def test_arp_without_an_address_is_not_presence(self):
        host, now = mark(last_arp=HOUR, last_seen=200 * HOUR)
        self.assertFalse(host["recent"])
        self.assertEqual(host["seen_by"], "arp")
        # the last sighting all the same
        self.assertAlmostEqual(host["seen_at"], now - HOUR, delta=2)

    def test_arp_with_an_address_is(self):
        host, _now = mark(ip="10.1.2.3", last_arp=HOUR, last_seen=200 * HOUR)
        self.assertTrue(host["recent"])
        self.assertEqual(host["seen_by"], "arp")

    def test_a_fresh_fdb_without_an_address_is(self):
        host, _now = mark(last_seen=HOUR, last_arp=200 * HOUR)
        self.assertTrue(host["recent"])
        self.assertEqual(host["seen_by"], "fdb")

    def test_a_ping_without_an_address_is(self):
        host, _now = mark(last_ping_ok=HOUR, last_seen=200 * HOUR)
        self.assertTrue(host["recent"])
        self.assertEqual(host["seen_by"], "ping")

    def test_an_uplink_is_recent_by_definition(self):
        host, _now = mark(last_arp=HOUR, uplink_only=[("10.0.0.1", "Gi0/26")])
        self.assertTrue(host["recent"])

    def test_never_seen(self):
        host, _now = mark()
        self.assertFalse(host["recent"])
        self.assertEqual(host["seen_by"], "")


PAGE = r"""
const at = new Date(2026, 10, 23, 14, 5).getTime() / 1000;
const out = {};
for (const code of ["ru", "en"]) {
  lang = code;
  out[code] = [
    goneSeenText({ seen_by: "arp", ip: "", seen_at: at }),
    goneSeenText({ seen_by: "arp", ip: "10.1.2.3", seen_at: at }),
    goneSeenText({ seen_by: "fdb", ip: "", seen_at: at }),
  ];
}
__done(out);
"""


@unittest.skipUnless(NODE, "needs node to run the page's JavaScript")
class PageTest(unittest.TestCase):
    def test_the_row_says_why_its_fresh_date_is_here(self):
        out = run_page(PAGE)
        arp, held, fdb = out["ru"]
        self.assertTrue(arp.startswith("в ARP был 23.11"), arp)
        self.assertIn("14:05", arp)
        self.assertTrue(arp.endswith("· адреса за ним больше нет"), arp)
        self.assertTrue(held.startswith("был 23.11"), held)
        self.assertTrue(fdb.startswith("был 23.11"), fdb)
        self.assertTrue(out["en"][0].startswith("in ARP at "), out["en"][0])
        self.assertTrue(out["en"][0].endswith("· no longer holds an address"))


if __name__ == "__main__":
    unittest.main()
