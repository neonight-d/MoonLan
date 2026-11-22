"""The demo of v0.8.7: what this version changed, on the virtual network.

The start and five scans of the demo, in a process of its own (the
service reads its configuration once, at import):

- a fresh demo database holds the reading of access-sw-3: before the
  first scan it is on the map as restored, the other switches as empty
  nodes, and the first scan is still the initial inventory;
- 10.0.0.26 runs out of its budget every scan and was never read in
  full: an empty node named "Annex", standing on the core's Gi0/16;
- access-sw-5 does not answer on scans 3 and 4: drawn from its reading,
  switch_down raised on the second silent scan and cleared on scan 5;
- "Not seen for a while" has the records nobody saw for days, and the
  journal has ip_released in its own group.

Run with:  python -m unittest discover -s tests
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = r"""
import asyncio, json, logging
from moonlan import demo, server

logging.getLogger("moonlan").setLevel(logging.ERROR)


async def topology():
    return json.loads((await server.api_topology()).body)


async def states():
    return {sw["ip"]: sw["state"] for sw in (await topology())["switches"]}


def down_alarm():
    return any(
        a["type"] == "switch_down" and a["subject"] == demo.DOWN_SWITCH
        for a in server.db.alarms(True)
    )


async def journal(group=None):
    answer = await server.api_journal(limit=1000, group=group, before=None)
    return answer["events"]


async def run():
    out = {"scans": []}
    demo.seed_notes(server.db)
    demo.seed_reading(server.db)
    await server.restore_readings()
    out["restored"] = await states()
    out["hosts_before_scan"] = len(server.db.hosts_by_mac())
    for n in range(1, 6):
        await server.run_scan()
        await server.run_counters()
        topo = await topology()
        out["scans"].append({
            "states": await states(),
            "down_alarm": down_alarm(),
            "unconfirmed": len(server.db.unconfirmed_macs()),
            "new_mac_alarms": server.db._conn.execute(
                "SELECT COUNT(*) FROM alarms WHERE type = 'new_mac'"
            ).fetchone()[0],
        })
        if n == 1:
            out["first"] = topo
            out["orphans"] = [
                key for key, row in server.db.notes().items()
                if row.get("missing_since")
            ]
            out["status"] = await server.api_status()
        if n == 3:
            out["silent"] = topo
    out["groups"] = sorted({e["group"] for e in await journal()})
    out["addresses"] = len(await journal(["addresses"]))
    out["devices_ip_released"] = sum(
        1 for e in await journal(["devices"]) if e["event"] == "ip_released"
    )
    print(json.dumps(out))

asyncio.run(run())
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

    def test_restored_before_the_first_scan(self):
        restored = self.out["restored"]
        self.assertEqual(restored["10.0.0.23"], "restored")
        self.assertEqual(
            {ip: state for ip, state in restored.items() if ip != "10.0.0.23"},
            {ip: "never" for ip in restored if ip != "10.0.0.23"},
        )
        # a reading, not a record: the first scan is the initial inventory
        self.assertEqual(self.out["hosts_before_scan"], 0)
        first = self.out["scans"][0]
        # every address confirmed in one poll, and none of it announced
        self.assertEqual(first["unconfirmed"], 0)
        self.assertEqual(first["new_mac_alarms"], 0)
        self.assertEqual(first["states"]["10.0.0.23"], "fresh")

    def test_the_switch_that_never_finishes(self):
        for scan in self.out["scans"]:
            self.assertEqual(scan["states"]["10.0.0.26"], "never")
        stub = next(sw for sw in self.out["first"]["switches"] if sw["ip"] == "10.0.0.26")
        self.assertIs(stub["answered"], True)
        self.assertEqual(stub["attached"]["switch"], "10.0.0.10")
        self.assertEqual(stub["attached"]["port"], "Gi0/16")
        self.assertEqual(stub["note"]["label"], "Annex")
        self.assertNotIn("sw:10.0.0.26", self.out["orphans"])
        self.assertNotIn("00:1e:58:a0:00:26", {h["mac"] for h in self.out["first"]["hosts"]})

    def test_the_switch_that_goes_quiet_and_comes_back(self):
        scans = self.out["scans"]
        self.assertEqual([s["states"]["10.0.0.25"] for s in scans],
                         ["fresh", "fresh", "unreachable", "unreachable", "fresh"])
        self.assertEqual([s["down_alarm"] for s in scans],
                         [False, False, False, True, False])
        silent = self.out["silent"]
        sw = next(s for s in silent["switches"] if s["ip"] == "10.0.0.25")
        self.assertIs(sw["answered"], False)
        self.assertGreater(sw["down_since"], 0)
        # still linked to the core, from its reading
        self.assertTrue([
            link for link in silent["links"] if "10.0.0.25" in (link["a"], link["b"])
        ])
        # the slow one is late, as before
        self.assertEqual(scans[1]["states"]["10.0.0.22"], "over_budget")

    def test_not_seen_for_a_while(self):
        gone = [h for h in self.out["first"]["unlocated"] if h["recent"] is False]
        self.assertGreaterEqual(len(gone), 5)
        self.assertTrue(all(h["off_map"] == "expired" for h in gone))
        status = self.out["status"]
        self.assertEqual(
            status["unlocated_recent"] + status["unlocated_gone"],
            status["unlocated_count"],
        )
        self.assertGreaterEqual(status["unlocated_gone"], 5)
        self.assertGreater(status["unlocated_recent"], 0)

    def test_addresses_have_their_own_group(self):
        self.assertIn("addresses", self.out["groups"])
        self.assertGreaterEqual(self.out["addresses"], 3)
        self.assertEqual(self.out["devices_ip_released"], 0)


if __name__ == "__main__":
    unittest.main()
