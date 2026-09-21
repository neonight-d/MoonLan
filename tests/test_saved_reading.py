"""A reading nobody took is not an observation.

A switch that runs out of its poll budget keeps the last table that did
arrive, and the branch behind it stays on the map. That decision
(v0.6.12) stands: cables do not move every ten minutes, and the card
says when the reading was taken.

What must not happen is the copy being counted as a sighting. The old
object went into `collected` whole, and from there:

- `upsert_hosts` set `last_seen = now` and `seen_count += 1` for every
  device behind it, so a device unplugged an hour ago still reported
  "last seen: just now";
- a MAC present in that one reading accumulated confirmations from
  repeats of itself and was announced in the journal as a new device,
  without a second observation ever happening;
- `FdbStability` re-stamped every entry as fresh, so a three-poll
  smoothing never expired and became permanent memory.

The same sin as v0.6.4's "no answer is not zero" and v0.6.13's "a dash
is not expired", from the other side: not an absence dressed up as a
value, but a copy dressed up as an observation.

Run with:  python -m unittest discover -s tests
"""

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
from moonlan.topology import FdbStability, build_topology  # noqa: E402

CORE, SLOW = SWITCHES[0], SWITCHES[1]
OTHERS = SWITCHES[2:]
DEVICE = "aa:bb:cc:00:00:07"
NEWCOMER = "aa:bb:cc:00:00:99"


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


def network(over_budget: bool, macs=(DEVICE,)) -> list[SwitchData]:
    core = switch(CORE, "core-sw", 1)
    slow = switch(SLOW, "slow-sw", 2)
    core.fdb[slow.bridge_mac] = 1
    slow.fdb[core.bridge_mac] = 25
    for mac in macs:
        slow.fdb[mac] = 7
    if over_budget:
        slow.over_budget = True
        slow.over_budget_scans = 1
        slow.polled_at = time.time() - 3600
    return [core, slow]


def spare(ip: str) -> SwitchData:
    """One of the other configured switches: present, and beside the
    point — the shared config has eight."""
    sw = switch(ip, f"sw-{ip}", int(ip.rsplit(".", 1)[1]) + 10)
    return sw


class HostMarkingTest(unittest.TestCase):
    def test_a_device_behind_a_stale_switch_is_marked_and_dated(self):
        _sw, _links, hosts, *_rest = build_topology(
            network(over_budget=True), unmanaged_threshold=0
        )
        host = next(h for h in hosts if h["mac"] == DEVICE)
        self.assertTrue(host["from_saved"])
        self.assertAlmostEqual(host["reading_at"], time.time() - 3600, delta=5)
        # …and it is still on the map, at its port. That part of
        # v0.6.12 is not being undone.
        self.assertEqual((host["switch"], host["port"]), (SLOW, "Gi0/7"))

    def test_a_device_behind_a_healthy_switch_carries_no_such_mark(self):
        _sw, _links, hosts, *_rest = build_topology(
            network(over_budget=False), unmanaged_threshold=0
        )
        host = next(h for h in hosts if h["mac"] == DEVICE)
        self.assertNotIn("from_saved", host)

    def test_the_link_behind_it_stays_on_the_map(self):
        _sw, links, *_rest = build_topology(
            network(over_budget=True), unmanaged_threshold=0
        )
        self.assertIn(
            frozenset({CORE, SLOW}),
            {frozenset({link["a"], link["b"]}) for link in links},
        )


class SmoothingTest(unittest.TestCase):
    """A three-poll window fed from a copy of itself never closes."""

    def test_a_saved_reading_does_not_refresh_the_countdown(self):
        stability = FdbStability(ttl=3)
        table = {DEVICE: 7}
        self.assertEqual(stability.merge(SLOW, table), table)
        for _ in range(5):
            merged = stability.merge(SLOW, table, confirm=False)
            # the saved reading still draws the links behind it…
            self.assertEqual(merged, table)
        # …but the smoothing cache itself has expired
        self.assertEqual(stability.merge(SLOW, {}, confirm=False), {})

    def test_a_real_poll_still_refreshes_it(self):
        stability = FdbStability(ttl=3)
        table = {DEVICE: 7}
        for _ in range(5):
            stability.merge(SLOW, table)
        self.assertEqual(stability.merge(SLOW, {}), table)


