"""A switch stays on the map without a fresh poll (v0.8.7).

Before this version the last full reading of a switch lived only in the
process. After a restart a switch that never finished a poll inside its
budget was simply gone — its node, its branch, and its name, which went
to "Notes without a node" — and `/api/polling` called it
`reachable: false`, the default of an empty reading rather than a fact.
A switch that stopped answering went the same way: `build_topology`
drew only the ones that answered.

Now every switch of `switches:` is on the map in one of five states —
fresh, over_budget, unreachable, restored, never — and the last full
reading of each is in the database:

- the reading survives the trip through JSON and draws the same map;
- after a restart the map comes back from the database at once, marked
  restored, and nothing of it is recorded as a sighting;
- a switch that stops answering stays, drawn from its reading, while
  switch_down goes by the answer;
- a switch with no reading anywhere is an empty node, standing on the
  port its management MAC is seen on;
- a reading in another format, or older than host_retention_days, is
  ignored, and the reading of a switch no longer configured is deleted.

Run with:  python -m unittest discover -s tests
"""

import asyncio
import copy
import dataclasses
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from service_fixture import SWITCHES  # noqa: E402,F401

from moonlan import demo, poller, server  # noqa: E402
from moonlan.mapping import upkeep  # noqa: E402
from moonlan.lldp import LldpNeighbor  # noqa: E402
from moonlan.snmp_collector import (  # noqa: E402
    READING_FORMAT,
    PortInfo,
    SwitchData,
)
from moonlan.stp import StpData, StpPort  # noqa: E402
from moonlan.topology import build_topology  # noqa: E402
from web_source import NODE, run_page  # noqa: E402

CORE, EDGE, QUIET = SWITCHES[0], SWITCHES[1], SWITCHES[2]
DEVICE = "aa:bb:cc:5a:f0:07"
# the management address of QUIET as a router's ARP table gives it
QUIET_MAC = "00:1e:58:00:00:33"


def round_trip(sw: SwitchData) -> SwitchData:
    return SwitchData.from_dict(json.loads(json.dumps(sw.to_dict())))


def topology_of(network: list[SwitchData]) -> str:
    """The parts of the map a reading feeds, as one comparable string"""
    switches, links, hosts, pseudo, vlans, bridges, _info = build_topology(
        network
    )
    return json.dumps(
        [switches, links, hosts, pseudo, vlans, bridges],
        sort_keys=True, default=sorted,
    )


class RoundTripTest(unittest.TestCase):
    def test_every_field_is_saved_or_left_out_on_purpose(self):
        """A field added to SwitchData without a thought about the
        reading fails here, not at somebody's restart."""
        left_out = {
            "reachable",        # a reading is of a switch that answered
            "loop_detection",   # the counters cycle refreshes it
            "over_budget", "over_budget_scans",  # this scan's facts
            "state", "answered", "down_since",   # …and so are these
        }
        saved = set(SwitchData(ip="10.0.0.1").to_dict()) - {"format"}
        fields = {f.name for f in dataclasses.fields(SwitchData)}
        self.assertEqual(fields - left_out, saved)
        for cls, raw in ((PortInfo, PortInfo(if_index=1)),
                         (StpPort, StpPort(bridge_port=1)),
                         (StpData, StpData()),
                         (LldpNeighbor, LldpNeighbor(None, 1))):
            self.assertEqual(
                {f.name for f in dataclasses.fields(cls)}, set(raw.to_dict()),
                cls.__name__,
            )

    def test_the_demo_network_draws_the_same_map_from_its_readings(self):
        """The demo has everything a real reading has: a D-Link
        aggregate inferred from missing bridge-ports, LACP, LLDP with
        and without capabilities, spanning tree with blocking ports,
        VLANs and port labels."""
        original = demo.demo_network()
        saved = [json.dumps(sw.to_dict()) for sw in original]
        restored = [SwitchData.from_dict(json.loads(raw)) for raw in saved]
        # what the map is built from really is in there
        core = next(sw for sw in restored if sw.ip == "10.0.0.10")
        self.assertTrue(core.lag_groups)
        self.assertTrue(core.lldp_neighbors)
        self.assertIsNotNone(core.stp)
        self.assertTrue(core.stp.ports)
        self.assertTrue(core.vlan_names or any(sw.vlan_names for sw in restored))
        self.assertEqual(topology_of(restored), topology_of(original))

    def test_the_types_come_back(self):
        sw = demo.demo_network()[0]
        back = round_trip(sw)
        self.assertEqual(back.ports.keys(), sw.ports.keys())
        self.assertTrue(all(isinstance(k, int) for k in back.port_pvid))
        self.assertTrue(all(isinstance(k, int) for k in back.lag_groups))
        self.assertIsInstance(back.own_macs, set)
        self.assertIsInstance(back.lldp_forwarded, set)
        if back.lldp_neighbors:
            self.assertIsInstance(back.lldp_neighbors[0].cap_enabled, set)
        self.assertEqual(back.to_dict(), sw.to_dict())

    def test_another_format_is_refused(self):
        raw = SwitchData(ip="10.0.0.1").to_dict()
        raw["format"] = READING_FORMAT + 1
        with self.assertRaises(ValueError):
            SwitchData.from_dict(raw)


