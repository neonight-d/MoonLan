""""Not on map" in two (v0.8.7).

`_assemble_hosts()` put everything that is not on the map into one list:
devices seen only on uplinks, devices known only from ARP, records whose
port was reset — and records nobody had seen for longer than
host_grace_hours. On the network this was written for that list had 480
entries, 478 of them gone for days; the two devices that are there now
and have no place were lost in it.

Each record off the map now says whether anybody saw it inside
host_grace_hours (`recent`) and why it is off the map (`off_map`). The
page shows the recent ones as before, and the rest folded under "Not
seen for a while", which the search opens when it finds something there.

Run with:  python -m unittest discover -s tests
"""

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from service_fixture import SWITCHES  # noqa: E402,F401

from moonlan import server  # noqa: E402
from web_source import NODE, run_page  # noqa: E402

CORE = SWITCHES[0]
UPLINK_ONLY = "aa:bb:cc:00:01:01"
ARP_ONLY = "aa:bb:cc:00:01:02"
EXPIRED = "aa:bb:cc:00:01:03"
EXPIRED_PINGED = "aa:bb:cc:00:01:04"
RESET = "aa:bb:cc:00:01:05"
HOUR = 3600


def row(mac, *, switch="", port="", ip="", last_seen=0.0, last_arp=0.0,
        last_ping_ok=0.0):
    return {
        "mac": mac, "ip": ip, "name": "", "switch_ip": switch, "port": port,
        "vlan": 0, "first_seen": last_seen or last_arp, "last_seen": last_seen,
        "last_arp": last_arp, "last_ping_ok": last_ping_ok, "ping_up": 0,
        "ip_confirmed": last_arp, "seen_count": 2, "confirmed": 1,
        "corrupt_of": "", "monitored": 0,
    }


class RecordsTest(unittest.TestCase):
    def setUp(self):
        self._grace = server.config.host_grace_hours
        self._days = server.config.host_retention_days
        server.config.host_grace_hours = 72
        server.config.host_retention_days = 30

    def tearDown(self):
        server.config.host_grace_hours = self._grace
        server.config.host_retention_days = self._days

    def assemble(self):
        now = time.time()
        rows = {
            UPLINK_ONLY: row(UPLINK_ONLY, last_seen=now),
            # ARP saw it yesterday; no switch port ever did
            ARP_ONLY: row(ARP_ONLY, ip="10.1.2.3", last_arp=now - 24 * HOUR),
            # on a port of a switch on the map, gone for four days
            EXPIRED: row(EXPIRED, switch=CORE, port="Gi0/5",
                         last_seen=now - 96 * HOUR),
            # …and one of those still answers ping
            EXPIRED_PINGED: row(EXPIRED_PINGED, switch=CORE, port="Gi0/6",
                                ip="10.1.2.4", last_seen=now - 96 * HOUR,
                                last_ping_ok=now - HOUR),
            # a port the poll had no name for (v0.8.6), seen an hour ago
            RESET: row(RESET, switch=CORE, port="", last_seen=now - HOUR),
        }
        _hosts, _groups, unlocated, _trunks = server._assemble_hosts(
            [], rows, [], {CORE},
            uplink_only={UPLINK_ONLY: [(CORE, "Gi0/26")]},
        )
        return now, {h["mac"]: h for h in unlocated}

    def test_recent_and_why(self):
        now, off = self.assemble()
        self.assertEqual(
            {mac: (h["recent"], h["off_map"]) for mac, h in off.items()},
            {
                UPLINK_ONLY: (True, "uplink_only"),
                ARP_ONLY: (True, "no_port"),
                EXPIRED: (False, "expired"),
                EXPIRED_PINGED: (True, "expired"),
                RESET: (True, "no_port"),
            },
        )

    def test_when_seen_and_when_forgotten(self):
        now, off = self.assemble()
        self.assertAlmostEqual(off[EXPIRED]["seen_at"], now - 96 * HOUR, delta=2)
        self.assertAlmostEqual(
            off[EXPIRED]["forget_at"], now - 96 * HOUR + 30 * 86400, delta=2
        )
        # ARP counts toward both
        self.assertAlmostEqual(off[ARP_ONLY]["seen_at"], now - 24 * HOUR, delta=2)
        # …a ping only toward "seen": purge_old_hosts goes by FDB and ARP
        self.assertAlmostEqual(off[EXPIRED_PINGED]["seen_at"], now - HOUR, delta=2)
        self.assertAlmostEqual(
            off[EXPIRED_PINGED]["forget_at"], now - 96 * HOUR + 30 * 86400,
            delta=2,
        )

    def test_retention_zero_forgets_nothing(self):
        server.config.host_retention_days = 0
        _now, off = self.assemble()
        self.assertIsNone(off[EXPIRED]["forget_at"])


class StatusTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._unlocated = server.state.unlocated

    def tearDown(self):
        server.state.unlocated = self._unlocated

    async def test_the_counters(self):
        server.state.unlocated = [
            {"mac": "aa:00:00:00:00:01", "recent": True},
            {"mac": "aa:00:00:00:00:02", "recent": False},
            {"mac": "aa:00:00:00:00:03", "recent": False},
        ]
        status = await server.api_status()
        # all of them, as before: nobody's script breaks
        self.assertEqual(status["unlocated_count"], 3)
        self.assertEqual(status["unlocated_recent"], 1)
        self.assertEqual(status["unlocated_gone"], 2)


PAGE = r"""
// just enough of an element for the lists beside the map
const fakeEl = () => {
  const el = {
    children: [], attrs: {}, classes: new Set(), dataset: {}, textContent: "",
    replaceChildren(...items) { this.children = items; },
    querySelectorAll() { return this.children; },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
  };
  el.classList = {
    toggle(c, on) { (on === undefined ? !el.classes.has(c) : on) ? el.classes.add(c) : el.classes.delete(c); },
    add(c) { el.classes.add(c); },
    remove(c) { el.classes.delete(c); },
    contains(c) { return el.classes.has(c); },
  };
  return el;
};
for (const key of ["namedList", "namedCount", "namedSection", "switchList", "switchCount",
                   "hostList", "hostCount", "unlocatedList", "unlocatedCount",
                   "unlocatedSection", "goneSection", "goneToggle", "goneList", "goneCount"]) {
  els[key] = fakeEl();
}
els.search = { value: "" };
li = (main, sub, dot, onClick, search) => {
  const item = fakeEl();
  item.dataset.search = (search || main + " " + sub).toLowerCase();
  item.main = main;
  item.sub = sub;
  return item;
};
listTabStop = () => {};
const now = Date.now() / 1000;
topology = {
  switches: [{ ip: "10.0.0.1", name: "core-sw", named: true }],
  hosts: [], links: [], pseudo_switches: [], bridges: [], external_networks: [],
  offline_groups: [], trunk_groups: [],
  unlocated: [
    { mac: "aa:bb:cc:00:01:02", ip: "10.1.2.3", name: "", recent: true, seen_at: now - 86400 },
    { mac: "aa:bb:cc:00:01:03", ip: "", name: "", recent: false, seen_at: now - 4 * 86400,
      switch: "10.0.0.1", port: "Gi0/5" },
    { mac: "aa:bb:cc:00:01:09", ip: "", name: "", recent: false, seen_at: now - 2 * 86400,
      switch: "10.0.0.1", port: "Gi0/9" },
  ],
};
lang = "ru";
const out = {};
renderSidebar();
out.recent = els.unlocatedList.children.map((li) => li.main);
out.gone = els.goneList.children.map((li) => [li.main, li.sub]);
out.goneCount = els.goneCount.textContent;
out.folded = els.goneList.classes.has("hidden");
out.expanded = els.goneToggle.getAttribute("aria-expanded");
// a MAC that is only in the folded list opens it
els.search.value = "01:03";
applySearchFilter();
out.searchOpens = !els.goneList.classes.has("hidden");
out.searchCount = els.goneCount.textContent;
// …and the folding comes back with an empty query
els.search.value = "";
applySearchFilter();
out.backFolded = els.goneList.classes.has("hidden");
// a click opens it and the browser remembers
toggleGone();
out.clicked = [!els.goneList.classes.has("hidden"), localStorage.getItem("moonlan-gone-open")];
renderSidebar();
out.stillOpen = !els.goneList.classes.has("hidden");
__done(out);
"""


@unittest.skipUnless(NODE, "needs node to run the page's JavaScript")
class PageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_page(PAGE)

    def test_two_lists(self):
        self.assertEqual(self.out["recent"], ["10.1.2.3"])
        # the newest first, with when and where it was seen last
        self.assertEqual(
            [main for main, _sub in self.out["gone"]],
            ["aa:bb:cc:00:01:09", "aa:bb:cc:00:01:03"],
        )
        self.assertIn("был ", self.out["gone"][0][1])
        self.assertIn("core-sw Gi0/9", self.out["gone"][0][1])
        self.assertEqual(self.out["goneCount"], "2")

    def test_folded_until_opened_or_searched(self):
        self.assertTrue(self.out["folded"])
        self.assertEqual(self.out["expanded"], "false")
        self.assertTrue(self.out["searchOpens"])
        self.assertEqual(self.out["searchCount"], "1 из 2")
        self.assertTrue(self.out["backFolded"])
        self.assertEqual(self.out["clicked"], [True, "1"])
        self.assertTrue(self.out["stillOpen"])


if __name__ == "__main__":
    unittest.main()
