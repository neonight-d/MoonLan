"""A router of `routers:` says which MACs are its own (v0.8.11).

A router moved from a single port onto a LACP pair. Its bond's MAC — the
source of all its traffic — had no IP, because a router's own addresses
are not in its own ARP table, and it was two bits away from the MAC of
the bond's second member, confirmed a few days before. So it was taken
for a damaged copy and held off the map; the member's MAC was drawn
instead, and named after the router; LLDP reported the router under
the MAC of the port it had left, and its name reached no host; and that
old port's MAC stayed grey on the old port.

The router's own tables answer all of it: ifPhysAddress, ifName,
ifType and ipAdEntIfIndex. The cases below are that network, with
names and addresses of the test network.

Run with:  python -m unittest discover -s tests
"""

import asyncio
import json
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from service_fixture import SWITCHES  # noqa: E402,F401

from moonlan import notes, server  # noqa: E402
from web_source import APP, NODE, run_page  # noqa: E402
from moonlan.lldp import LldpNeighbor  # noqa: E402
from moonlan.routers import (  # noqa: E402
    RouterIdentity, card, identity_from_tables, own_macs, place,
)
from moonlan.snmp_collector import (  # noqa: E402
    OID_IF_DESCR, OID_IF_NAME, OID_IF_PHYS_ADDRESS, OID_IF_TYPE,
    OID_IP_AD_ENT_IF_INDEX, PortInfo, SnmpCollector, SwitchData,
)

CORE, ACCESS = "10.1.0.10", "10.1.0.21"
ROUTER = "10.1.0.1"
BOND = "02:ee:00:00:10:05"     # bond1, ether4 and the VLANs on them
MEMBER = "02:ee:00:00:10:06"   # ether5, two bits from BOND
OLD = "02:ee:00:00:20:02"      # ether2, the port the router left
SPARE = "02:ee:00:00:44:01"    # ether1, never in any MAC table
LAG = "LAG Gi0/15+Gi0/16"


def identity() -> RouterIdentity:
    """The router, out of its tables."""
    return identity_from_tables(
        ROUTER,
        phys={1: SPARE, 2: OLD, 4: BOND, 5: MEMBER,
              10: BOND, 20: BOND, 30: BOND},
        names={1: "ether1", 2: "ether2", 4: "ether4", 5: "ether5",
               10: "bond1", 20: "vlan-mgt", 30: "vlan-users"},
        types={1: 6, 2: 6, 4: 6, 5: 6, 10: 161, 20: 135, 30: 135},
        ip_index={ROUTER: 20, "10.1.1.1": 20, "10.1.2.1": 30},
    )


class IdentityTest(unittest.TestCase):
    def test_a_mac_is_named_after_the_interface_that_owns_it(self):
        found = identity()
        self.assertEqual(found.macs, {SPARE, OLD, BOND, MEMBER})
        # the aggregate before its member, a port before a VLAN
        self.assertEqual(found.name(BOND), "bond1")
        self.assertEqual(
            found.interfaces[BOND], ["bond1", "ether4", "vlan-mgt", "vlan-users"]
        )
        self.assertEqual(found.kinds[BOND], "bond")
        self.assertEqual(found.name(MEMBER), "ether5")
        self.assertEqual(found.name(OLD), "ether2")

    def test_the_address_in_routers_is_on_one_of_them(self):
        found = identity()
        self.assertEqual(found.primary, BOND)
        self.assertEqual(found.ips["10.1.2.1"], BOND)
        self.assertEqual(found.addresses(), {ROUTER, "10.1.1.1", "10.1.2.1"})

    def test_no_types_no_addresses_still_an_identity(self):
        found = identity_from_tables(
            ROUTER, {2: OLD, 5: MEMBER}, {2: "ether2", 5: "ether5"}
        )
        self.assertTrue(found)
        self.assertEqual(found.primary, "")
        self.assertEqual(found.name(MEMBER), "ether5")

    def test_an_interface_without_a_name_is_called_by_its_index(self):
        found = identity_from_tables(ROUTER, {7: MEMBER}, {})
        self.assertEqual(found.name(MEMBER), "7")

    def test_no_macs_is_an_empty_identity(self):
        found = identity_from_tables(ROUTER, {}, {1: "ether1"})
        self.assertFalse(found)
        self.assertFalse(found.is_it(MEMBER, [ROUTER]))

    def test_lldp_is_the_router_by_its_chassis_or_its_addresses(self):
        found = identity()
        self.assertTrue(found.is_it(OLD, []))
        self.assertTrue(found.is_it("02:ee:00:00:99:01", ["10.1.1.1"]))
        self.assertFalse(found.is_it("02:ee:00:00:99:01", ["10.1.9.9"]))

    def test_a_mac_two_routers_claim_stays_with_the_first(self):
        first = identity_from_tables("10.1.0.1", {1: BOND}, {1: "vrrp"})
        second = identity_from_tables("10.1.0.2", {1: BOND}, {1: "vrrp"})
        self.assertEqual(own_macs([second, first])[BOND].address, "10.1.0.1")