def switch(ip: str, name: str, octet: int) -> SwitchData:
    sw = SwitchData(
        ip=ip, reachable=True, sys_name=name,
        bridge_mac=f"02:00:00:00:00:{octet:02x}", polled_at=time.time(),
    )
    sw.own_macs = {sw.bridge_mac}
    for i in range(1, 27):
        sw.ports[i] = PortInfo(
            if_index=i, name=f"Gi0/{i}", oper_up=True, speed_mbps=1000
        )
    return sw


class StubCollector:
    """core-sw with access-sw behind Gi0/1 and a device behind that;
    QUIET hangs past every budget, the rest answer at once."""

    def __init__(self):
        self.silent: set[str] = set()
        self.hang: set[str] = {QUIET}
        self.answered_at: dict[str, float] = {}

    def begin_scan_cycle(self) -> None:
        pass

    async def collect_arp(self, host: str) -> dict[str, str]:
        return {QUIET_MAC: QUIET}

    async def collect_router_identity(self, host: str):
        # the router answers no interface table (v0.8.11): ARP alone
        from moonlan.routers import RouterIdentity
        return RouterIdentity(address=host)

    async def collect(self, host: str) -> SwitchData:
        if host in self.silent:
            return SwitchData(ip=host)
        self.answered_at[host] = time.time()
        if host in self.hang:
            await asyncio.sleep(30)
        core = switch(CORE, "core-sw", 1)
        edge = switch(EDGE, "access-sw", 2)
        core.fdb[edge.bridge_mac] = 1
        edge.fdb[core.bridge_mac] = 25
        edge.fdb[DEVICE] = 7
        # QUIET's management MAC on the core's Gi0/9, as a device's
        core.fdb[QUIET_MAC] = 9
        if host == CORE:
            return core
        if host == EDGE:
            return edge
        return switch(host, f"sw-{host}", int(host.rsplit(".", 1)[1]) + 10)


class ScanStatesTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.collector = StubCollector()
        self._get_collector = poller.get_collector
        poller.get_collector = lambda: self.collector
        self._routers = server.rt.config.routers
        server.rt.config.routers = ["10.0.0.254"]
        server.rt.switch_data.clear()
        server.rt.alarm_engine._snmp_fails.clear()
        for table in ("hosts", "journal", "alarms", "notes", "switch_readings"):
            server.rt.db._conn.execute(f"DELETE FROM {table}")
        server.rt.db._conn.commit()
        server.rt.alarm_engine._active.clear()
        self.raised: list[tuple[str, str]] = []
        self._raise = server.rt.alarm_engine._raise
        self._clear = server.rt.alarm_engine._clear

        async def record_raise(alarm_type, subject, message, **kwargs):
            if alarm_type == "switch_down":
                self.raised.append(("raise", subject))

        async def record_clear(alarm_type, subject, message, note=""):
            if alarm_type == "switch_down":
                self.raised.append(("clear", subject))

        server.rt.alarm_engine._raise = record_raise
        server.rt.alarm_engine._clear = record_clear

    def tearDown(self):
        poller.get_collector = self._get_collector
        server.rt.config.routers = self._routers
        server.rt.alarm_engine._raise = self._raise
        server.rt.alarm_engine._clear = self._clear
        server.rt.state.scanning = False

    async def scan(self):
        with self.assertLogs("moonlan", "INFO"):
            await poller.run_scan()
        return {sw["ip"]: sw for sw in server.rt.state.as_dict()["switches"]}

    def row(self, mac):
        return server.rt.db.hosts_by_mac().get(mac)

    async def test_a_full_poll_is_saved(self):
        await self.scan()
        rows = server.rt.db.switch_readings()
        self.assertIn(CORE, rows)
        self.assertEqual(rows[CORE]["format"], READING_FORMAT)
        saved = json.loads(rows[CORE]["data"])
        self.assertEqual(saved["sys_name"], "core-sw")
        # this scan's facts are not part of a reading
        for key in ("over_budget", "over_budget_scans", "state",
                    "answered", "loop_detection"):
            self.assertNotIn(key, saved)
        # nothing to save of the one that never finished
        self.assertNotIn(QUIET, rows)

    async def test_a_silent_switch_stays_drawn_and_switch_down_goes_by_the_answer(self):
        await self.scan()
        await self.scan()            # DEVICE confirmed
        seen = self.row(DEVICE)
        polled_at = server.rt.switch_data[EDGE].polled_at

        self.collector.silent = {EDGE}
        drawn = await self.scan()
        self.assertEqual(drawn[EDGE]["state"], "unreachable")
        self.assertIs(drawn[EDGE]["answered"], False)
        self.assertEqual(drawn[EDGE]["polled_at"], polled_at)
        self.assertGreater(drawn[EDGE]["down_since"], 0)
        # its branch is drawn from the reading…
        topo = server.rt.state.as_dict()
        self.assertIn(
            frozenset({CORE, EDGE}),
            {frozenset({link["a"], link["b"]}) for link in topo["links"]},
        )
        host = next(h for h in topo["hosts"] if h["mac"] == DEVICE)
        self.assertTrue(host["from_saved"])
        # …and nothing behind it is a sighting
        self.assertEqual(self.row(DEVICE)["last_seen"], seen["last_seen"])
        self.assertEqual(self.row(DEVICE)["seen_count"], seen["seen_count"])
        # switch_down after SWITCH_DOWN_AFTER silent scans, drawn or not
        self.assertNotIn(("raise", EDGE), self.raised)
        drawn = await self.scan()
        self.assertEqual(drawn[EDGE]["state"], "unreachable")
        self.assertIn(("raise", EDGE), self.raised)
        self.assertEqual(self.row(DEVICE)["last_seen"], seen["last_seen"])

        self.collector.silent = set()
        drawn = await self.scan()
        self.assertEqual(drawn[EDGE]["state"], "fresh")
        self.assertIs(drawn[EDGE]["answered"], True)
        self.assertEqual(drawn[EDGE]["down_since"], 0)
        self.assertIn(("clear", EDGE), self.raised)
        self.assertGreater(self.row(DEVICE)["last_seen"], seen["last_seen"])

    async def test_never_read_in_full_is_an_empty_node_on_its_port(self):
        server.rt.db.set_note(
            "sw:" + QUIET, "Annex", "", "manual", "", time.time() - 86400
        )
        for _ in range(4):
            drawn = await self.scan()
        quiet = drawn[QUIET]
        self.assertEqual(quiet["state"], "never")
        self.assertEqual(quiet["ports_total"], 0)
        self.assertTrue(quiet["over_budget"])
        self.assertEqual(quiet["over_budget_scans"], 4)
        # the first request came back: it answers, only too slowly
        self.assertIs(quiet["answered"], True)
        # on the core's port its management MAC is seen on…
        self.assertEqual(
            quiet["attached"],
            {"switch": CORE, "port": "Gi0/9", "mac": QUIET_MAC},
        )
        topo = server.rt.state.as_dict()
        # …and that MAC is the switch, not a dot beside it
        self.assertNotIn(QUIET_MAC, {h["mac"] for h in topo["hosts"]})
        self.assertNotIn(QUIET_MAC, {h["mac"] for h in topo["unlocated"]})
        self.assertIsNone(self.row(QUIET_MAC))
        # no link is invented for it
        self.assertFalse([
            link for link in topo["links"] if QUIET in (link["a"], link["b"])
        ])
        # its name is on the map, not under "Notes without a node"
        self.assertIn("sw:" + QUIET, upkeep._noted_node_ids())
        self.assertFalse(server.rt.db.note("sw:" + QUIET).get("missing_since"))
        # /api/polling: it answered — not "reachable: false"
        polling = {
            row["ip"]: row for row in (await server.api_polling())["switches"]
        }
        self.assertIs(polling[QUIET]["answered"], True)
        self.assertEqual(polling[QUIET]["state"], "never")
        self.assertEqual(polling[CORE]["state"], "fresh")

    async def test_over_budget_before_the_first_answer_is_not_known(self):
        self.collector.hang = set()

        async def late(host):
            await asyncio.sleep(30)

        original = self.collector.collect

        async def collect(host):
            if host == QUIET:
                await late(host)     # not even sysName came back
            return await original(host)

        self.collector.collect = collect
        drawn = await self.scan()
        self.assertIsNone(drawn[QUIET]["answered"])
        polling = {
            row["ip"]: row for row in (await server.api_polling())["switches"]
        }
        self.assertIsNone(polling[QUIET]["answered"])
        self.assertFalse(polling[QUIET]["reachable"])


