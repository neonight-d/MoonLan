"""A synthetic port only for an aggregate, named by its members (v0.8.6).

Two things were seen on the production network:

- An access switch whose dot1dBasePortIfIndex has no gaps at all once
  answered a MAC-table row with bridge-port 402792706. The collector made it a synthetic port at once, before
  anyone asked whether it was an aggregate, and "-402792706" stayed in
  the database as the port of three devices — "Offline · 3".
- An LACP link between two core switches was right, and its ends were
  called "bridge-port 1" and "bridge-port 25": numbers D-Link changes
  when the members flap, and that nobody cabled.

Now an unmapped bridge-port is a port only when it is an aggregate,
and that port is "LAG Gi0/1+Gi0/2". The names already in the
database are renamed or reset after a full poll.

Run with:  python -m unittest discover -s tests
"""

import asyncio
import contextlib
import io
import json
import re
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from service_fixture import SWITCHES  # noqa: E402

from moonlan import diag, poller, server, snmp_collector  # noqa: E402
from moonlan.routes import topology as routes_topology  # noqa: E402
from moonlan.mapping import switches  # noqa: E402
from moonlan.snmp_collector import (  # noqa: E402
    OID_BRIDGE_ADDRESS,
    OID_FDB_PORT,
    OID_IF_DESCR,
    OID_IF_HIGH_SPEED,
    OID_IF_NAME,
    OID_IF_OPER_STATUS,
    OID_IF_TYPE,
    OID_PORT_IFINDEX,
    OID_SYS_NAME,
    SnmpCollector,
    SwitchData,
    PortInfo,
    WalkStatus,
)
from moonlan.topology import FdbStability  # noqa: E402

CORE_A, CORE_B, ACCESS = SWITCHES[0], SWITCHES[1], SWITCHES[2]
PHANTOM_BRIDGE_PORT = 402792706
PHANTOM_MAC = "00:1b:2c:3d:4e:5f"
ACCESS_HOST = "00:1b:2c:00:00:03"
# devices core-a sees through its aggregate only: a group behind the trunk
BEHIND_TRUNK = [f"00:1b:2c:aa:00:{n:02x}" for n in range(1, 5)]
CORE_A_LAG = "LAG Gi0/1+Gi0/2"
CORE_B_LAG = "LAG 1/25+1/26"


def _mac_bytes(mac: str) -> bytes:
    return bytes(int(part, 16) for part in mac.split(":"))


def _specs(phantom: bool) -> dict[str, dict]:
    """Three switches as their agents answer: two core switches joined
    by a D-Link aggregate on each side (bridge-ports missing from the
    mapping), and an access switch with a mapping that has no gaps."""
    core_a_mac, core_b_mac, access_mac = (
        "02:00:00:00:00:10", "02:00:00:00:00:21", "02:00:00:00:00:35"
    )
    access_fdb = {core_a_mac: 25, ACCESS_HOST: 3}
    if phantom:
        access_fdb[PHANTOM_MAC] = PHANTOM_BRIDGE_PORT
    return {
        CORE_A: {
            "name": "core-a", "bridge": core_a_mac,
            "ports": {i: f"Gi0/{i}" for i in range(1, 27)},
            "mapping": {i: i for i in range(3, 27)},
            "fdb": {core_b_mac: 1, access_mac: 5, **{m: 1 for m in BEHIND_TRUNK}},
        },
        CORE_B: {
            "name": "core-b", "bridge": core_b_mac,
            "ports": {i: f"1/{i}" for i in range(1, 29)},
            "mapping": {i: i for i in range(1, 29) if i not in (25, 26)},
            "fdb": {core_a_mac: 25},
        },
        ACCESS: {
            "name": "es3528m", "bridge": access_mac,
            "ports": {i: f"Port{i}" for i in range(1, 29)},
            "mapping": {i: i for i in range(1, 29)},
            "fdb": access_fdb,
        },
    }