class StubWalks(SnmpCollector):
    """A collector whose tables are dictionaries; records what it asked."""

    def __init__(self, tables: dict[str, dict]):
        self.tables = tables
        self.asked: list[str] = []

    async def _walk(self, host, oid):
        self.asked.append(oid)
        for index, value in sorted(self.tables.get(oid, {}).items()):
            yield (index if isinstance(index, tuple) else (index,)), value


def mac_bytes(mac: str) -> bytes:
    return bytes(int(part, 16) for part in mac.split(":"))


class CollectorTest(unittest.TestCase):
    def collect(self, tables) -> tuple[RouterIdentity, list[str]]:
        collector = StubWalks(tables)
        found = asyncio.run(collector.collect_router_identity(ROUTER))
        return found, collector.asked

    def test_the_identity_out_of_the_tables(self):
        found, _asked = self.collect({
            OID_IF_PHYS_ADDRESS: {
                2: mac_bytes(OLD), 4: mac_bytes(BOND), 5: mac_bytes(MEMBER),
                10: mac_bytes(BOND), 20: mac_bytes(BOND),
                99: b"", 98: b"\x00" * 6,          # loopback, a tunnel
            },
            OID_IF_NAME: {2: "ether2", 4: "ether4", 5: "", 10: "bond1",
                          20: "vlan-mgt"},
            OID_IF_DESCR: {5: "ether5", 10: "some descr"},
            OID_IF_TYPE: {2: 6, 4: 6, 5: 6, 10: 161, 20: 135},
            OID_IP_AD_ENT_IF_INDEX: {(10, 1, 0, 1): 20, (10, 1, 1, 1): 20},
        })
        self.assertEqual(found.macs, {OLD, BOND, MEMBER})
        self.assertEqual(found.name(BOND), "bond1")
        # ifDescr only where ifName is empty
        self.assertEqual(found.name(MEMBER), "ether5")
        self.assertEqual(found.primary, BOND)
        self.assertEqual(found.ips, {ROUTER: BOND, "10.1.1.1": BOND})

    def test_ifdescr_is_not_asked_for_when_every_name_is_there(self):
        _found, asked = self.collect({
            OID_IF_PHYS_ADDRESS: {5: mac_bytes(MEMBER)},
            OID_IF_NAME: {5: "ether5"},
        })
        self.assertNotIn(OID_IF_DESCR, asked)

    def test_a_router_that_does_not_answer_has_an_empty_identity(self):
        found, asked = self.collect({OID_IF_NAME: {1: "ether1"}})
        self.assertFalse(found)
        self.assertEqual(found.address, ROUTER)
        # one walk, not four
        self.assertEqual(asked, [OID_IF_PHYS_ADDRESS])


