"""The line under a row does not repeat the row (v0.8.8).

The first line of a device in the lists is its name, or what hostLabel
finds: DNS, IP, LLDP, MAC. The repeats were weeded out only among the
parts of the second line, never against the first, so a device with
neither a name nor an IP showed its MAC twice — plain in "Not seen for
a while" — and one with an IP alone its IP twice, in the hosts and in
"Not on map" too. One helper for the three lists now leaves out what
the first line says.

Run with:  python -m unittest discover -s tests
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from web_source import NODE, function, run_page  # noqa: E402

PAGE = r"""
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
    add(c) { el.classes.add(c); }, remove(c) { el.classes.delete(c); },
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
li = (main, sub) => {
  const item = fakeEl();
  item.main = main;
  item.sub = sub;
  return item;
};
listTabStop = () => {};
const now = Date.now() / 1000;
const bare = { mac: "aa:bb:cc:00:03:01", ip: "", name: "" };
const byIp = { mac: "aa:bb:cc:00:03:02", ip: "10.1.2.7", name: "" };
const noted = { mac: "aa:bb:cc:00:03:03", ip: "10.1.2.8", name: "printer-2",
                note: { label: "Front desk printer" } };
const dns = { mac: "aa:bb:cc:00:03:04", ip: "10.1.2.9", name: "nas.local" };
topology = {
  switches: [{ ip: "10.0.0.1", name: "core-sw", named: true }],
  hosts: [bare, byIp, noted, dns].map((h) => ({ ...h, switch: "10.0.0.1", port: "Gi0/2" })),
  links: [], pseudo_switches: [], bridges: [], external_networks: [],
  offline_groups: [], trunk_groups: [],
  unlocated: [
    ...[bare, byIp, noted].map((h) => ({ ...h, mac: h.mac.replace("03", "04"), recent: true })),
    ...[bare, byIp].map((h) => ({ ...h, mac: h.mac.replace("03", "05"), recent: false,
                                    seen_at: now - 5 * 86400, seen_by: "fdb",
                                    switch: "10.0.0.1", port: "Gi0/9" })),
  ],
};
lang = "en";
renderSidebar();
const rows = (list) => list.children.map((item) => [item.main, item.sub]);
__done({ hosts: rows(els.hostList), unlocated: rows(els.unlocatedList), gone: rows(els.goneList) });
"""


@unittest.skipUnless(NODE, "needs node to run the page's JavaScript")
class PageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_page(PAGE)

    def check(self, rows, mac_part):
        bare, by_ip = rows[0], rows[1]
        # neither a name nor an IP: the MAC once
        self.assertEqual(bare[0], f"aa:bb:cc:00:{mac_part}:01")
        self.assertNotIn(bare[0], bare[1])
        # an IP and no name: the IP once, the MAC under it
        self.assertEqual(by_ip[0], "10.1.2.7")
        self.assertNotIn("10.1.2.7", by_ip[1])
        self.assertIn(f"aa:bb:cc:00:{mac_part}:02", by_ip[1])

    def test_hosts(self):
        rows = self.out["hosts"]
        self.check(rows, "03")
        self.assertEqual(rows[0][1], "")
        self.assertEqual(rows[1][1], "aa:bb:cc:00:03:02")
        # a note's label first, and what hostLabel finds stays under it
        self.assertEqual(rows[2][0], "Front desk printer")
        self.assertEqual(rows[2][1], "printer-2 · 10.1.2.8 · aa:bb:cc:00:03:03")
        # a DNS name first, the IP under it
        self.assertEqual(rows[3], ["nas.local", "10.1.2.9 · aa:bb:cc:00:03:04"])

    def test_not_on_map(self):
        rows = self.out["unlocated"]
        self.check(rows, "04")
        self.assertEqual(rows[2][1], "printer-2 · 10.1.2.8 · aa:bb:cc:00:04:03")

    def test_not_seen_for_a_while(self):
        rows = self.out["gone"]
        self.check(rows, "05")
        self.assertNotIn("aa:bb:cc:00:05:01 ·", rows[0][1])
        self.assertTrue(rows[0][1].startswith("seen "), rows[0][1])
        self.assertIn("core-sw Gi0/9", rows[0][1])


class SourceTest(unittest.TestCase):
    def test_one_helper_for_the_three_lists(self):
        sidebar = function("renderSidebar")
        self.assertEqual(sidebar.count("rowSubline(h, "), 3)
        self.assertEqual(sidebar.count("rowTitle(h)"), 3)
        self.assertNotIn(".filter((part, i, all) => part && all.indexOf(part) === i)", sidebar)


if __name__ == "__main__":
    unittest.main()