class StartupTest(unittest.IsolatedAsyncioTestCase):
    """Which saved readings come back at the start, and which do not"""

    def setUp(self):
        server.rt.switch_data.clear()
        server.rt.db._conn.execute("DELETE FROM switch_readings")
        server.rt.db._conn.commit()
        self._days = server.rt.config.host_retention_days
        server.rt.config.host_retention_days = 30

    def tearDown(self):
        server.rt.config.host_retention_days = self._days
        server.rt.switch_data.clear()

    def save(self, ip: str, age_days: float = 0, fmt: int = READING_FORMAT):
        sw = switch(ip, f"sw-{ip}", 50)
        sw.polled_at = time.time() - age_days * 86400
        server.rt.db.save_switch_reading(
            ip, sw.polled_at, fmt, "test", json.dumps(sw.to_dict())
        )

    async def test_what_comes_back_and_what_does_not(self):
        self.save(CORE)
        self.save(EDGE, fmt=READING_FORMAT + 7)
        self.save(QUIET, age_days=31)
        self.save("10.9.9.9")        # no longer in switches:
        with self.assertLogs("moonlan", "INFO") as logged:
            await poller.restore_readings()
        text = "\n".join(logged.output)
        self.assertEqual(server.rt.switch_data[CORE].map_state, "restored")
        self.assertIsNone(server.rt.switch_data[CORE].answered)
        # nothing came back for these two: empty, not restored
        self.assertEqual(server.rt.switch_data[EDGE].map_state, "never")
        self.assertEqual(server.rt.switch_data[QUIET].map_state, "never")
        self.assertIn(f"saved reading of {EDGE} is in an older format — ignored", text)
        self.assertIn("older than host_retention_days", text)
        # deleted, in one line
        self.assertNotIn("10.9.9.9", server.rt.db.switch_readings())
        self.assertEqual(
            len([line for line in logged.output if "10.9.9.9" in line]), 1
        )
        drawn = {sw["ip"]: sw for sw in server.rt.state.as_dict()["switches"]}
        self.assertEqual(drawn[CORE]["state"], "restored")
        # the rest have nothing: empty nodes, not missing ones
        self.assertEqual(drawn[EDGE]["state"], "never")
        self.assertEqual(drawn[EDGE]["over_budget_scans"], 0)

    async def test_retention_zero_keeps_any_age(self):
        server.rt.config.host_retention_days = 0
        self.save(CORE, age_days=400)
        with self.assertLogs("moonlan", "INFO"):
            await poller.restore_readings()
        self.assertEqual(server.rt.switch_data[CORE].map_state, "restored")