class ReportTest(unittest.TestCase):
    def setUp(self):
        server.rt._identity_reported.clear()

    def lines(self, found: RouterIdentity) -> list[str]:
        with self.assertLogs("moonlan", "INFO") as logs:
            server.log.info("marker")
            server._report_identity(found)
        return [line for line in logs.output if "marker" not in line]

    def test_an_empty_identity_is_said_once(self):
        empty = RouterIdentity(address=ROUTER)
        first = self.lines(empty)
        self.assertEqual(len(first), 1)
        self.assertIn("only through ARP", first[0])
        self.assertEqual(self.lines(empty), [])
        self.assertEqual(self.lines(empty), [])

    def test_an_identity_is_said_when_it_arrives_and_when_it_changes(self):
        empty = RouterIdentity(address=ROUTER)
        self.lines(empty)
        arrived = self.lines(identity())
        self.assertEqual(len(arrived), 1)
        self.assertIn("4 MAC(s) of its own", arrived[0])
        self.assertIn(f"{ROUTER} is on bond1", arrived[0])
        self.assertEqual(self.lines(identity()), [])
        self.assertEqual(len(self.lines(empty)), 1)


def host(mac: str, switch: str = ACCESS, port: str = LAG, **extra) -> dict:
    return {"mac": mac, "switch": switch, "port": port, **extra}


class PlaceTest(unittest.TestCase):
    def test_one_node_for_two_macs_on_one_aggregate(self):
        placed = place(
            [identity()], [host(MEMBER), host(BOND)], [], [],
            {OLD: {"switch": ACCESS, "port": "Gi0/21", "last_seen": 1.0}},
        )
        [node] = placed.nodes
        self.assertEqual(node.node_id, "host:" + BOND)
        self.assertEqual((node.switch, node.port), (ACCESS, LAG))
        self.assertEqual(node.seen, {BOND, MEMBER})
        # the member on the same aggregate, and the port it left
        self.assertEqual(placed.merged, {
            MEMBER: "host:" + BOND, OLD: "host:" + BOND,
            SPARE: "host:" + BOND,
        })

    def test_without_its_address_the_node_is_another_mac_seen_now(self):
        placed = place(
            [identity()], [host(MEMBER)], [], [],
            {BOND: {"switch": ACCESS, "port": LAG, "last_seen": 1.0}},
        )
        self.assertEqual(placed.nodes[0].node_id, "host:" + MEMBER)
        self.assertEqual(placed.merged[BOND], "host:" + MEMBER)

    def test_then_the_port_its_lldp_is_heard_on(self):
        # neither is the interface of its address: the cable is where
        # LLDP hears the router, whatever the MACs sort as
        heard = {"switch": ACCESS, "port": "Gi0/20", "chassis_id": OLD,
                 "mgmt_ips": []}
        hosts = [host(MEMBER), host(OLD, port="Gi0/20")]
        self.assertLess(MEMBER, OLD)
        placed = place([identity()], hosts, [], [heard])
        self.assertEqual(placed.nodes[0].node_id, "host:" + OLD)
        placed = place([identity()], hosts, [], [])
        self.assertEqual(placed.nodes[0].node_id, "host:" + MEMBER)
        # the interface of its address still comes first
        placed = place([identity()], hosts + [host(BOND)], [], [heard])
        self.assertEqual(placed.nodes[0].node_id, "host:" + BOND)

    def test_a_real_port_before_a_guess_on_a_trunk(self):
        placed = place(
            [identity()],
            [host(BOND, port="Gi0/3", approximate=True), host(MEMBER)],
            [], [],
        )
        self.assertEqual(placed.nodes[0].node_id, "host:" + MEMBER)

    def test_only_in_the_database_the_latest_record_is_the_node(self):
        placed = place([identity()], [], [], [], {
            OLD: {"switch": ACCESS, "port": "Gi0/21", "last_seen": 100.0},
            MEMBER: {"switch": ACCESS, "port": LAG, "last_seen": 200.0},
        })
        self.assertEqual(placed.nodes[0].node_id, "host:" + MEMBER)

    def test_nowhere_on_the_map_no_node_and_nothing_merged(self):
        placed = place([identity()], [host("00:ee:00:00:00:01")], [], [])
        self.assertEqual(placed.nodes, [])
        self.assertEqual(placed.merged, {})

    def test_an_empty_identity_places_nothing(self):
        placed = place([RouterIdentity(ROUTER)], [host(BOND)], [], [])
        self.assertEqual(placed.nodes, [])

    def test_lldp_on_the_node_port_by_chassis_or_by_address(self):
        by_chassis = {"switch": ACCESS, "port": LAG, "chassis_id": OLD,
                      "mgmt_ips": []}
        elsewhere = {"switch": ACCESS, "port": "Gi0/3", "chassis_id": OLD,
                     "mgmt_ips": []}
        by_address = {"switch": ACCESS, "port": LAG,
                      "chassis_id": "02:ee:00:00:99:01",
                      "mgmt_ips": ["10.1.2.1"]}
        stranger = {"switch": ACCESS, "port": LAG,
                    "chassis_id": "02:ee:00:00:99:02", "mgmt_ips": ["10.1.9.9"]}
        node = place([identity()], [host(BOND)], [],
                     [elsewhere, stranger, by_chassis]).nodes[0]
        self.assertIs(node.lldp, by_chassis)
        node = place([identity()], [host(BOND)], [],
                     [stranger, by_address]).nodes[0]
        self.assertIs(node.lldp, by_address)
        node = place([identity()], [host(BOND)], [], [elsewhere]).nodes[0]
        self.assertIsNone(node.lldp)

    def test_a_bridge_that_is_the_router_is_its_node(self):
        bridge = {"id": "bridge:" + OLD, "chassis_id": OLD, "switch": ACCESS,
                  "port": LAG, "mgmt_ips": ["10.1.1.1"]}
        placed = place([identity()], [host(BOND), host(MEMBER)], [bridge], [])
        [node] = placed.nodes
        self.assertTrue(node.is_bridge)
        self.assertEqual(node.node_id, bridge["id"])
        self.assertEqual(node.mac, BOND)
        self.assertEqual(placed.merged[BOND], bridge["id"])
        self.assertEqual(placed.merged[MEMBER], bridge["id"])


