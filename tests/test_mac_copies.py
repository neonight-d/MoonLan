"""Damaged copies of a MAC stay off the map (v0.8.5).

A device whose transmitter garbles frames makes the switch learn
addresses a bit or two away from its own. Until v0.8.5 the copies were
looked for only among the addresses that would STILL be unconfirmed
after the poll. With new_host_confirm_scans = 2 a copy seen for the
second time was not among them — it was about to be confirmed — so it
was measured as a sample instead, and confirmed. A port of a USB-C
hub's Ethernet grew a hundred of them, every one a "device" for the
whole grace window.

The candidates and the samples are now two sets: what the database
has not accepted (however many polls it was seen in), and what it had
accepted before this poll, on the same switch, this poll or within
the grace window.

Run with:  python -m unittest discover -s tests
"""

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from service_fixture import SWITCHES  # noqa: E402,F401

from moonlan import corruption, server  # noqa: E402
from moonlan.snmp_collector import PortInfo, SwitchData  # noqa: E402

CORE, ACCESS = SWITCHES[0], SWITCHES[1]
SAMPLE = "20:7b:d5:1a:31:8d"
COPY = "20:7b:d5:1a:31:87"           # two bits away
COPIES = ["20:7b:d5:1a:31:87", "20:7b:d5:1a:31:8f", "20:7b:d5:1a:31:9d"]
FAR = "20:77:b5:7c:37:87"            # twelve bits away: a neighbour


class CandidateTest(unittest.TestCase):
    ROWS = {
        "aa:00:00:00:00:01": {"confirmed": 1, "ip": "", "switch_ip": ACCESS,
                              "last_seen": 1000.0},
        "aa:00:00:00:00:02": {"confirmed": 0, "ip": "", "switch_ip": ACCESS,
                              "last_seen": 1000.0, "seen_count": 1},
        "aa:00:00:00:00:03": {"confirmed": 0, "ip": "10.0.99.3",
                              "switch_ip": ACCESS, "last_seen": 1000.0},
    }

    def test_seen_before_or_not_it_is_a_candidate_until_accepted(self):
        found = corruption.candidates(
            ["aa:00:00:00:00:01", "aa:00:00:00:00:02", "aa:00:00:00:00:03",
             "aa:00:00:00:00:04"],
            self.ROWS,
        )
        # the unconfirmed one and the brand-new one; a confirmed address
        # and one with an IP are real
        self.assertEqual(found, {"aa:00:00:00:00:02", "aa:00:00:00:00:04"})

    def test_this_polls_arp_vouches_too(self):
        found = corruption.candidates(
            ["aa:00:00:00:00:02", "aa:00:00:00:00:04"], self.ROWS,
            with_ip={"aa:00:00:00:00:04"},
        )
        self.assertEqual(found, {"aa:00:00:00:00:02"})


class SampleTest(unittest.TestCase):
    def test_the_grace_window_and_the_switch_of_this_poll(self):
        now = 100000.0
        rows = {
            "aa:00:00:00:00:01": {"confirmed": 1, "ip": "", "switch_ip": ACCESS,
                                  "last_seen": now - 600},
            "aa:00:00:00:00:02": {"confirmed": 1, "ip": "", "switch_ip": ACCESS,
                                  "last_seen": now - 7200},
            # not accepted: never a sample
            "aa:00:00:00:00:03": {"confirmed": 0, "ip": "", "switch_ip": ACCESS,
                                  "last_seen": now},
            # the database still places it on the core, this poll on ACCESS
            "aa:00:00:00:00:04": {"confirmed": 1, "ip": "", "switch_ip": CORE,
                                  "last_seen": now - 60},
        }
        hosts = [{"mac": "aa:00:00:00:00:04", "switch": ACCESS, "port": "Gi0/3"}]
        samples = corruption.samples_by_switch(hosts, rows, None, 3600, now)
        self.assertEqual(samples[ACCESS], {"aa:00:00:00:00:01", "aa:00:00:00:00:04"})
        self.assertEqual(samples[CORE], {"aa:00:00:00:00:04"})


