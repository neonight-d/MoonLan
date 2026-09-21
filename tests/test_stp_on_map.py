"""What the STP panel says and what the map draws must be one answer.

The panel called mb0 the root. The map drew it as an ordinary switch:
white border, no caption. Both read the same field, `stp.operating` and
`stp.is_root`, off the same objects — but at different moments.

A D-Link running RSTP answers every dot1dStp* object with a zero, so
nothing in its own reply says it is the root. v0.6.11 added the test
that works: a neighbour that has demonstrably processed BPDUs names
this switch's address as its root. That test is `judge_network`, it
needs the whole network in hand, and it ran *after* build_topology —
which had already decided the node was not even operating.

Three fields wrong from one order of operations: `stp_operating`,
`stp_root`, and the blocking ports the map skips for a switch it
believes is not running a tree.

Run with:  python -m unittest discover -s tests
"""

import asyncio
import sys
import time
import unittest
from pathlib import Path

# One shared configuration for every test that imports the service —
# see tests/service_fixture.py for why it cannot be per-module.
# `unittest discover -s tests` puts this directory on sys.path;
# `python -m unittest tests.test_scan_budget` does not.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from service_fixture import SWITCHES  # noqa: E402,F401

from moonlan import server  # noqa: E402
from moonlan.snmp_collector import PortInfo, SwitchData  # noqa: E402
from moonlan.stp import StpData, StpPort, judge  # noqa: E402

ROOT_MAC = "02:4d:4c:00:00:01"


def ray_mac(ip: str) -> str:
    """A MAC of its own for every ray: the shared config has eight
    switches, and two of them answering to one address would confuse
    the very lookup this test is about."""
    return f"02:4d:4c:00:01:{int(ip.rsplit('.', 1)[1]):02x}"


def _root_switch(ip: str) -> SwitchData:
    """The root as the real D-Links report one: all zeros.

    Its own answers cannot distinguish it from a switch with STP
    switched off. Only its neighbours can.
    """
    sw = SwitchData(
        ip=ip, reachable=True, sys_name="core", bridge_mac=ROOT_MAC,
        polled_at=time.time(),
    )
    sw.own_macs = {ROOT_MAC}
    sw.ports[1] = PortInfo(if_index=1, name="Gi0/1", oper_up=True)
    sw.ports[2] = PortInfo(if_index=2, name="Gi0/2", oper_up=True)
    sw.stp = judge(StpData(
        supported=True, protocol_spec=3, priority=0,
        time_since_change=0, top_changes=0,
        designated_root="0/00:00:00:00:00:00", root_cost=0, root_port=0,
        version=2, sys_uptime=4_000_000, own_macs=set(sw.own_macs),
        # …down to a port table where every port reads disabled, which
        # is what those switches really answer while their CLI shows a
        # converged tree
        ports={
            1: StpPort(bridge_port=1, if_index=1, state=1, enabled=True,
                       name="Gi0/1", link_up=True),
            2: StpPort(bridge_port=2, if_index=2, state=1, enabled=True,
                       name="Gi0/2", link_up=True),
        },
    ))
    return sw


def _ray_switch(ip: str) -> SwitchData:
    """A switch that accepted that root at a cost above zero.

    Nothing but a bridge processing BPDUs produces that, which is what
    makes it a witness.
    """
    sw = SwitchData(
        ip=ip, reachable=True, sys_name="ray", bridge_mac=ray_mac(ip),
        polled_at=time.time(),
    )
    sw.own_macs = {sw.bridge_mac}
    sw.ports[1] = PortInfo(if_index=1, name="Gi0/1", oper_up=True)
    sw.stp = judge(StpData(
        supported=True, protocol_spec=3, priority=32768,
        time_since_change=118_000, top_changes=7,
        designated_root=f"4096/{ROOT_MAC}", root_cost=20000, root_port=1,
        version=2, sys_uptime=4_000_000, own_macs=set(sw.own_macs),
        ports={
            1: StpPort(bridge_port=1, if_index=1, state=5, enabled=True,
                       name="Gi0/1", link_up=True),
        },
    ))
    return sw


class StubCollector:
    """Two switches: a silent root and the neighbour that vouches."""

    def __init__(self, ips: list[str]):
        self.ips = ips

    def begin_scan_cycle(self) -> None:
        pass

    async def collect(self, host: str) -> SwitchData:
        return (
            _root_switch(host) if host == self.ips[0] else _ray_switch(host)
        )


class RootOnTheMapTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ips = list(server.config.switches)
        self.assertGreaterEqual(len(self.ips), 2)
        self._get_collector = server.get_collector
        server.get_collector = lambda: StubCollector(self.ips)
        server.switch_data.clear()

    def tearDown(self):
        server.get_collector = self._get_collector
        server.state.scanning = False

    async def test_a_root_its_neighbours_vouch_for_reaches_the_map(self):
        await server.run_scan()
        drawn = {sw["ip"]: sw for sw in server.state.as_dict()["switches"]}
        root = drawn[self.ips[0]]
        # what the panel says…
        panel = {
            entry["ip"]: entry
            for entry in server.state.as_dict()["stp"]["switches"]
        }
        self.assertTrue(panel[self.ips[0]]["operating"])
        self.assertTrue(panel[self.ips[0]]["is_root"])
        self.assertTrue(panel[self.ips[0]]["confirmed_root"])
        # …and what the map draws, which used to disagree
        self.assertTrue(root["stp_operating"])
        self.assertTrue(root["stp_root"])

    async def test_the_panel_and_the_map_agree_on_the_verdict(self):
        """Every field judge_network touches has to reach the map.

        `operating` is the one the map reads to decide whether this
        switch's blocking ports mean anything at all, so it was wrong
        in the same breath as the root caption.
        """
        await server.run_scan()
        drawn = {sw["ip"]: sw for sw in server.state.as_dict()["switches"]}
        panel = {
            entry["ip"]: entry
            for entry in server.state.as_dict()["stp"]["switches"]
        }
        for ip in self.ips[:2]:
            self.assertEqual(
                drawn[ip]["stp_operating"], panel[ip]["operating"], ip
            )
            self.assertEqual(drawn[ip]["stp_root"], panel[ip]["is_root"], ip)
        # one tree, not two: the root is a root, not a second island
        self.assertEqual(
            server.state.as_dict()["stp"]["verdict"]["verdict"], "single"
        )


if __name__ == "__main__":
    unittest.main()