class CardTest(unittest.TestCase):
    def test_the_one_on_the_map_then_seen_now_then_known(self):
        shown = card(identity(), BOND, {BOND, MEMBER}, {OLD})
        self.assertEqual(shown["address"], ROUTER)
        self.assertEqual(
            [(i["name"], i["mac"], i["on_map"]) for i in shown["interfaces"]],
            [("bond1", BOND, True), ("ether5", MEMBER, False),
             ("ether2", OLD, False)],
        )
        self.assertEqual(shown["interfaces"][0]["names"],
                         ["bond1", "ether4", "vlan-mgt", "vlan-users"])
        # never met in a MAC table: a line of their own
        self.assertEqual([i["name"] for i in shown["unmet"]], ["ether1"])


# ---------- the scan ----------

def switch(ip: str, name: str, octet: int) -> SwitchData:
    sw = SwitchData(
        ip=ip, reachable=True, sys_name=name,
        bridge_mac=f"02:ee:00:00:00:{octet:02x}", polled_at=time.time(),
    )
    sw.own_macs = {sw.bridge_mac}
    for i in range(1, 27):
        sw.ports[i] = PortInfo(
            if_index=i, name=f"Gi0/{i}", oper_up=True, speed_mbps=1000
        )
    return sw


def router_neighbour(chassis: str = OLD, mgmt_ips=("10.1.1.1",)):
    """The router over LLDP on Gi0/16, a member of the aggregate."""
    return LldpNeighbor(
        local_ifindex=16, local_port_num=16, port_matched_by="loc_id",
        chassis_id=chassis, chassis_subtype=4, port_id="vlan-mgt",
        sys_name="RouterOS-R", sys_desc="RouterOS 6.x",
        cap_enabled={"router"}, cap_known=True,
        mgmt_ip=mgmt_ips[0] if mgmt_ips else "", mgmt_ips=list(mgmt_ips),
    )