class StubAgent(SnmpCollector):
    """The real collect() over agents that answer from `_specs`."""

    def __init__(self, phantom: bool = False):
        self._engine = None
        self._community = None
        self._walk_status = {}
        self._retries_on_break = 0
        self._per_host = {}
        self._dead_oids = {}
        self._cycle = 0
        self._dead_oid_strikes = 0
        self._dead_oid_cooldown = 30
        self.answered_at = {}
        self._communities = {}
        self.phantom = phantom

    def begin_scan_cycle(self) -> None:
        pass

    async def collect(self, host: str) -> SwitchData:
        if host not in _specs(self.phantom):
            # one of the other configured switches, beside the point
            sw = SwitchData(ip=host, reachable=True, sys_name=f"sw-{host}",
                            polled_at=time.time())
            sw.ports[1] = PortInfo(if_index=1, name="Gi0/1")
            return sw
        return await super().collect(host)

    async def _target(self, host):
        return None

    async def _get(self, host, oid):
        spec = _specs(self.phantom).get(host)
        if spec is None:
            return None
        if oid == OID_SYS_NAME:
            return spec["name"]
        if oid == OID_BRIDGE_ADDRESS:
            return _mac_bytes(spec["bridge"])
        return None

    async def _walk(self, host, oid):
        spec = _specs(self.phantom).get(host, {})
        ports = spec.get("ports", {})
        rows: dict[tuple, object] = {}
        if oid in (OID_IF_DESCR, OID_IF_NAME):
            rows = {(i,): name for i, name in ports.items()}
        elif oid == OID_IF_TYPE:
            rows = {(i,): 6 for i in ports}
        elif oid == OID_IF_OPER_STATUS:
            rows = {(i,): 1 for i in ports}
        elif oid == OID_IF_HIGH_SPEED:
            rows = {(i,): 1000 for i in ports}
        elif oid == OID_PORT_IFINDEX:
            rows = {(bp,): i for bp, i in spec.get("mapping", {}).items()}
        elif oid == OID_FDB_PORT:
            rows = {
                tuple(_mac_bytes(mac)): bp
                for mac, bp in spec.get("fdb", {}).items()
            }
        self._walk_status[(host, oid)] = WalkStatus(oid=oid, rows=len(rows))
        for suffix in sorted(rows):
            yield suffix, rows[suffix]

    def last_walk_status(self, host, oid):
        return self._walk_status.get((host, oid)) or WalkStatus(oid=oid)


def _negative_numbers(value, path="") -> list[str]:
    """Every string anywhere in `value` that is a negative number."""
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            found += _negative_numbers(key, f"{path}.{key}[key]")
            found += _negative_numbers(item, f"{path}.{key}")
    elif isinstance(value, list):
        for i, item in enumerate(value):
            found += _negative_numbers(item, f"{path}[{i}]")
    elif isinstance(value, str):
        if re.fullmatch(r"-\d+", value) or re.search(r":-\d+$", value):
            found.append(f"{path} = {value!r}")
    return found


