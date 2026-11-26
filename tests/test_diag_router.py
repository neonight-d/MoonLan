"""diag for a router on an aggregate (v0.8.11).

`diag --host <MAC>` says in one line what settles the case: the MAC is
an interface of a router in `routers:`, or it is held back as a
possible damaged copy of another address. `diag --topology` gives an
aggregate the state the ports panel gives it — up while one member is
— instead of the synthetic port's own oper_up, which is never set:
[LAG Gi0/1+Gi0/2 DOWN] with both members working.

Run with:  python -m unittest discover -s tests
"""

import asyncio
import contextlib
import io
import logging
import time
import unittest

from moonlan import demo, diag
from moonlan.config import Config
from moonlan.routers import RouterIdentity, identity_from_tables
from moonlan.snmp_collector import PortInfo, SwitchData
from moonlan.topology import port_up

ACCESS = "10.1.0.21"
ROUTER = "10.1.0.1"
BOND = "02:ee:00:00:10:05"
MEMBER = "02:ee:00:00:10:06"   # two bits from BOND
OLD = "02:ee:00:00:20:02"


def identity() -> RouterIdentity:
    return identity_from_tables(
        ROUTER, {2: OLD, 4: BOND, 5: MEMBER, 10: BOND},
        {2: "ether2", 4: "ether4", 5: "ether5", 10: "bond1"},
        {2: 6, 4: 6, 5: 6, 10: 161}, {ROUTER: 10},
    )


def record(mac: str, **fields) -> dict:
    row = {
        "mac": mac, "ip": "", "switch_ip": ACCESS, "port": "Gi0/15",
        "confirmed": 0, "corrupt_of": "", "last_seen": time.time() - 60,
        "seen_count": 40,
    }
    row.update(fields)
    return row


class PortUpTest(unittest.TestCase):
    def switch(self) -> SwitchData:
        sw = SwitchData(ip=ACCESS, reachable=True)
        for i in (15, 16, 17):
            sw.ports[i] = PortInfo(if_index=i, name=f"Gi0/{i}", oper_up=False)
        return sw

    def test_a_synthetic_aggregate_is_up_while_a_member_is(self):
        sw = self.switch()
        sw.lag_groups = {15: [15, 16]}
        sw.ports[-15] = PortInfo(if_index=-15, name="LAG Gi0/15+Gi0/16")
        self.assertFalse(port_up(sw, -15))
        sw.ports[16].oper_up = True
        self.assertTrue(port_up(sw, -15))
        self.assertFalse(sw.ports[-15].oper_up)  # its own never says

    def test_an_ieee_aggregate_goes_by_its_members_too(self):
        sw = self.switch()
        sw.lag_members = {15: 1000, 16: 1000}
        sw.ports[1000] = PortInfo(if_index=1000, name="Po1", oper_up=False)
        sw.ports[15].oper_up = True
        self.assertTrue(port_up(sw, 1000))

    def test_a_plain_port_is_its_own_state(self):
        sw = self.switch()
        self.assertFalse(port_up(sw, 17))
        sw.ports[17].oper_up = True
        self.assertTrue(port_up(sw, 17))
        self.assertFalse(port_up(sw, 99))


class StubCollector:
    def __init__(self, identity: RouterIdentity, fdb: dict[str, int]):
        self.identity = identity
        self.fdb = fdb

    async def collect_router_identity(self, ip):
        return self.identity

    async def collect_arp(self, ip):
        return {}


def run(coroutine) -> str:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        asyncio.run(coroutine)
    return out.getvalue()


class TopologyViewTest(unittest.TestCase):
    def test_no_down_on_an_aggregate_whose_members_work(self):
        async def collect_all(collector, cfg):
            return demo.demo_network(advance=False), []

        saved = diag._make_collector, diag._collect_all
        diag._make_collector = lambda community, timeout: StubCollector(
            RouterIdentity(ROUTER), {}
        )
        diag._collect_all = collect_all
        cfg = Config()
        cfg.switches = list(demo.SWITCH_IPS)
        cfg.routers = []
        logging.disable(logging.CRITICAL)
        try:
            text = run(diag.run_topology_view("public", 1, cfg))
        finally:
            diag._make_collector, diag._collect_all = saved
            logging.disable(logging.NOTSET)
        links = [line for line in text.splitlines() if "LAG Gi0/3+Gi0/4 " in line
                 and " — " in line]
        self.assertEqual(len(links), 1, text)
        self.assertIn("[LAG Gi0/3+Gi0/4 up]", links[0])
        self.assertIn("[LAG Gi0/23+Gi0/24 up]", links[0])
        self.assertNotIn("DOWN", links[0])