class RouterNetwork:
    """core-sw, and access-sw behind its Gi0/1 with the router on the
    aggregate Gi0/15+Gi0/16 (the D-Link kind: FDB on bridge-port 15's
    synthetic port). `access` is MAC -> port of what access-sw sees."""

    def __init__(self):
        self.access: dict[str, int] = {BOND: -15, MEMBER: -15}
        self.lldp = [router_neighbour()]
        self.identity = identity()
        self.arp: dict[str, str] = {}

    def begin_scan_cycle(self) -> None:
        pass

    async def collect(self, ip: str) -> SwitchData:
        core = switch(CORE, "core-sw", 1)
        access = switch(ACCESS, "access-sw", 2)
        access.lag_groups = {15: [15, 16]}
        access.ports[-15] = PortInfo(if_index=-15, name=LAG, oper_up=False,
                                     is_physical=False)
        core.fdb[access.bridge_mac] = 1
        access.fdb[core.bridge_mac] = 25
        for mac, port in self.access.items():
            access.fdb[mac] = port
            core.fdb[mac] = 1
        access.lldp_neighbors = list(self.lldp)
        return core if ip == CORE else access

    async def collect_arp(self, ip: str) -> dict[str, str]:
        return dict(self.arp)

    async def collect_router_identity(self, ip: str) -> RouterIdentity:
        return self.identity


class RouterScanCase(unittest.IsolatedAsyncioTestCase):
    """The service with the router network, scan by scan, on a database
    holding what the real one held: the member confirmed, the bond held
    back, the old port's MAC grey, a note and a pin on the member."""

    def setUp(self):
        self.network = RouterNetwork()
        self._get_collector = server.get_collector
        server.get_collector = lambda: self.network
        c = server.rt.config
        self._saved = (
            c.switches, c.routers, c.new_host_confirm_scans,
            c.filter_suspect_macs, c.unmanaged_threshold,
            c.thresholds.corruption_hamming_bits, c.host_grace_hours,
            server.rt.first_scan_done,
        )
        c.switches = [CORE, ACCESS]
        c.routers = [ROUTER]
        c.new_host_confirm_scans = 2
        c.filter_suspect_macs = True
        c.unmanaged_threshold = 3
        c.thresholds.corruption_hamming_bits = 8
        c.host_grace_hours = 24
        server.rt.first_scan_done = True
        server.rt.switch_data.clear()
        server.rt.prev_pseudo_ports = set()
        server.rt._identity_reported.clear()
        for table in ("hosts", "journal", "notes", "layout", "bridges",
                      "lldp_neighbors"):
            server.rt.db._conn.execute(f"DELETE FROM {table}")
        server.rt.db._conn.execute("DELETE FROM alarms WHERE type = 'new_mac'")
        server.rt.db._conn.commit()
        now = time.time()
        server.rt.db.seed_host(MEMBER, ACCESS, LAG, 1, first_seen=now - 6 * 86400,
                            last_seen=now - 60, seen_count=500)
        server.rt.db.seed_host(OLD, ACCESS, "Gi0/21", 1,
                            first_seen=now - 300 * 86400,
                            last_seen=now - 3600, seen_count=9000)
        server.rt.db._conn.execute(
            "INSERT INTO hosts (mac, switch_ip, port, vlan, first_seen, "
            "last_seen, seen_count, confirmed) VALUES (?, ?, ?, 10, ?, ?, 40, 0)",
            (BOND, ACCESS, LAG, now - 6 * 86400, now - 60),
        )
        server.rt.db._conn.commit()
        server.rt.db.set_note("host:" + MEMBER, "Gateway", "", "manual",
                           "admin", now - 3600)
        server.rt.db.set_node_position("host:" + MEMBER, 120.0, -40.0, True)

    def tearDown(self):
        server.get_collector = self._get_collector
        c = server.rt.config
        (
            c.switches, c.routers, c.new_host_confirm_scans,
            c.filter_suspect_macs, c.unmanaged_threshold,
            c.thresholds.corruption_hamming_bits, c.host_grace_hours,
            server.rt.first_scan_done,
        ) = self._saved
        server.rt.state.scanning = False
        server.rt.prev_pseudo_ports = set()
        server.rt.switch_data.clear()

    def row(self, mac: str):
        return server.rt.db.hosts_by_mac().get(mac)

    def hosts(self) -> dict[str, dict]:
        return {h["mac"]: h for h in server.rt.state.as_dict()["hosts"]}

    def drawn(self) -> set[str]:
        return {m for m, h in self.hosts().items() if not h.get("merged_into")}

    def events(self, event: str, mac: str | None = None) -> list[dict]:
        rows = server.rt.db._conn.execute(
            "SELECT * FROM journal WHERE event = ?", (event,)
        ).fetchall()
        return [dict(r) for r in rows if mac is None or r["mac"] == mac]