class CollectorTest(unittest.TestCase):
    def setUp(self):
        snmp_collector._warned_bridge_ports.clear()

    def test_an_unknown_bridge_port_is_no_port(self):
        agent = StubAgent(phantom=True)
        with self.assertLogs("moonlan.snmp_collector", "INFO") as logged:
            data = asyncio.run(agent.collect(ACCESS))
        self.assertNotIn(-PHANTOM_BRIDGE_PORT, data.ports)
        self.assertNotIn(PHANTOM_MAC, data.fdb)
        self.assertEqual(data.lag_groups, {})
        # the real device of this switch is where it was
        self.assertEqual(data.fdb[ACCESS_HOST], 3)
        warnings = [r for r in logged.records if r.levelname == "WARNING"]
        self.assertEqual(len(warnings), 1)
        self.assertIn(str(PHANTOM_BRIDGE_PORT), warnings[0].getMessage())
        self.assertIn(PHANTOM_MAC, warnings[0].getMessage())
        self.assertTrue(any(
            "unknown bridge-port: 1" in r.getMessage() for r in logged.records
        ))

    def test_the_warning_is_once_per_process(self):
        agent = StubAgent(phantom=True)
        with self.assertLogs("moonlan.snmp_collector", "WARNING"):
            asyncio.run(agent.collect(ACCESS))
        with self.assertNoLogs("moonlan.snmp_collector", "WARNING"):
            asyncio.run(agent.collect(ACCESS))

    def test_a_dlink_aggregate_is_named_by_its_members(self):
        agent = StubAgent()
        core_a = asyncio.run(agent.collect(CORE_A))
        core_b = asyncio.run(agent.collect(CORE_B))
        self.assertEqual(core_a.lag_groups, {1: [1, 2]})
        self.assertEqual(core_a.ports[-1].name, CORE_A_LAG)
        self.assertEqual(core_b.ports[-25].name, CORE_B_LAG)
        self.assertEqual(core_a.fdb[_specs(False)[CORE_B]["bridge"]], -1)


class NamelessPortTest(unittest.TestCase):
    def test_port_name_never_gives_a_negative_number(self):
        from moonlan import topology
        topology._nameless_ports.clear()
        sw = SwitchData(ip=ACCESS)
        sw.ports[3] = PortInfo(if_index=3, name="Port3")
        with self.assertLogs("moonlan.topology", "WARNING") as logged:
            self.assertEqual(topology.port_name(sw, -PHANTOM_BRIDGE_PORT), "")
        self.assertEqual(len(logged.records), 1)
        with self.assertNoLogs("moonlan.topology", "WARNING"):
            self.assertEqual(topology.port_name(sw, -PHANTOM_BRIDGE_PORT), "")
        self.assertEqual(topology.port_name(sw, 3), "Port3")
        self.assertEqual(topology.port_name(sw, 7), "7")

    def test_smoothing_forgets_a_port_the_poll_does_not_have(self):
        stability = FdbStability(ttl=3)
        stability.merge(ACCESS, {PHANTOM_MAC: -PHANTOM_BRIDGE_PORT, ACCESS_HOST: 3})
        merged = stability.merge(ACCESS, {}, ports={3, 25})
        self.assertEqual(merged, {ACCESS_HOST: 3})


class ServiceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        snmp_collector._warned_bridge_ports.clear()
        self.agent = StubAgent()
        self._get_collector = poller.get_collector
        poller.get_collector = lambda: self.agent
        self._stability = server.rt.fdb_stability
        server.rt.fdb_stability = FdbStability()
        server.rt.switch_data.clear()
        server.rt.config.switch_snmp = {}
        for table in ("hosts", "journal", "layout", "notes"):
            server.rt.db._conn.execute(f"DELETE FROM {table}")
        server.rt.db._conn.commit()

    def tearDown(self):
        poller.get_collector = self._get_collector
        server.rt.fdb_stability = self._stability
        server.rt.state.scanning = False

    async def test_the_link_and_the_group_behind_it_carry_the_names(self):
        await poller.run_scan()
        drawn = server.rt.state.as_dict()
        link = next(
            link for link in drawn["links"]
            if {link["a"], link["b"]} == {CORE_A, CORE_B}
        )
        ends = {link["a"]: link["a_port"], link["b"]: link["b_port"]}
        self.assertEqual(ends, {CORE_A: CORE_A_LAG, CORE_B: CORE_B_LAG})
        self.assertTrue(link["lag"]["trunk"])
        groups = {(g["switch"], g["port"]) for g in drawn["trunk_groups"]}
        self.assertIn((CORE_A, CORE_A_LAG), groups)

    async def test_no_negative_number_reaches_the_topology(self):
        """A remembered entry on a port this poll does not have, a
        database record from before, and the phantom row itself."""
        self.agent.phantom = True
        core_a_mac = _specs(False)[CORE_A]["bridge"]
        server.rt.fdb_stability._cache[ACCESS] = {
            core_a_mac: [-PHANTOM_BRIDGE_PORT, 3],
            PHANTOM_MAC: [-PHANTOM_BRIDGE_PORT, 3],
        }
        now = time.time()
        server.rt.db.seed_host(
            "00:1b:2c:00:00:77", ACCESS, str(-PHANTOM_BRIDGE_PORT), 1,
            first_seen=now - 86400, last_seen=now - 600, seen_count=5,
        )
        with self.assertLogs("moonlan", "WARNING"):
            await poller.run_scan()
        answer = json.loads((await routes_topology.api_topology()).body)
        self.assertEqual(_negative_numbers(answer), [])
        on_access = [h for h in answer["hosts"] if h["switch"] == ACCESS]
        self.assertNotIn(PHANTOM_MAC, {h["mac"] for h in on_access})
        # the device with the old port is off the map, as one without
        # a place
        unlocated = {h["mac"]: h for h in answer["unlocated"]}
        self.assertIn("00:1b:2c:00:00:77", unlocated)

    async def test_old_names_in_the_database(self):
        now = time.time()
        server.rt.db.seed_host(
            "00:1b:2c:00:00:71", ACCESS, str(-PHANTOM_BRIDGE_PORT), 1,
            first_seen=now - 86400, last_seen=now - 600, seen_count=5,
        )
        server.rt.db.seed_host(
            "00:1b:2c:00:00:72", CORE_A, "bridge-port 1", 1,
            first_seen=now - 86400, last_seen=now - 600, seen_count=5,
        )
        server.rt.db.seed_host(
            "00:1b:2c:00:00:73", CORE_B, "bridge-port 7", 1,
            first_seen=now - 86400, last_seen=now - 600, seen_count=5,
        )
        server.rt.db.save_layout({
            f"offline:{CORE_A}:bridge-port 1": {"x": 10, "y": 20, "pinned": True},
            f"pseudo:{ACCESS}:-{PHANTOM_BRIDGE_PORT}": {"x": 1, "y": 2},
        })
        server.rt.db.set_note(
            f"trunk:{CORE_A}:bridge-port 1", "Basement", "", "manual", "", now
        )
        server.rt.db.set_note(
            f"pseudo:{CORE_B}:bridge-port 7", "Old", "", "manual", "", now
        )
        with self.assertLogs("moonlan", "INFO") as logged:
            await poller.run_scan()
        rows = server.rt.db.hosts_by_mac()
        self.assertEqual(rows["00:1b:2c:00:00:71"]["port"], "")
        self.assertEqual(rows["00:1b:2c:00:00:72"]["port"], CORE_A_LAG)
        self.assertEqual(rows["00:1b:2c:00:00:73"]["port"], "")
        layout = server.rt.db.layout()
        self.assertIn(f"offline:{CORE_A}:{CORE_A_LAG}", layout)
        self.assertTrue(layout[f"offline:{CORE_A}:{CORE_A_LAG}"]["pinned"])
        self.assertNotIn(f"offline:{CORE_A}:bridge-port 1", layout)
        # no aggregate to rename it to: left to age out
        self.assertIn(f"pseudo:{ACCESS}:-{PHANTOM_BRIDGE_PORT}", layout)
        notes = server.rt.db.notes()
        self.assertIn(f"trunk:{CORE_A}:{CORE_A_LAG}", notes)
        self.assertNotIn(f"trunk:{CORE_A}:bridge-port 1", notes)
        # …and a note whose port is no aggregate is left as it was: it
        # goes to "Notes without a node"
        self.assertIn(f"pseudo:{CORE_B}:bridge-port 7", notes)
        lines = [r.getMessage() for r in logged.records
                 if "before v0.8.6" in r.getMessage()]
        self.assertEqual(len(lines), 3, lines)   # one per switch
        # the next poll has nothing left to do, and says nothing about it
        with self.assertLogs("moonlan", "INFO") as logged:
            await poller.run_scan()
        self.assertFalse(
            [r for r in logged.records if "before v0.8.6" in r.getMessage()]
        )

    async def test_a_saved_reading_renames_nothing(self):
        """Only a full poll says which aggregates exist."""
        now = time.time()
        server.rt.db.seed_host(
            "00:1b:2c:00:00:74", CORE_B, "bridge-port 25", 1,
            first_seen=now - 86400, last_seen=now - 600, seen_count=5,
        )
        stale = SwitchData(ip=CORE_B, reachable=True, over_budget=True)
        await switches._retire_port_names([stale])
        self.assertEqual(
            server.rt.db.hosts_by_mac()["00:1b:2c:00:00:74"]["port"],
            "bridge-port 25",
        )


