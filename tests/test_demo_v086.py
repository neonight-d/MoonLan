"""The demo of v0.8.6: what this version changed, on the virtual network.

Two scans and two counters cycles of the demo, in a process of its own
(the service reads its configuration once, at import):

- the D-Link-style trunks are "LAG …" on the link, the group behind it
  and the ports panel, and the note with the old key is renamed;
- access-sw-4 answers a row on bridge-port 402792706, which is dropped
  with one WARNING for the process;
- the core has Loop Detection off and spanning tree running: no
  alarm, while access-sw-3 (off, no tree) keeps it;
- the journal has an event of every group.

Run with:  python -m unittest discover -s tests
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = r"""
import asyncio, json, logging
records = []


class Grab(logging.Handler):
    def emit(self, record):
        records.append([record.levelname, record.getMessage()])


root = logging.getLogger("moonlan")
root.addHandler(Grab())
root.setLevel(logging.INFO)
root.propagate = False
from moonlan import demo, server
from asgi_client import call


async def run():
    demo.seed_notes(server.rt.db)
    for _ in range(2):
        await server.run_scan()
        await server.run_counters()

asyncio.run(run())
topo = call(server.app, "GET", "/api/topology").json()
out = {
    "topology": topo,
    "core_ports": call(server.app, "GET", "/api/switch/10.0.0.10/ports").json()["ports"],
    "ray4_ports": [p["name"] for p in call(
        server.app, "GET", "/api/switch/10.0.0.24/ports").json()["ports"]],
    "alarms": sorted({(a["type"], a["subject"]) for a in call(
        server.app, "GET", "/api/alarms").json()["alarms"]
        if a["type"] == "loop_detection_disabled"}),
    "groups": sorted({e["group"] for e in call(
        server.app, "GET", "/api/journal?limit=1000").json()["events"]}),
    "notes": sorted(server.rt.db.notes()),
    "records": records,
}
print(json.dumps(out))
"""


class DemoTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.yaml"
            config.write_text(f"db_path: {Path(tmp) / 'moonlan.db'}\n", encoding="utf-8")
            env = {**os.environ, "MOONLAN_DEMO": "1", "MOONLAN_CONFIG": str(config),
                   "PYTHONPATH": os.pathsep.join([str(root), str(root / "tests")])}
            done = subprocess.run([sys.executable, "-c", SCRIPT], cwd=tmp, env=env,
                                  capture_output=True, text=True, timeout=300, check=True)
        cls.out = json.loads(done.stdout.strip().splitlines()[-1])

    def test_the_trunks_are_named_by_their_members(self):
        links = self.out["topology"]["links"]
        ends = {(link["a"], link["a_port"]) for link in links} | {
            (link["b"], link["b_port"]) for link in links}
        self.assertIn(("10.0.0.10", "LAG Gi0/3+Gi0/4"), ends)
        self.assertIn(("10.0.0.23", "LAG Gi0/23+Gi0/24"), ends)
        self.assertFalse([e for e in ends if e[1].startswith("bridge-port")])

    def test_the_aggregate_row_in_the_ports_panel(self):
        row = next(p for p in self.out["core_ports"] if p["name"] == "LAG Gi0/3+Gi0/4")
        self.assertTrue(row["aggregate"])
        self.assertEqual(row["members"], ["Gi0/3", "Gi0/4"])
        self.assertTrue(row["oper_up"])
        self.assertEqual(row["speed_mbps"], 2000)

    def test_the_old_note_key_is_renamed(self):
        self.assertIn("trunk:10.0.0.10:LAG Gi0/3+Gi0/4", self.out["notes"])
        self.assertNotIn("trunk:10.0.0.10:bridge-port 3", self.out["notes"])

    def test_the_unknown_bridge_port_is_dropped_once_said(self):
        warnings = [m for level, m in self.out["records"]
                    if level == "WARNING" and "bridge-port 402792706" in m]
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("00:1b:2c:3d:4e:5f", warnings[0])
        self.assertTrue(any("unknown bridge-port: 1" in m for _l, m in self.out["records"]))
        self.assertFalse([n for n in self.out["ray4_ports"] if re.fullmatch(r"-?\d+", n)
                          or n.startswith("bridge-port")])

    def test_no_negative_number_anywhere(self):
        found = []

        def walk(value):
            if isinstance(value, dict):
                for item in value.values():
                    walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)
            elif isinstance(value, str) and (re.fullmatch(r"-\d+", value)
                                             or re.search(r":-\d+$", value)):
                found.append(value)

        walk(self.out["topology"])
        self.assertEqual(found, [])

    def test_the_database_follows_the_new_names(self):
        # the core's renamed note key, in one line; nothing from the
        # second scan, which has nothing left to rename
        lines = [m for _l, m in self.out["records"] if "before v0.8.6" in m]
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].startswith("10.0.0.10:"))
        self.assertIn("1 layout/note key(s) renamed", lines[0])

    def test_loop_detection_off_with_a_tree_raises_nothing(self):
        alarms = [subject for _type, subject in self.out["alarms"]]
        self.assertNotIn("10.0.0.10", alarms)
        self.assertIn("10.0.0.23", alarms)

    def test_the_journal_has_every_group(self):
        self.assertTrue(
            {"alarms", "ping", "devices", "map", "users", "settings"} <= set(self.out["groups"]),
            self.out["groups"],
        )


if __name__ == "__main__":
    unittest.main()