class WithoutIdentityTest(RouterScanCase):
    """What the map was: the router answers no interface table."""

    async def test_the_bond_is_held_back_and_the_member_drawn(self):
        self.network.identity = RouterIdentity(address=ROUTER)
        await server.run_scan()
        self.assertFalse(self.row(BOND)["confirmed"])
        self.assertEqual(
            server.rt.suspect_by_port[(ACCESS, LAG)],
            [{"mac": BOND, "sample": MEMBER, "distance": 2}],
        )
        self.assertEqual(self.drawn(), {MEMBER, OLD})
        self.assertTrue(self.hosts()[OLD]["stale"])
        # the LLDP chassis is a MAC that is on no host of this port
        self.assertNotIn("lldp", self.hosts()[MEMBER])
        self.assertNotIn("router", self.hosts()[MEMBER])
        self.assertEqual(self.events("note_moved"), [])


class WithIdentityTest(RouterScanCase):
    async def test_the_bond_is_a_device_and_no_copy(self):
        await server.run_scan()
        row = self.row(BOND)
        self.assertTrue(row["confirmed"])
        self.assertEqual(row["corrupt_of"], "")
        self.assertNotIn((ACCESS, LAG), server.rt.suspect_by_port)
        self.assertNotIn("suspect_corrupt", self.hosts()[BOND])

    async def test_one_node_and_the_rest_drawn_as_it(self):
        await server.run_scan()
        hosts = self.hosts()
        self.assertEqual(self.drawn(), {BOND})
        self.assertEqual(hosts[BOND]["port"], LAG)
        self.assertEqual(hosts[MEMBER]["merged_into"], "host:" + BOND)
        # the port it left: no grey host there
        self.assertEqual(hosts[OLD]["merged_into"], "host:" + BOND)
        self.assertEqual(server.rt.state.as_dict()["offline_groups"], [])
        # and the records stay
        self.assertIsNotNone(self.row(OLD))
        self.assertIsNotNone(self.row(MEMBER))

    async def test_the_node_has_the_address_the_name_and_the_interfaces(self):
        await server.run_scan()
        node = self.hosts()[BOND]
        self.assertEqual(node["router_ip"], ROUTER)
        # LLDP came under the old port's MAC, on the aggregate's member
        self.assertEqual(node["lldp"]["sys_name"], "RouterOS-R")
        self.assertEqual(node["lldp"]["kind"], "router")
        self.assertEqual(node["lldp"]["port_id"], "vlan-mgt")
        self.assertEqual(node["lldp"]["mgmt_ips"], ["10.1.1.1"])
        self.assertEqual(
            [(i["name"], i["on_map"]) for i in node["router"]["interfaces"]],
            [("bond1", True), ("ether5", False), ("ether2", False)],
        )
        self.assertEqual(
            [i["name"] for i in node["router"]["unmet"]], ["ether1"]
        )

    async def test_lldp_by_address_when_the_chassis_is_a_stranger(self):
        self.network.lldp = [
            router_neighbour("02:ee:00:00:99:01", ("10.1.2.1",))
        ]
        await server.run_scan()
        self.assertEqual(self.hosts()[BOND]["lldp"]["sys_name"], "RouterOS-R")

    async def test_the_note_and_the_pin_follow_the_node_once(self):
        await server.run_scan()
        node_id = "host:" + BOND
        note = server.rt.db.note(node_id)
        self.assertEqual(note["label"], "Gateway")
        self.assertIsNone(server.rt.db.note("host:" + MEMBER))
        layout = server.rt.db.layout()
        self.assertEqual(
            (layout[node_id]["x"], layout[node_id]["y"],
             layout[node_id]["pinned"]),
            (120.0, -40.0, True),
        )
        self.assertNotIn("host:" + MEMBER, layout)
        [moved] = self.events("note_moved")
        self.assertEqual(moved["user"], server.MOONLAN_ACTOR)
        self.assertEqual(
            json.loads(moved["details"]),
            {"from": "host:" + MEMBER, "to": node_id},
        )
        [pinned] = self.events("layout_pinned")
        self.assertEqual(pinned["user"], server.MOONLAN_ACTOR)
        self.assertEqual(json.loads(pinned["details"])["ids"], [node_id])
        await server.run_scan()
        await server.run_scan()
        self.assertEqual(len(self.events("note_moved")), 1)
        self.assertEqual(len(self.events("layout_pinned")), 1)

    async def test_a_node_with_a_note_of_its_own_keeps_it(self):
        now = time.time()
        server.rt.db.set_note("host:" + BOND, "Core router", "", "manual",
                           "admin", now)
        server.rt.db.set_node_position("host:" + BOND, 5.0, 6.0, True)
        for _ in range(notes.MISSING_SCANS):
            await server.run_scan()
        self.assertEqual(server.rt.db.note("host:" + BOND)["label"], "Core router")
        self.assertEqual(self.events("note_moved"), [])
        self.assertEqual(self.events("layout_pinned"), [])
        self.assertEqual(server.rt.db.layout()["host:" + BOND]["x"], 5.0)
        # the member's note is a note without a node now
        left = server.rt.db.note("host:" + MEMBER)
        self.assertEqual(left["label"], "Gateway")
        self.assertEqual(notes.state(left), "missing")

    async def test_no_new_mac_for_an_interface_of_the_router(self):
        server.rt.db.delete_hosts([BOND])
        await server.run_scan()
        self.assertTrue(self.row(BOND)["confirmed"])
        self.assertEqual(self.events("new_mac", BOND), [])
        alarms = server.rt.db._conn.execute(
            "SELECT COUNT(*) AS n FROM alarms WHERE type = 'new_mac'"
        ).fetchone()["n"]
        self.assertEqual(alarms, 0)

    async def test_held_back_is_released_with_one_line(self):
        with self.assertLogs("moonlan", "INFO") as logs:
            await server.run_scan()
        released = [l for l in logs.output if "are not damaged copies" in l]
        self.assertEqual(len(released), 1)
        self.assertIn(f"{BOND} (bond1 of {ROUTER}, was held back", released[0])
        with self.assertLogs("moonlan", "INFO") as logs:
            await server.run_scan()
        self.assertEqual(
            [l for l in logs.output if "are not damaged copies" in l], []
        )

    async def test_a_record_marked_as_a_copy_is_unmarked(self):
        server.rt.db._conn.execute(
            "UPDATE hosts SET corrupt_of = ?, confirmed = 1 WHERE mac = ?",
            (MEMBER, BOND),
        )
        server.rt.db._conn.commit()
        with self.assertLogs("moonlan", "INFO") as logs:
            await server.run_scan()
        self.assertEqual(self.row(BOND)["corrupt_of"], "")
        self.assertIn(BOND, self.drawn())
        self.assertTrue(any(
            f"was marked a copy of {MEMBER}" in line for line in logs.output
        ))


