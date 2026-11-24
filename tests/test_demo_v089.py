"""The demo of v0.8.9: what this version changed, on the virtual network.

The start, a scan and twenty ping cycles of the demo, in a process of
its own (the service reads its configuration once, at import):

- at ping cycle 12 every device behind access-sw-2 Gi0/5 goes silent at
  once: port_hosts_down; at cycle 16 one of them answers again — not
  half yet, so the alarm stands, and its text says who is still silent,
  in place: one row, one raise in the journal; at cycle 20 most of them
  are back and it clears;
- "Not seen for a while" has a record that answered ping hours ago and
  holds no address any more, `seen_by: "ping"`.

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


def mass():
    return [a["message"] for a in server.db.alarms(True) if a["type"] == "port_hosts_down"]


async def run():
    out = {"texts": {}}
    demo.seed_notes(server.db)
    demo.seed_reading(server.db)
    await server.restore_readings()
    await server.run_scan()
    while demo._ping_cycle < demo.MASS_RECOVER_AT:
        await server.run_ping()
        out["texts"][demo._ping_cycle] = mass()
    out["raised"] = [
        e["details"] for e in server.db.journal(2000)
        if e["event"] == "alarm_raised" and "port_hosts_down" in e["details"]
    ]
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
        cls.texts = {int(k): v for k, v in cls.out["texts"].items()}

    def test_part_of_the_port_comes_back_and_the_text_follows(self):
        down, partly = self.texts[12], self.texts[16]
        self.assertEqual(len(down), 1)
        self.assertEqual(len(partly), 1)
        self.assertRegex(down[0], r"^(\d+) devices went silent at once at .*; still silent: (\d+) of \1: ")
        n = int(down[0].split()[0])
        self.assertIn(f"still silent: {n - 1} of {n}: ", partly[0])
        # the same until then, and one raise only, with its first words
        self.assertEqual(self.texts[15], down)
        self.assertEqual(len(self.out["raised"]), 1)
        self.assertIn(f"still silent: {n} of {n}", self.out["raised"][0])
        # most of them back: cleared
        self.assertEqual(self.texts[20], [])

    def test_a_ping_without_an_address_is_long_gone(self):
        ping = [h for h in self.out["unlocated"]
                if h.get("seen_by") == "ping" and not h.get("ip")]
        self.assertEqual([h["mac"] for h in ping], ["00:ee:00:00:0e:01"])
        self.assertIs(ping[0]["recent"], False)


if __name__ == "__main__":
    unittest.main()