PAGE = r"""
const now = Date.now() / 1000;
const sw = (ip, state, extra) => ({
  ip, name: "sw-" + ip, named: true, model: "", mac: "", descr: "",
  stp_operating: false, stp_root: false, over_budget: state === "over_budget",
  polled_at: now - 3600, over_budget_scans: 0, ports_total: 26, ports_up: 3,
  port_prefix: "Gi0/", state, answered: state === "unreachable" ? false : true,
  down_since: state === "unreachable" ? now - 600 : 0, ...extra,
});
topology = {
  switches: [
    sw("10.0.0.1", "fresh"),
    sw("10.0.0.2", "unreachable"),
    sw("10.0.0.3", "restored", { answered: null }),
    sw("10.0.0.4", "never", {
      name: "10.0.0.4", named: false, ports_total: 0, ports_up: 0,
      polled_at: 0, over_budget: true, over_budget_scans: 41,
      started_at: now - 86400, budget_seconds: 120,
      attached: { switch: "10.0.0.1", port: "Gi0/9", mac: "00:1e:58:00:00:33" },
    }),
    sw("10.0.0.5", "over_budget", { over_budget_scans: 3, answered: null }),
  ],
  links: [
    { a: "10.0.0.1", a_port: "Gi0/1", b: "10.0.0.2", b_port: "Gi0/25", source: "fdb", speed_mbps: 1000 },
    { a: "10.0.0.1", a_port: "Gi0/2", b: "10.0.0.3", b_port: "Gi0/25", source: "both", speed_mbps: 1000 },
    { a: "10.0.0.1", a_port: "Gi0/3", b: "10.0.0.5", b_port: "Gi0/25", source: "fdb", speed_mbps: 1000 },
  ],
  hosts: [
    { mac: "aa:bb:cc:00:00:01", switch: "10.0.0.2", port: "Gi0/7", vlan: 1, name: "" },
    { mac: "aa:bb:cc:00:00:02", switch: "10.0.0.1", port: "Gi0/8", vlan: 1, name: "" },
  ],
  pseudo_switches: [], bridges: [], external_networks: [], offline_groups: [],
  trunk_groups: [], unlocated: [], vlan_names: {},
};
const { nodes, edges } = buildGraphData();
const node = (id) => nodes.find((n) => n.id === id);
const edge = (from, to) => edges.find((e) => e.from === from && e.to === to);
const out = {};
out.stub = {
  dashed: !!(node("sw:10.0.0.4").shapeProperties || {}).borderDashes,
  semantic: node("sw:10.0.0.4").semantic,
  label: node("sw:10.0.0.4").label,
};
const stubEdge = edges.find((e) => e.id === "stubedge:10.0.0.4");
out.stubEdge = stubEdge && { from: stubEdge.from, to: stubEdge.to, dashed: !!stubEdge.dashes };
out.links = Object.fromEntries(
  ["10.0.0.2", "10.0.0.3", "10.0.0.5"].map((ip) => {
    const e = edge("sw:10.0.0.1", "sw:" + ip);
    return [ip, { opacity: e.color.opacity, reading: e.semantic.includes("reading") }];
  })
);
out.hostEdges = {
  muted: edges.find((e) => e.id === "hostedge:aa:bb:cc:00:00:01").color.opacity,
  fresh: edges.find((e) => e.id === "hostedge:aa:bb:cc:00:00:02").color.opacity,
};
out.labels = Object.fromEntries(
  ["10.0.0.2", "10.0.0.3"].map((ip) => [ip, node("sw:" + ip).label])
);
out.hints = {};
for (const language of ["en", "ru"]) {
  lang = language;
  out.hints[language] = Object.fromEntries(
    topology.switches.map((s) => [s.ip, String(switchStateHint(s))])
  );
}
__done(out);
"""


@unittest.skipUnless(NODE, "needs node to run the page's JavaScript")
class PageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_page(PAGE)

    def test_the_empty_node_is_dashed_and_stands_on_its_port(self):
        self.assertTrue(self.out["stub"]["dashed"])
        self.assertIn("stub", self.out["stub"]["semantic"])
        self.assertEqual(self.out["stubEdge"], {
            "from": "sw:10.0.0.1", "to": "sw:10.0.0.4", "dashed": True,
        })

    def test_the_branch_of_a_reading_is_muted(self):
        links = self.out["links"]
        for ip in ("10.0.0.2", "10.0.0.3"):
            self.assertTrue(links[ip]["reading"], ip)
            self.assertLessEqual(links[ip]["opacity"], 0.35, ip)
        # over budget is drawn as before (v0.6.12)
        self.assertFalse(links["10.0.0.5"]["reading"])
        self.assertLess(self.out["hostEdges"]["muted"], self.out["hostEdges"]["fresh"])
        self.assertIn("not answering since", self.out["labels"]["10.0.0.2"])
        self.assertIn("data from before the restart", self.out["labels"]["10.0.0.3"])

    def test_the_card_says_where_the_data_comes_from(self):
        ru = self.out["hints"]["ru"]
        self.assertEqual(ru["10.0.0.1"], "")
        self.assertIn("Не отвечает с", ru["10.0.0.2"])
        self.assertIn(", данные от", ru["10.0.0.2"])
        self.assertIn("Данные до перезапуска, от", ru["10.0.0.3"])
        self.assertIn("полный опрос не завершился ни разу: 41 скан не уложился в 120 с", ru["10.0.0.4"])
        self.assertIn("host_budget_seconds", ru["10.0.0.4"])
        self.assertIn("Не уложился в бюджет 3 сканов подряд, данные от", ru["10.0.0.5"])
        # answered unknown: the full text does not claim it answers
        self.assertIn("неизвестно", ru["10.0.0.5"])
        en = self.out["hints"]["en"]
        self.assertIn("41 scans did not fit in 120 s", en["10.0.0.4"])
        self.assertIn("Not answering since", en["10.0.0.2"])


