"""The demo of v0.8.11: a router of `routers:` on a LACP pair.

Three scans of the demo in a process of its own (the service reads its
configuration once, at import). The router is on access-sw-1 Gi0/20
under ether2's MAC in the first scan and on the pair Gi0/15+Gi0/16 from
the second: its bond's MAC is two bits from its second member's, none
of its MACs has an IP, and LLDP names it by ether2's MAC.

With what the router says about itself, the map has one router node on
the pair with its name and address, "Router interfaces" in its card, no
suspect MACs on the pair, and the note written on ether2's MAC on it.
Without it — the same demo with the router answering no table — the
bond is held back as a damaged copy: the demo is the bug, and its
answer.

Run with:  python -m unittest discover -s tests
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from moonlan import demo

SCRIPT = r"""
import asyncio, json, logging, sys
from moonlan import demo, poller, server

logging.getLogger("moonlan").setLevel(logging.ERROR)
if sys.argv[1] == "without":
    demo.router_identities = lambda: []


async def run():
    demo.seed_notes(server.rt.db)
    out = {"scans": []}
    for _ in range(3):
        await poller.run_scan()
        topo = json.loads((await server.api_topology()).body)
        ports = await server.api_switch_ports("10.0.0.21")
        ports = json.loads(ports.body) if hasattr(ports, "body") else ports
        out["scans"].append({
            "hosts": [h for h in topo["hosts"] if h["mac"] in demo.LAG_ROUTER_MACS],
            "lag": [p for p in ports["ports"] if p["name"] == "LAG Gi0/15+Gi0/16"],
        })
    out["journal"] = [
        e for e in server.rt.db.journal(2000)
        if e["event"] in ("note_moved", "new_mac")
    ]
    out["note"] = server.rt.db.note("host:" + demo.LAG_ROUTER_BOND)
    out["rows"] = {
        mac: row for mac, row in server.rt.db.hosts_by_mac().items()
        if mac in demo.LAG_ROUTER_MACS
    }
    print(json.dumps(out))

asyncio.run(run())
"""

LAG = "LAG Gi0/15+Gi0/16"


def run_demo(mode: str) -> dict:
    root = Path(__file__).resolve().parent.parent
    with tempfile.TemporaryDirectory() as tmp:
        config = Path(tmp) / "config.yaml"
        config.write_text(f"db_path: {Path(tmp) / 'moonlan.db'}\n", encoding="utf-8")
        env = {**os.environ, "MOONLAN_DEMO": "1", "MOONLAN_CONFIG": str(config),
               "PYTHONPATH": os.pathsep.join([str(root), str(root / "tests")])}
        done = subprocess.run([sys.executable, "-c", SCRIPT, mode], cwd=tmp,
                              env=env, capture_output=True, text=True,
                              timeout=300, check=True)
    return json.loads(done.stdout.strip().splitlines()[-1])


def by_mac(scan: dict) -> dict[str, dict]:
    return {h["mac"]: h for h in scan["hosts"]}


class WithTheIdentityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_demo("with")

    def test_one_router_node_on_the_pair(self):
        for scan in self.out["scans"][1:]:
            hosts = by_mac(scan)
            nodes = [m for m, h in hosts.items() if not h.get("merged_into")]
            self.assertEqual(nodes, [demo.LAG_ROUTER_BOND])
            node = hosts[demo.LAG_ROUTER_BOND]
            self.assertEqual((node["switch"], node["port"]), ("10.0.0.21", LAG))
            self.assertEqual(node["router_ip"], demo.LAG_ROUTER_IP)
            self.assertEqual(node["lldp"]["sys_name"], demo.LAG_ROUTER_NAME)
            self.assertEqual(node["lldp"]["kind"], "router")
            for mac in (demo.LAG_ROUTER_MEMBER, demo.LAG_ROUTER_OLD):
                self.assertEqual(hosts[mac]["merged_into"],
                                 "host:" + demo.LAG_ROUTER_BOND)

    def test_the_card_lists_its_interfaces(self):
        node = by_mac(self.out["scans"][-1])[demo.LAG_ROUTER_BOND]
        self.assertEqual(
            [(i["name"], i["mac"], i["on_map"]) for i in node["router"]["interfaces"]],
            [("bond1", demo.LAG_ROUTER_BOND, True),
             ("ether5", demo.LAG_ROUTER_MEMBER, False),
             ("ether2", demo.LAG_ROUTER_OLD, False)],
        )
        self.assertEqual([i["name"] for i in node["router"]["unmet"]], ["ether1"])

    def test_no_suspect_macs_on_the_pair(self):
        for scan in self.out["scans"]:
            [lag] = scan["lag"]
            self.assertEqual(lag.get("suspect_macs") or [], [])

    def test_in_the_first_scan_it_is_where_it_was(self):
        hosts = by_mac(self.out["scans"][0])
        self.assertNotIn(demo.LAG_ROUTER_BOND, hosts)
        node = hosts[demo.LAG_ROUTER_OLD]
        self.assertNotIn("merged_into", node)
        self.assertEqual(node["port"], "Gi0/20")
        self.assertEqual(hosts[demo.LAG_ROUTER_MEMBER]["merged_into"],
                         "host:" + demo.LAG_ROUTER_OLD)

    def test_the_note_followed_the_router_once(self):
        moved = [e for e in self.out["journal"] if e["event"] == "note_moved"]
        self.assertEqual(len(moved), 1)
        self.assertEqual(moved[0]["user"], "@moonlan")
        self.assertEqual(json.loads(moved[0]["details"]), {
            "from": "host:" + demo.LAG_ROUTER_OLD,
            "to": "host:" + demo.LAG_ROUTER_BOND,
        })
        self.assertEqual(self.out["note"]["label"], demo.LAG_ROUTER_NOTE[1])

    def test_no_new_mac_for_the_router(self):
        self.assertEqual(
            [e for e in self.out["journal"]
             if e["event"] == "new_mac" and e["mac"] in demo.LAG_ROUTER_MACS],
            [],
        )
        for mac in (demo.LAG_ROUTER_BOND, demo.LAG_ROUTER_MEMBER):
            self.assertTrue(self.out["rows"][mac]["confirmed"])
            self.assertEqual(self.out["rows"][mac]["ip"], "")


class WithoutTheIdentityTest(unittest.TestCase):
    """The router answering no table: the defect itself, in the demo."""

    @classmethod
    def setUpClass(cls):
        cls.out = run_demo("without")

    def test_the_bond_is_held_back_as_a_copy_of_the_member(self):
        scan = self.out["scans"][-1]
        [lag] = scan["lag"]
        self.assertEqual(
            [(s["mac"], s["sample"]) for s in lag["suspect_macs"]],
            [(demo.LAG_ROUTER_BOND, demo.LAG_ROUTER_MEMBER)],
        )
        hosts = by_mac(scan)
        self.assertNotIn(demo.LAG_ROUTER_BOND, hosts)
        self.assertNotIn("merged_into", hosts[demo.LAG_ROUTER_MEMBER])
        self.assertNotIn("lldp", hosts[demo.LAG_ROUTER_MEMBER])
        self.assertTrue(hosts[demo.LAG_ROUTER_OLD]["stale"])
        self.assertFalse(self.out["rows"][demo.LAG_ROUTER_BOND]["confirmed"])


if __name__ == "__main__":
    unittest.main()