class RegressionTest(RouterScanCase):
    """What has nothing to do with the router is as it was."""

    SAMPLE = "20:7b:d5:46:92:8d"
    COPY = "20:7b:d5:46:92:87"       # two bits away

    async def test_neighbouring_macs_with_an_ip_are_two_devices(self):
        await server.run_scan()
        self.network.access[self.SAMPLE] = 7
        await server.run_scan()
        await server.run_scan()
        self.assertTrue(self.row(self.SAMPLE)["confirmed"])
        self.network.access[self.COPY] = 7
        self.network.arp = {self.COPY: "10.1.5.20"}
        await server.run_scan()
        self.assertTrue(self.row(self.COPY)["confirmed"])
        self.assertIn(self.COPY, self.drawn())
        self.assertNotIn((ACCESS, "Gi0/7"), server.rt.suspect_by_port)

    async def test_a_real_copy_on_a_device_port_is_still_found(self):
        await server.run_scan()
        self.network.access[self.SAMPLE] = 7
        await server.run_scan()
        await server.run_scan()
        self.network.access[self.COPY] = 7
        for _ in range(3):
            await server.run_scan()
        self.assertFalse(self.row(self.COPY)["confirmed"])
        self.assertEqual(
            server.rt.suspect_by_port[(ACCESS, "Gi0/7")],
            [{"mac": self.COPY, "sample": self.SAMPLE, "distance": 2}],
        )
        self.assertNotIn(self.COPY, self.drawn())


