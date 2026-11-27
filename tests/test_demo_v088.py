"""The demo of v0.8.8: what this version changed, on the virtual network.

The start and two scans of the demo, in a process of its own (the
service reads its configuration once, at import):

- /api/polling says where the time of each poll went: the two switches
  that run out of budget are cut in a walk (`cut_at`), the rest have
  their `slowest_walks` — made-up numbers in the real shape;
- the switch that has never been read in full carries switch_stale as
  an older version left it before a restart — a count of scans, the
  date of a reading that is not on the map — and the first scan brings
  the text up to date in place, without a second raise;
- "Not seen for a while" has records seen in ARP hours ago that hold no
  address any more, `seen_by: "arp"`.

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
from moonlan import demo, poller, server

logging.getLogger("moonlan").setLevel(logging.ERROR)


def stale():
    return [a for a in server.rt.db.alarms(True) if a["type"] == "switch_stale"]


async def run():
    out = {}
    demo.seed_notes(server.rt.db)
    demo.seed_reading(server.rt.db)
    demo.seed_stale_alarm(server.rt.db)
    await server.rt.alarm_engine.load()
    await poller.restore_readings()
    out["before"] = [a["message"] for a in stale()]
    await poller.run_scan()
    out["after"] = [(a["subject"], a["message"]) for a in stale()]
    await poller.run_scan()
    out["polling"] = (await server.api_polling())["switches"]
    out["raised"] = sum(
        1 for e in server.rt.db.journal(1000)
        if e["event"] == "alarm_raised" and "switch_stale" in e["details"]
    )
    topo = json.loads((await server.api_topology()).body)
    out["unlocated"] = topo["unlocated"]
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

    def test_where_the_time_went(self):
        rows = {row["ip"]: row for row in self.out["polling"]}
        for ip in ("10.0.0.26", "10.0.0.22"):
            cut = rows[ip]["cut_at"]
            self.assertTrue(cut["in_walk"], ip)
            self.assertTrue(cut["oid"] and cut["name"], ip)
            self.assertGreater(cut["seconds"], 100)
        self.assertEqual(rows["10.0.0.26"]["cut_at"]["name"], "Q_FDB_PORT")
        fresh = [row for ip, row in rows.items() if row["state"] == "fresh"]
        self.assertTrue(fresh)
        for row in fresh:
            self.assertIsNone(row["cut_at"])
            walks = row["slowest_walks"]
            self.assertTrue(0 < len(walks) <= 3, row["ip"])
            seconds = [w["seconds"] for w in walks]
            self.assertEqual(seconds, sorted(seconds, reverse=True))

    def test_the_stale_alarm_is_brought_up_to_date(self):
        self.assertEqual(len(self.out["before"]), 1)
        self.assertIn("scans running", self.out["before"][0])
        self.assertIn("the data on the map is from", self.out["before"][0])
        (subject, message), = self.out["after"]
        self.assertEqual(subject, "10.0.0.26")
        self.assertIn("not finished a full poll since the service started at", message)
        self.assertIn("the map shows an empty node", message)
        self.assertNotIn("scans running", message)
        # the raise of a week ago, and no other
        self.assertEqual(self.out["raised"], 1)

    def test_arp_without_an_address_is_long_gone(self):
        arp = [h for h in self.out["unlocated"]
               if h.get("seen_by") == "arp" and not h.get("ip")]
        self.assertGreaterEqual(len(arp), 3)
        self.assertTrue(all(h["recent"] is False for h in arp))


if __name__ == "__main__":
    unittest.main()