class LinesTest(unittest.TestCase):
    def test_an_interface_of_a_router(self):
        self.assertEqual(
            diag.router_interface_line([identity()], BOND),
            f"an interface of router {ROUTER} (bond1)",
        )
        self.assertEqual(
            diag.router_interface_line([identity()], MEMBER),
            f"an interface of router {ROUTER} (ether5)",
        )
        self.assertEqual(diag.router_interface_line([identity()], "02:ee:00:00:00:01"), "")
        self.assertEqual(diag.router_interface_line([RouterIdentity(ROUTER)], BOND), "")

    def rows(self) -> dict[str, dict]:
        return {
            MEMBER: record(MEMBER, confirmed=1),
            BOND: record(BOND),
        }

    def test_held_back_as_a_copy(self):
        self.assertEqual(
            diag.copy_line(BOND, self.rows(), [ACCESS], set(), 86400,
                           time.time(), 8),
            f"held back as a possible damaged copy of {MEMBER} (2 bits)",
        )

    def test_a_router_own_mac_is_held_back_by_nothing(self):
        self.assertEqual(
            diag.copy_line(BOND, self.rows(), [ACCESS], set(identity().macs),
                           86400, time.time(), 8),
            "",
        )

    def test_an_ip_takes_it_out_of_the_candidates(self):
        rows = self.rows()
        rows[BOND]["ip"] = "10.1.5.20"
        self.assertEqual(
            diag.copy_line(BOND, rows, [ACCESS], set(), 86400, time.time(), 8),
            "",
        )

    def test_on_another_switch_it_is_no_copy(self):
        self.assertEqual(
            diag.copy_line(BOND, self.rows(), ["10.1.0.22"], set(), 86400,
                           time.time(), 8),
            "",
        )

    def test_a_marked_record_says_so(self):
        rows = self.rows()
        rows[BOND].update(confirmed=1, corrupt_of=MEMBER)
        line = diag.copy_line(BOND, rows, [ACCESS], set(), 86400, time.time(), 8)
        self.assertTrue(
            line.startswith(f"marked as a damaged copy of {MEMBER} (2 bits)"),
            line,
        )

    def test_one_bit_is_one_bit(self):
        rows = {MEMBER: record(MEMBER, confirmed=1),
                "02:ee:00:00:10:07": record("02:ee:00:00:10:07")}
        self.assertTrue(diag.copy_line(
            "02:ee:00:00:10:07", rows, [ACCESS], set(), 86400, time.time(), 8,
        ).endswith("(1 bit)"))


class HostDiagTest(unittest.TestCase):
    """The whole of `diag --host`, its SNMP and the database stubbed."""

    def run_host(self, mac: str, found: RouterIdentity, rows: list[dict]) -> str:
        collector = StubCollector(found, {})

        async def own_macs_light(collector, switch):
            return "access-sw", {"02:ee:00:00:00:02"}

        async def port_maps(collector, switch):
            return {15: -15}, {-15: "LAG Gi0/15+Gi0/16"}, {-15: 10}

        async def fdb_rows(collector, switch):
            return [
                {"mac": BOND, "bridge_port": 15, "reason": "", "oid": "x"},
                {"mac": MEMBER, "bridge_port": 15, "reason": "", "oid": "y"},
            ]

        saved = (diag._make_collector, diag._own_macs_light, diag._port_maps,
                 diag._fdb_rows, diag._db_snapshot)
        diag._make_collector = lambda community, timeout: collector
        diag._own_macs_light = own_macs_light
        diag._port_maps = port_maps
        diag._fdb_rows = fdb_rows
        diag._db_snapshot = lambda path: [dict(r) for r in rows]
        cfg = Config()
        cfg.switches = [ACCESS]
        cfg.routers = [ROUTER]
        try:
            return run(diag.run_host_diag(mac, "public", 1, cfg))
        finally:
            (diag._make_collector, diag._own_macs_light, diag._port_maps,
             diag._fdb_rows, diag._db_snapshot) = saved

    def test_the_bond_of_a_router(self):
        text = self.run_host(
            BOND, identity(),
            [record(MEMBER, confirmed=1), record(BOND)],
        )
        self.assertIn(f"\nan interface of router {ROUTER} (bond1)\n", text)
        self.assertNotIn("held back", text)

    def test_the_same_mac_without_the_identity_is_held_back(self):
        text = self.run_host(
            BOND, RouterIdentity(ROUTER),
            [record(MEMBER, confirmed=1), record(BOND)],
        )
        self.assertNotIn("an interface of router", text)
        self.assertIn(
            f"\nheld back as a possible damaged copy of {MEMBER} (2 bits)\n",
            text,
        )


if __name__ == "__main__":
    unittest.main()