PAGE = r"""
const router = {
  address: "10.1.0.1",
  interfaces: [
    { mac: "02:ee:00:00:10:05", name: "bond1", kind: "bond", on_map: true,
      names: ["bond1", "ether4", "vlan-mgt"], more: 0 },
    { mac: "02:ee:00:00:10:06", name: "ether5", kind: "ethernet",
      on_map: false, names: ["ether5"], more: 0 },
    { mac: "02:ee:00:00:20:02", name: "ether2", kind: "ethernet",
      on_map: false, names: ["ether2"], more: 0 },
  ],
  unmet: [
    { mac: "02:ee:00:00:44:01", name: "ether1", names: ["ether1"], more: 0 },
  ],
};
const out = {};
for (const code of ["ru", "en"]) {
  lang = code;
  out[code] = {
    card: routerInterfaces(router).text,
    none: htmlValue(routerInterfaces(null)),
    by: fmt("evBy", { user: actorName("@moonlan") }),
    console: actorName("@console"),
    person: actorName("admin"),
  };
}
__done(out);
"""


@unittest.skipUnless(NODE, "needs node to run the page's JavaScript")
class CardPageTest(unittest.TestCase):
    def test_the_interfaces_in_the_card(self):
        out = run_page(PAGE)
        ru = out["ru"]["card"]
        self.assertIn("Интерфейсы маршрутизатора", ru)
        self.assertIn("bond1 · 02:ee:00:00:10:05", ru)
        self.assertIn("(на карте)", ru)
        self.assertEqual(ru.count("(на карте)"), 1)
        self.assertIn("ether5 · 02:ee:00:00:10:06", ru)
        self.assertIn("ether2 · 02:ee:00:00:20:02", ru)
        self.assertLess(ru.index("bond1"), ru.index("ether5"))
        self.assertLess(ru.index("ether5"), ru.index("ether2"))
        # every name of the bond's MAC in its tooltip
        self.assertIn('title="bond1, ether4, vlan-mgt"', ru)
        self.assertIn("ещё 1: не встречался в таблицах MAC коммутаторов", ru)
        self.assertIn("ether1 · 02:ee:00:00:44:01", ru)  # its tooltip
        en = out["en"]["card"]
        self.assertIn("Router interfaces", en)
        self.assertIn("(on the map)", en)
        self.assertIn("1 more, never in a switch MAC table", en)
        self.assertEqual(out["ru"]["none"], "")

    def test_the_journal_says_moonlan_did_it(self):
        out = run_page(PAGE)
        self.assertIn("MoonLan", out["ru"]["by"])
        self.assertNotIn("@moonlan", out["ru"]["by"])
        self.assertIn("MoonLan", out["en"]["by"])
        self.assertEqual(out["ru"]["console"], "консоль сервера")
        self.assertEqual(out["en"]["person"], "admin")


class PageSourceTest(unittest.TestCase):
    def test_both_cards_show_the_interfaces(self):
        self.assertEqual(APP.count("routerInterfaces(host.router)"), 1)
        self.assertEqual(APP.count("routerInterfaces(bridge.router)"), 1)

    def test_the_journal_and_the_note_word_moonlan_as_the_console(self):
        self.assertIn("user: actorName(ev.user)", APP)
        self.assertIn("user: actorName(note.updated_by)", APP)


if __name__ == "__main__":
    unittest.main()