class AggregateRowTest(unittest.IsolatedAsyncioTestCase):
    """The aggregate's row in the ports panel is its members' (v0.8.6)."""

    def setUp(self):
        from collections import deque
        from moonlan.counters import PortRates
        self.deque, self.PortRates = deque, PortRates
        server.rt.switch_data.clear()
        sw = SwitchData(ip=CORE_B, reachable=True, sys_name="core-b")
        for i in range(1, 29):
            sw.ports[i] = PortInfo(if_index=i, name=f"1/{i}",
                                   oper_up=i in (25, 26), speed_mbps=1000)
        sw.lag_groups = {25: [25, 26]}
        sw.ports[-25] = PortInfo(if_index=-25, name=CORE_B_LAG,
                                 is_physical=False)
        server.rt.switch_data[CORE_B] = sw
        self.sw = sw
        for key in [k for k in server.rt.counter_store._rates if k[0] == CORE_B]:
            del server.rt.counter_store._rates[key]

    def tearDown(self):
        server.rt.switch_data.clear()
        for key in [k for k in server.rt.counter_store._rates if k[0] == CORE_B]:
            del server.rt.counter_store._rates[key]

    def _rate(self, if_index, in_mbps, out_mbps, ago=10.0):
        server.rt.counter_store._rates[(CORE_B, if_index)] = self.deque([
            self.PortRates(
                ts=time.time() - ago, in_mbps=in_mbps, out_mbps=out_mbps,
                in_errors_per_min=1.0, out_errors_per_min=0.0,
                in_discards_per_min=2.0, out_discards_per_min=0.5,
            )
        ])

    async def _rows(self):
        answer = await server.api_switch_ports(CORE_B)
        return {p["name"]: p for p in answer["ports"]}

    async def test_the_aggregate_is_the_sum_of_its_members(self):
        self._rate(25, 3.0, 1.0, ago=10)
        self._rate(26, 2.0, 0.5, ago=40)
        rows = await self._rows()
        lag = rows[CORE_B_LAG]
        self.assertTrue(lag["aggregate"])
        self.assertEqual(lag["members"], ["1/25", "1/26"])
        self.assertTrue(lag["oper_up"])
        self.assertEqual(lag["speed_mbps"], 2000)
        self.assertEqual(lag["in_mbps"], 5.0)
        self.assertEqual(lag["out_mbps"], 1.5)
        self.assertEqual(lag["errors_per_min"], 2.0)
        self.assertEqual(lag["discards_per_min"], 5.0)
        # as old as its oldest member's reading
        self.assertAlmostEqual(lag["rate_age_seconds"], 40, delta=2)
        # the members name the aggregate they are in
        self.assertEqual(rows["1/25"]["lag"], CORE_B_LAG)
        self.assertEqual(rows["1/26"]["lag"], CORE_B_LAG)
        self.assertFalse(rows["1/25"]["aggregate"])
        # there is no "bridge-port 25" row next to them
        self.assertFalse([n for n in rows if n.startswith("bridge-port")])

    async def test_one_member_not_measured_makes_the_sum_unknown(self):
        self._rate(25, 3.0, 1.0)
        rows = await self._rows()
        lag = rows[CORE_B_LAG]
        self.assertIsNone(lag["in_mbps"])
        self.assertIsNone(lag["out_mbps"])
        self.assertIsNone(lag["rate_age_seconds"])
        self.assertEqual(lag["speed_mbps"], 2000)

    async def test_a_degraded_aggregate_counts_the_members_that_are_up(self):
        self.sw.ports[26].oper_up = False
        rows = await self._rows()
        self.assertTrue(rows[CORE_B_LAG]["oper_up"])
        self.assertEqual(rows[CORE_B_LAG]["speed_mbps"], 1000)
        self.sw.ports[25].oper_up = False
        rows = await self._rows()
        self.assertFalse(rows[CORE_B_LAG]["oper_up"])
        self.assertEqual(rows[CORE_B_LAG]["speed_mbps"], 0)

    async def test_a_lag_mib_aggregate_is_left_alone(self):
        self.sw.ports[1000] = PortInfo(if_index=1000, name="Po1",
                                       is_physical=False, oper_up=True,
                                       speed_mbps=2000)
        self.sw.lag_members = {1: 1000, 2: 1000}
        rows = await self._rows()
        self.assertFalse(rows["Po1"]["aggregate"])
        self.assertEqual(rows["Po1"]["members"], [])
        self.assertEqual(rows["Po1"]["speed_mbps"], 2000)

    async def test_it_sorts_beside_its_first_member(self):
        answer = await server.api_switch_ports(CORE_B)
        names = [p["name"] for p in answer["ports"]]
        self.assertEqual(
            names[names.index("1/25"):names.index("1/25") + 3],
            ["1/25", CORE_B_LAG, "1/26"],
        )