RESTART = r"""
import asyncio, copy, json, sys
from moonlan import demo, poller, server
from moonlan.snmp_collector import SwitchData

NETWORK = {sw.ip: sw for sw in demo.demo_network()}


class Agent:
    answered_at = {}

    def begin_scan_cycle(self):
        pass

    async def collect(self, host):
        self.answered_at[host] = 1e18
        return copy.deepcopy(NETWORK[host])


poller.get_collector = lambda: Agent()


def picture():
    topo = server.rt.state.as_dict()
    return {
        "switches": sorted(
            [sw["ip"], sw["name"], sw["ports_total"], sw["ports_up"], sw["state"]]
            for sw in topo["switches"]
        ),
        "links": sorted(
            [l["a"], l["a_port"], l["b"], l["b_port"]] for l in topo["links"]
        ),
        "hosts": sorted([h["mac"], h["switch"], h["port"]] for h in topo["hosts"]),
        "from_saved": sum(1 for h in topo["hosts"] if h.get("from_saved")),
    }


def inventory():
    return {
        mac: [row["last_seen"], row["seen_count"]]
        for mac, row in server.rt.db.hosts_by_mac().items()
    }


def new_macs():
    return server.rt.db._conn.execute(
        "SELECT COUNT(*) FROM journal WHERE event = 'new_mac'"
    ).fetchone()[0]


async def run(phase):
    out = {}
    if phase == "before":
        await poller.run_scan()
        await poller.run_scan()
        out["map"] = picture()
    else:
        out["inventory_before"] = inventory()
        out["new_macs_before"] = new_macs()
        await poller.restore_readings()
        out["map"] = picture()
        out["inventory_after"] = inventory()
        out["new_macs_after"] = new_macs()
        await poller.run_scan()
        out["after_scan"] = picture()
    print(json.dumps(out))

asyncio.run(run(sys.argv[1]))
"""


class RestartTest(unittest.TestCase):
    """A scan, a new process on the same database, the map back"""

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parent.parent
        cls.out = {}
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.yaml"
            config.write_text(
                f"db_path: {Path(tmp) / 'moonlan.db'}\n"
                "scan_interval_minutes: 0\n"
                # the switches the demo network has data for: not the
                # one it keeps over budget
                "switches:\n" + "".join(
                    f"  - {ip}\n" for ip in demo.SWITCH_IPS
                    if ip != demo.NEVER_SWITCH
                ),
                encoding="utf-8",
            )
            env = {**os.environ, "MOONLAN_CONFIG": str(config),
                   "PYTHONPATH": os.pathsep.join([str(root), str(root / "tests")])}
            env.pop("MOONLAN_DEMO", None)
            for phase in ("before", "after"):
                done = subprocess.run(
                    [sys.executable, "-c", RESTART, phase], cwd=tmp, env=env,
                    capture_output=True, text=True, timeout=300, check=True,
                )
                cls.out[phase] = json.loads(done.stdout.strip().splitlines()[-1])

    def test_the_map_after_the_restart_is_the_map_before_it(self):
        before = self.out["before"]["map"]
        after = self.out["after"]["map"]
        self.assertEqual(after["links"], before["links"])
        self.assertEqual(after["hosts"], before["hosts"])
        self.assertEqual(
            [sw[:4] for sw in after["switches"]],
            [sw[:4] for sw in before["switches"]],
        )
        self.assertEqual({sw[4] for sw in before["switches"]}, {"fresh"})
        self.assertEqual({sw[4] for sw in after["switches"]}, {"restored"})
        # every device on it is drawn from a reading
        self.assertEqual(after["from_saved"], len(after["hosts"]))

    def test_nothing_restored_is_a_sighting(self):
        after = self.out["after"]
        self.assertEqual(after["inventory_after"], after["inventory_before"])
        self.assertEqual(after["new_macs_after"], after["new_macs_before"])

    def test_the_first_scan_makes_it_fresh(self):
        after = self.out["after"]["after_scan"]
        self.assertEqual({sw[4] for sw in after["switches"]}, {"fresh"})
        self.assertEqual(after["from_saved"], 0)
        self.assertEqual(after["links"], self.out["before"]["map"]["links"])


if __name__ == "__main__":
    unittest.main()