class StubCollector:
    """One healthy switch and one that can be told to hang."""

    def __init__(self):
        self.slow = False
        self.macs = [DEVICE]

    def begin_scan_cycle(self) -> None:
        pass

    async def collect(self, host: str) -> SwitchData:
        core, slow = network(over_budget=False, macs=tuple(self.macs))
        if host == CORE:
            return core
        if host != SLOW:
            return spare(host)
        if self.slow:
            import asyncio
            await asyncio.sleep(30)
        return slow


class InventoryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.collector = StubCollector()
        self._get_collector = server.get_collector
        server.get_collector = lambda: self.collector
        server.switch_data.clear()
        self._budget = server.config.snmp.host_budget_seconds
        server.config.snmp.host_budget_seconds = 1
        server.config.switch_snmp = {}
        server.db._conn.execute("DELETE FROM hosts")
        server.db._conn.execute("DELETE FROM journal")
        server.db._conn.commit()

    def tearDown(self):
        server.get_collector = self._get_collector
        server.config.snmp.host_budget_seconds = self._budget
        server.state.scanning = False

    def _row(self, mac: str):
        return server.db.hosts_by_mac().get(mac)

    def _journal(self, event: str, mac: str = "") -> int:
        return server.db._conn.execute(
            "SELECT COUNT(*) AS n FROM journal WHERE event = ? AND mac = ?",
            (event, mac),
        ).fetchone()["n"]

    async def test_last_seen_does_not_move_while_nobody_is_looking(self):
        await server.run_scan()          # one good poll
        await server.run_scan()          # …and a second, to confirm it
        row = self._row(DEVICE)
        self.assertIsNotNone(row)
        seen_at, count = row["last_seen"], row["seen_count"]

        self.collector.slow = True
        for _ in range(3):
            with self.assertLogs("moonlan", "WARNING"):
                await server.run_scan()
        row = self._row(DEVICE)
        self.assertEqual(row["last_seen"], seen_at)
        self.assertEqual(row["seen_count"], count)

        # …and one real poll starts it again
        self.collector.slow = False
        await server.run_scan()
        row = self._row(DEVICE)
        self.assertGreater(row["last_seen"], seen_at)
        self.assertEqual(row["seen_count"], count + 1)

    async def test_a_newcomer_is_not_confirmed_by_repeats_of_one_reading(self):
        """It has to be SEEN twice, not read twice out of one table."""
        await server.run_scan()
        self.collector.macs = [DEVICE, NEWCOMER]
        await server.run_scan()          # the newcomer's one sighting
        self.assertEqual(self._journal("new_mac", NEWCOMER), 0)

        self.collector.slow = True
        for _ in range(4):
            with self.assertLogs("moonlan", "WARNING"):
                await server.run_scan()
        row = self._row(NEWCOMER)
        self.assertEqual(row["seen_count"], 1)
        self.assertFalse(row["confirmed"])
        self.assertEqual(self._journal("new_mac", NEWCOMER), 0)

        # one real poll, and it is a device
        self.collector.slow = False
        await server.run_scan()
        row = self._row(NEWCOMER)
        self.assertTrue(row["confirmed"])
        self.assertEqual(self._journal("new_mac", NEWCOMER), 1)

    async def test_the_branch_stays_drawn_throughout(self):
        await server.run_scan()
        self.collector.slow = True
        for _ in range(3):
            with self.assertLogs("moonlan", "WARNING"):
                await server.run_scan()
        drawn = server.state.as_dict()
        self.assertIn(
            frozenset({CORE, SLOW}),
            {frozenset({link["a"], link["b"]}) for link in drawn["links"]},
        )
        host = next(h for h in drawn["hosts"] if h["mac"] == DEVICE)
        self.assertTrue(host["from_saved"])
        self.assertEqual(host["switch"], SLOW)


if __name__ == "__main__":
    unittest.main()