class AggregateRowPageTest(unittest.TestCase):
    def test_the_page_shows_it_as_an_aggregate(self):
        from web_source import function, strings
        source = function("renderPorts")
        self.assertIn("p.is_physical || p.aggregate", source)
        self.assertIn('t("portAggregate")', source)
        self.assertIn('"portAggregateMembers"', source)
        # no loop column for it
        self.assertIn("if (p.aggregate) tr.append(", source)
        for lang in ("en", "ru"):
            table = strings(lang)
            for key in ("portAggregate", "portAggregateMembers",
                        "portAggregateHint"):
                self.assertTrue(table.get(key), (lang, key))


class DiagFdbTest(unittest.TestCase):
    def test_diag_rejects_the_row_the_service_drops(self):
        agent = StubAgent(phantom=True)
        original = diag._make_collector
        diag._make_collector = lambda community, timeout: agent
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                asyncio.run(diag.run_fdb_dump(
                    ACCESS, None, "public", 1, server.rt.config
                ))
        finally:
            diag._make_collector = original
        text = out.getvalue()
        self.assertIn(
            f"REJECTED: bridge-port {PHANTOM_BRIDGE_PORT} is not in "
            f"dot1dBasePortIfIndex, and it is not an aggregate", text,
        )
        self.assertIn("unknown bridge-port: 1", text)
        self.assertIn("bad MAC: 0", text)

    def test_diag_keeps_an_aggregate(self):
        agent = StubAgent()
        original = diag._make_collector
        diag._make_collector = lambda community, timeout: agent
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                asyncio.run(diag.run_fdb_dump(
                    CORE_A, None, "public", 1, server.rt.config
                ))
        finally:
            diag._make_collector = original
        text = out.getvalue()
        self.assertIn("unknown bridge-port: 0", text)
        self.assertIn(f"({CORE_A_LAG})", text)


if __name__ == "__main__":
    unittest.main()