class SuspectTest(unittest.TestCase):
    def test_the_port_of_the_copy_and_the_nearest_sample(self):
        hosts = [
            {"mac": COPY, "switch": ACCESS, "port": "Gi0/9"},
            {"mac": FAR, "switch": ACCESS, "port": "Gi0/9"},
        ]
        suspects = corruption.find_suspects(
            hosts, {COPY, FAR}, {ACCESS: {SAMPLE}}, 8
        )
        self.assertEqual(
            suspects,
            {(ACCESS, "Gi0/9"): [{"mac": COPY, "sample": SAMPLE, "distance": 2}]},
        )

    def test_a_sample_on_another_switch_does_not_count(self):
        hosts = [{"mac": COPY, "switch": ACCESS, "port": "Gi0/9"}]
        self.assertEqual(
            corruption.find_suspects(hosts, {COPY}, {CORE: {SAMPLE}}, 8), {}
        )

    def test_off_with_no_bits(self):
        hosts = [{"mac": COPY, "switch": ACCESS, "port": "Gi0/9"}]
        self.assertEqual(
            corruption.find_suspects(hosts, {COPY}, {ACCESS: {SAMPLE}}, 0), {}
        )

    def test_likely_copies_before_any_port_is_known(self):
        tables = {ACCESS: {SAMPLE, COPY, FAR}, CORE: {COPY}}
        self.assertEqual(
            corruption.likely_copies(tables, {COPY, FAR}, {ACCESS: {SAMPLE}}, 8),
            {COPY},
        )


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
    """The core, an access switch behind its port 1, and the rest of
    the shared config's switches standing by. `access` and `core` are
    MAC -> port of the hosts this poll finds on each."""

    def __init__(self):
        self.access: dict[str, int] = {}
        self.core: dict[str, int] = {}

    def begin_scan_cycle(self) -> None:
        pass

    async def collect(self, host: str) -> SwitchData:
        core = switch(CORE, "core-sw", 1)
        access = switch(ACCESS, "access-sw", 2)
        core.fdb[access.bridge_mac] = 1
        access.fdb[core.bridge_mac] = 25
        for mac, port in self.access.items():
            access.fdb[mac] = port
            core.fdb[mac] = 1         # the core learns it on its trunk
        for mac, port in self.core.items():
            core.fdb[mac] = port
            access.fdb[mac] = 25
        if host == CORE:
            return core
        if host == ACCESS:
            return access
        return switch(host, f"sw-{host}", int(host.rsplit(".", 1)[1]) + 10)


class ScanTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.collector = StubCollector()
        self._get_collector = server.get_collector
        server.get_collector = lambda: self.collector
        server.switch_data.clear()
        self._saved = (
            server.config.new_host_confirm_scans,
            server.config.filter_suspect_macs,
            server.config.unmanaged_threshold,
            server.config.thresholds.corruption_hamming_bits,
        )
        server.config.new_host_confirm_scans = 2
        server.config.filter_suspect_macs = True
        server.config.unmanaged_threshold = 3
        server.config.thresholds.corruption_hamming_bits = 8
        server.config.switch_snmp = {}
        server.prev_pseudo_ports = set()
        server.db._conn.execute("DELETE FROM hosts")
        server.db._conn.execute("DELETE FROM journal")
        server.db._conn.commit()

    def tearDown(self):
        server.get_collector = self._get_collector
        (
            server.config.new_host_confirm_scans,
            server.config.filter_suspect_macs,
            server.config.unmanaged_threshold,
            server.config.thresholds.corruption_hamming_bits,
        ) = self._saved
        server.state.scanning = False
        server.prev_pseudo_ports = set()

    def row(self, mac: str):
        return server.db.hosts_by_mac().get(mac)

    def new_macs(self, mac: str) -> int:
        return server.db._conn.execute(
            "SELECT COUNT(*) AS n FROM journal WHERE event = 'new_mac' "
            "AND mac = ?", (mac,),
        ).fetchone()["n"]

    def drawn(self) -> set[str]:
        return {h["mac"] for h in server.state.as_dict()["hosts"]}

    async def inventory(self, **where) -> None:
        """The first poll of an empty database: the sample is real."""
        for name, macs in where.items():
            getattr(self.collector, name).update(macs)
        await server.run_scan()
        self.assertTrue(self.row(SAMPLE)["confirmed"])

    async def assert_held(self, mac: str, scans: int = 3) -> None:
        for n in range(scans):
            await server.run_scan()
            with self.subTest(scan=n + 2):
                row = self.row(mac)
                self.assertGreaterEqual(row["seen_count"], n + 1)
                self.assertFalse(row["confirmed"])
                self.assertEqual(self.new_macs(mac), 0)
                self.assertNotIn(mac, self.drawn())

    async def test_seen_in_three_polls_in_a_row_it_is_never_a_device(self):
        """The bug itself: it used to be confirmed in the second."""
        await self.inventory(access={SAMPLE: 7})
        self.collector.access[COPY] = 7
        await self.assert_held(COPY)
        found = server.suspect_by_port[(ACCESS, "Gi0/7")]
        self.assertEqual(found, [{"mac": COPY, "sample": SAMPLE, "distance": 2}])
        self.assertIn(SAMPLE, self.drawn())

    async def test_the_sample_switched_off_ten_minutes_ago(self):
        await self.inventory(access={SAMPLE: 7})
        server.db.set_last_seen(SAMPLE, time.time() - 600)
        self.collector.access = {COPY: 7}
        await self.assert_held(COPY)
        self.assertIn((ACCESS, "Gi0/7"), server.suspect_by_port)

    async def test_the_sample_on_the_next_port(self):
        """Moved from port 3 to port 5: the copies stay, and the port
        they are on is the one the panel and the alarm name."""
        await self.inventory(access={SAMPLE: 5})
        self.collector.access[COPY] = 3
        await self.assert_held(COPY)
        self.assertEqual(
            [s["mac"] for s in server.suspect_by_port[(ACCESS, "Gi0/3")]], [COPY]
        )
        self.assertNotIn((ACCESS, "Gi0/5"), server.suspect_by_port)

    async def test_a_sample_on_another_switch_is_not_one(self):
        """A switch learns a damaged address from the frame that
        reached it: the switch of the sample. Two bits apart on two
        switches are two devices."""
        await self.inventory(core={SAMPLE: 5})
        self.collector.access[COPY] = 7
        await server.run_scan()
        self.assertFalse(self.row(COPY)["confirmed"])
        await server.run_scan()
        self.assertTrue(self.row(COPY)["confirmed"])
        self.assertIn(COPY, self.drawn())
        self.assertEqual(server.suspect_by_port, {})

    async def test_an_ip_confirms_however_alike(self):
        await self.inventory(access={SAMPLE: 7})
        self.collector.access[COPY] = 7
        await server.run_scan()
        self.assertFalse(self.row(COPY)["confirmed"])
        server.db.set_ips({COPY: "10.0.99.87"})
        await server.run_scan()
        self.assertTrue(self.row(COPY)["confirmed"])
        self.assertIn(COPY, self.drawn())
        self.assertEqual(server.suspect_by_port, {})

    async def test_with_the_filter_off_the_copies_are_drawn(self):
        """filter_suspect_macs = false is the choice to see the damage:
        the copies are marked, and confirmed the way they used to be."""
        server.config.filter_suspect_macs = False
        await self.inventory(access={SAMPLE: 7})
        self.collector.access[COPY] = 7
        await server.run_scan()
        await server.run_scan()
        self.assertTrue(self.row(COPY)["confirmed"])
        host = next(h for h in server.state.as_dict()["hosts"] if h["mac"] == COPY)
        self.assertTrue(host["suspect_corrupt"])
        self.assertEqual(host["suspect_of"], SAMPLE)

    async def test_copies_seen_twice_make_no_switch_without_snmp(self):
        """Three copies seen for the second time are not in the
        projection of unconfirmed addresses; voting, they would make
        the port of one real device an unmanaged switch."""
        await self.inventory(access={SAMPLE: 7})
        self.collector.access.update({mac: 7 for mac in COPIES})
        for _ in range(3):
            await server.run_scan()
            pseudo = {
                (p["switch"], p["port"])
                for p in server.state.as_dict()["pseudo_switches"]
            }
            self.assertNotIn((ACCESS, "Gi0/7"), pseudo)
        self.assertEqual(
            sorted(s["mac"] for s in server.suspect_by_port[(ACCESS, "Gi0/7")]),
            sorted(COPIES),
        )

    async def test_a_far_neighbour_is_a_device(self):
        """Twelve bits away is past the threshold: a second device."""
        await self.inventory(access={SAMPLE: 7})
        self.collector.access[FAR] = 7
        await server.run_scan()
        await server.run_scan()
        self.assertTrue(self.row(FAR)["confirmed"])


if __name__ == "__main__":
    unittest.main()
