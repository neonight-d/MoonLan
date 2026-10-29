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

import asyncio
import json
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


class ScanCase(unittest.IsolatedAsyncioTestCase):
    """The service with a stub collector, scan by scan."""

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
        server.db._conn.execute("DELETE FROM notes")
        server.db._conn.execute(
            "DELETE FROM alarms WHERE type = 'port_frame_corruption'"
        )
        server.db._conn.commit()
        server.alarm_engine._corrupt.clear()
        server.alarm_engine._active = {
            key for key in server.alarm_engine._active
            if key[0] != "port_frame_corruption"
        }

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


class ScanTest(ScanCase):
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



def flip(mac: str, *bits: int) -> str:
    """mac with the given bits of its last three octets flipped"""
    number = corruption.mac_number(mac)
    for bit in bits:
        number ^= 1 << bit
    return ":".join(f"{b:02x}" for b in number.to_bytes(6, "big"))


# a hundred copies around the hub's address, one or two bits each
HUNDRED = sorted({flip(SAMPLE, a, b) for a in range(24) for b in range(a, 24)
                  if a != b or a % 3 == 0})[:100]


def record(mac: str, **fields) -> dict:
    row = {
        "mac": mac, "ip": "", "name": "", "switch_ip": ACCESS, "port": "1/5",
        "last_seen": 1000.0, "last_arp": 0, "ip_confirmed": 0,
        "last_ping_ok": 0, "monitored": 0, "seen_count": 2, "confirmed": 1,
        "corrupt_of": "",
    }
    row.update(fields)
    return row


class OldCopiesTest(unittest.TestCase):
    """What an older version confirmed: found by the record alone."""

    def rows(self, *extra: dict, copies=HUNDRED) -> dict[str, dict]:
        rows = {SAMPLE: record(SAMPLE, ip="10.0.99.49", last_arp=900.0,
                               ip_confirmed=900.0, last_ping_ok=950.0,
                               seen_count=4000)}
        rows.update((mac, record(mac)) for mac in copies)
        rows.update((row["mac"], row) for row in extra)
        return rows

    def test_the_hundred_from_the_screenshot(self):
        self.assertEqual(len(HUNDRED), 100)
        batches = corruption.confirmed_copies(self.rows(), set(), 8, 5)
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0]["sample"], SAMPLE)
        self.assertEqual(batches[0]["switch"], ACCESS)
        self.assertEqual(batches[0]["macs"], HUNDRED)
        self.assertEqual(batches[0]["ports"], ["1/5"])

    def test_a_real_neighbour_and_four_copies_are_left_alone(self):
        """Two machines of one batch a bit apart, one without an IP: it
        has been in the tables for days. Four copies are not a crowd."""
        neighbour = record(flip(SAMPLE, 20), seen_count=500)
        rows = self.rows(neighbour, copies=HUNDRED[:4])
        self.assertEqual(corruption.confirmed_copies(rows, set(), 8, 5), [])
        # …and a fifth copy makes it a crowd, the neighbour still apart
        rows = self.rows(neighbour, copies=HUNDRED[:5])
        (batch,) = corruption.confirmed_copies(rows, set(), 8, 5)
        self.assertEqual(batch["macs"], HUNDRED[:5])

    def test_a_note_a_label_or_a_watch_keeps_it(self):
        labelled, watched = HUNDRED[0], HUNDRED[1]
        rows = self.rows()
        rows[watched]["monitored"] = 1
        (batch,) = corruption.confirmed_copies(rows, {labelled}, 8, 5)
        self.assertNotIn(labelled, batch["macs"])
        self.assertNotIn(watched, batch["macs"])
        self.assertEqual(len(batch["macs"]), 98)

    def test_anything_that_ever_had_an_address_is_left_alone(self):
        for field, value in (("ip", "10.0.99.7"), ("last_arp", 900.0),
                             ("ip_confirmed", 900.0), ("last_ping_ok", 900.0),
                             ("name", "pc.example.local"), ("seen_count", 11),
                             ("confirmed", 0)):
            with self.subTest(field):
                rows = self.rows(copies=HUNDRED[:5])
                rows[HUNDRED[0]][field] = value
                # four left: not a crowd any more
                self.assertEqual(corruption.confirmed_copies(rows, set(), 8, 5), [])

    def test_the_sample_must_have_an_ip_on_the_same_switch(self):
        rows = self.rows()
        rows[SAMPLE]["switch_ip"] = CORE
        self.assertEqual(corruption.confirmed_copies(rows, set(), 8, 5), [])

    def test_marked_ones_count_and_are_not_marked_again(self):
        rows = self.rows()
        for mac in HUNDRED[:99]:
            rows[mac]["corrupt_of"] = SAMPLE
        (batch,) = corruption.confirmed_copies(rows, set(), 8, 5)
        self.assertEqual(batch["macs"], HUNDRED[99:])
        rows[HUNDRED[99]]["corrupt_of"] = SAMPLE
        self.assertEqual(corruption.confirmed_copies(rows, set(), 8, 5), [])


class MigrationTest(unittest.TestCase):
    def test_an_older_database_gets_the_column(self):
        import sqlite3
        import tempfile
        from moonlan.db import Database
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            conn = sqlite3.connect(path)
            conn.execute(
                "CREATE TABLE hosts (mac TEXT PRIMARY KEY, ip TEXT DEFAULT '', "
                "name TEXT DEFAULT '', switch_ip TEXT DEFAULT '', "
                "port TEXT DEFAULT '', first_seen REAL NOT NULL, "
                "last_seen REAL NOT NULL, last_ping_ok REAL DEFAULT 0, "
                "ping_up INTEGER DEFAULT 0)"
            )
            conn.execute(
                "INSERT INTO hosts (mac, first_seen, last_seen) VALUES (?, 1, 1)",
                (COPY,),
            )
            conn.commit()
            conn.close()
            with self.assertLogs("moonlan", "INFO") as logs:
                db = Database(path)
            self.assertIn("DB migration: added hosts.corrupt_of column",
                          "\n".join(logs.output))
            self.assertEqual(db.hosts_by_mac()[COPY]["corrupt_of"], "")
            db.close()


class OldCopiesScanTest(ScanCase):
    """The database of the service from the operator's report: the hub
    behind port 5, a hundred copies of its address confirmed, an
    "Offline · 100" beside it."""

    def setUp(self):
        super().setUp()
        now = time.time()
        server.db.seed_host(SAMPLE, ACCESS, "Gi0/5", 1, first_seen=now - 86400,
                            last_seen=now, seen_count=400)
        server.db.set_ips({SAMPLE: "10.0.99.49"})
        for n, mac in enumerate(HUNDRED):
            port = "Gi0/5" if n % 4 else "Gi0/3"  # the hub was on port 3 first
            server.db.seed_host(mac, ACCESS, port, 1, first_seen=now - 7200,
                                last_seen=now - 3600, seen_count=2)
        self.collector.access = {SAMPLE: 5}

    def events(self) -> list:
        return server.db._conn.execute(
            "SELECT mac, details FROM journal WHERE event = 'copies_removed'"
        ).fetchall()

    async def test_off_the_map_in_one_entry_and_gone_after_the_cleanup(self):
        await server.hide_confirmed_copies()
        (event,) = self.events()
        self.assertEqual(event["mac"], SAMPLE)
        data = json.loads(event["details"])
        self.assertEqual(data["n"], 100)
        self.assertEqual(sorted(data["macs"]), HUNDRED)
        self.assertEqual((data["switch"], data["ports"]), (ACCESS, ["Gi0/5", "Gi0/3"]))

        await server.run_scan()
        drawn = server.state.as_dict()
        self.assertEqual(self.drawn(), {SAMPLE})
        self.assertEqual(drawn["offline_groups"], [])
        self.assertEqual(drawn["unlocated"], [])
        self.assertEqual(server._known_hosts_per_port(server.db.hosts_by_mac(), {ACCESS}),
                         {(ACCESS, "Gi0/5"): 1})
        self.assertEqual(len(self.events()), 1)     # not once a scan

        await server.purge_copies()
        self.assertEqual(set(server.db.hosts_by_mac()), {SAMPLE})
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(server.db._conn.execute(
            "SELECT COUNT(*) FROM journal WHERE event <> 'copies_removed'"
        ).fetchone()[0], 0)

    async def test_after_every_scan(self):
        """No start in between: the scan finds them itself."""
        await server.run_scan()
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(self.drawn(), {SAMPLE})

    async def test_back_in_the_table_it_is_still_a_copy(self):
        await server.hide_confirmed_copies()
        self.collector.access.update({mac: 5 for mac in HUNDRED[:3]})
        await server.run_scan()
        self.assertEqual(self.drawn(), {SAMPLE})
        self.assertEqual(
            sorted(s["mac"] for s in server.suspect_by_port[(ACCESS, "Gi0/5")]),
            sorted(HUNDRED[:3]),
        )
        self.assertEqual(server.state.as_dict()["pseudo_switches"], [])

    async def test_an_ip_takes_it_back(self):
        await server.hide_confirmed_copies()
        server.db.set_ips({HUNDRED[0]: "10.0.99.50"})
        self.assertEqual(server.db.hosts_by_mac()[HUNDRED[0]]["corrupt_of"], "")
        await server.purge_copies()
        self.assertIn(HUNDRED[0], server.db.hosts_by_mac())

    async def test_with_the_filter_off_nothing_is_marked_or_removed(self):
        server.config.filter_suspect_macs = False
        await server.hide_confirmed_copies()
        await server.run_scan()
        self.assertEqual(self.events(), [])
        self.assertTrue(set(HUNDRED) <= self.drawn())
        server.db.mark_copies(SAMPLE, HUNDRED)
        await server.purge_copies()
        self.assertEqual(len(server.db.hosts_by_mac()), 101)



JOURNAL_PAGE = r"""
const out = {};
topology = { switches: [{ ip: "10.0.0.2", name: "access-sw" }] };
for (const l of ["en", "ru"]) {
  lang = l;
  out[l] = [104, 1, 2, 5, 21].map((n) => copiesEventText({
    event: "copies_removed", mac: "20:7b:d5:1a:31:8d",
    details: JSON.stringify({ switch: "10.0.0.2", ports: ["Gi0/5", "Gi0/3"], n: n,
                              macs: ["20:7b:d5:1a:31:87"] }),
  }));
}
out.other = copiesEventText({ event: "new_mac", mac: "20:7b:d5:1a:31:8d", details: "" });
__done(out);
"""


class JournalWordsTest(unittest.TestCase):
    def test_both_languages_have_the_entry(self):
        from web_source import strings
        for lang in ("en", "ru"):
            words = strings(lang)
            with self.subTest(lang):
                for key in ("ev_copies_removed", "evCopies", "evAddresses_one",
                            "evAddresses_other"):
                    self.assertTrue(words.get(key), key)
        ru = strings("ru")
        self.assertEqual(ru["ev_copies_removed"], "Убраны искажённые копии")
        for key in ("evAddresses_few", "evAddresses_many"):
            self.assertTrue(ru.get(key), key)

    def test_the_page_words_it(self):
        from web_source import NODE, run_page
        if not NODE:
            self.skipTest("needs node to run the page's JavaScript")
        out = run_page(JOURNAL_PAGE)
        self.assertEqual(out["en"][0],
                         "20:7b:d5:1a:31:8d: 104 addresses on access-sw Gi0/5, Gi0/3")
        self.assertEqual(out["en"][1],
                         "20:7b:d5:1a:31:8d: 1 address on access-sw Gi0/5, Gi0/3")
        self.assertEqual(
            [text.split(": ", 1)[1].split(" на ")[0] for text in out["ru"]],
            ["104 адреса", "1 адрес", "2 адреса", "5 адресов", "21 адрес"],
        )
        self.assertEqual(out["other"], "")



class AlarmWordsTest(unittest.IsolatedAsyncioTestCase):
    """The message the channels get says which fault is likelier."""

    def setUp(self):
        from moonlan.alarms import AlarmEngine
        from moonlan.config import Config
        from moonlan.db import Database
        from moonlan.notify import Notifier
        cfg = Config()
        self.db = Database(":memory:")
        self.addCleanup(self.db.close)
        self.engine = AlarmEngine(self.db, Notifier(cfg), cfg.thresholds,
                                  cfg.notifications)

    def message(self) -> str:
        (row,) = self.db.alarms(True, 10)
        self.assertEqual(row["type"], "port_frame_corruption")
        return row["message"]

    async def test_copies_of_one_address(self):
        found = [{"mac": mac, "sample": SAMPLE, "distance": 1} for mac in HUNDRED[:6]]
        await self.engine.on_corruption(
            {f"{ACCESS}:1/5": found},
            inbound_errors=lambda subject: 340 if subject == f"{ACCESS}:1/5" else None,
            window_minutes=10, maker=lambda mac: "Realtek" if mac == SAMPLE else "",
        )
        message = self.message()
        self.assertIn(f"6 distorted copies of {SAMPLE} (Realtek)", message)
        self.assertIn("its network adapter is the likelier fault", message)
        self.assertIn("; inbound errors in 10 min: 340", message)
        self.assertNotIn("cable", message)
        self.assertEqual(self.engine.corruption_samples(f"{ACCESS}:1/5"), {SAMPLE: 6})

    async def test_copies_of_several_addresses(self):
        other = "00:1e:67:aa:bb:01"
        found = [{"mac": mac, "sample": SAMPLE, "distance": 1} for mac in HUNDRED[:4]]
        found += [{"mac": flip(other, n), "sample": other, "distance": 1} for n in (1, 2)]
        await self.engine.on_corruption({f"{ACCESS}:1/5": found})
        message = self.message()
        self.assertIn("6 distorted copies of 2 addresses", message)
        self.assertIn("the cable, the patch cord, the port or an unmanaged switch", message)
        # no counters, no number
        self.assertNotIn("inbound errors", message)
        self.assertEqual(self.engine.corruption_samples(f"{ACCESS}:1/5"),
                         {SAMPLE: 4, other: 2})


class InboundErrorsTest(unittest.TestCase):
    def test_counted_over_the_window_and_unknown_is_not_zero(self):
        from moonlan.counters import CounterStore, Sample
        store = CounterStore()
        now = time.time()
        errors = 0
        for n in range(16):
            # 34 inbound errors a minute for a quarter of an hour
            store.update(ACCESS, {5: Sample(ts=now - (15 - n) * 60, in_errors=errors),
                                  6: Sample(ts=now - (15 - n) * 60)})
            errors += 34
        self.assertEqual(store.inbound_errors(ACCESS, 5, now - 600), 340)
        self.assertEqual(store.inbound_errors(ACCESS, 5, now - 3600), 510)
        self.assertIsNone(store.inbound_errors(ACCESS, 6, now - 600))
        self.assertIsNone(store.inbound_errors(ACCESS, 7, now - 600))


class HintScanTest(ScanCase):
    """What the alarm list and the ports panel are given to word."""

    async def test_the_alarm_and_the_panel_carry_the_addresses(self):
        from asgi_client import call
        await self.inventory(access={SAMPLE: 5})
        copies = iter(HUNDRED)
        for _ in range(3):
            # different copies every poll, the way a transmitter makes them
            self.collector.access.update({next(copies): 5, next(copies): 5})
            await server.run_scan()
        # the client runs a loop of its own: from a thread, beside this one
        alarms = (await asyncio.to_thread(call, server.app, "GET", "/api/alarms")
                  ).json()["alarms"]
        (alarm,) = [a for a in alarms if a["type"] == "port_frame_corruption"]
        self.assertEqual(alarm["subject"], f"{ACCESS}:Gi0/5")
        self.assertEqual(alarm["corruption"]["samples"],
                         [{"mac": SAMPLE, "vendor": "", "n": 6}])
        self.assertIsNone(alarm["corruption"]["errors_in"])  # no counters here
        self.assertEqual(alarm["corruption"]["minutes"], 10)

        ports = (await asyncio.to_thread(
            call, server.app, "GET", f"/api/switch/{ACCESS}/ports")).json()["ports"]
        port = next(p for p in ports if p["name"] == "Gi0/5")
        self.assertEqual(port["suspect_samples"], [{"mac": SAMPLE, "vendor": "", "n": 6}])
        self.assertEqual(len(port["suspect_macs"]), 6)

    def test_the_port_s_inbound_errors_by_its_name(self):
        from moonlan.counters import Sample
        sw = switch(ACCESS, "access-sw", 2)
        server.switch_data[ACCESS] = sw
        store = server.counter_store
        self.addCleanup(lambda: [store._rates.pop((ACCESS, 5), None),
                                 store._last.pop((ACCESS, 5), None)])
        now = time.time()
        for n, errors in enumerate((0, 100, 300)):
            store.update(ACCESS, {5: Sample(ts=now - (2 - n) * 60, in_errors=errors)})
        self.assertEqual(server._subject_inbound_errors(f"{ACCESS}:Gi0/5"), 300)
        self.assertIsNone(server._subject_inbound_errors(f"{ACCESS}:Gi0/9"))
        self.assertIsNone(server._subject_inbound_errors("192.0.2.1:Gi0/5"))


HINT_PAGE = r"""
const out = {};
const one = [{ mac: "20:7b:d5:1a:31:8d", vendor: "Realtek", n: 104 }];
const nameless = [{ mac: "20:7b:d5:1a:31:8d", vendor: "", n: 104 }];
const several = [{ mac: "20:7b:d5:1a:31:8d", vendor: "", n: 3 }, { mac: "00:1e:67:aa:bb:01", vendor: "", n: 2 }];
for (const l of ["en", "ru"]) {
  lang = l;
  out[l] = {
    one: copiesVerdict(one, "suspectOneHint", "suspectManyHint"),
    nameless: copiesVerdict(nameless, "suspectOneHint", "suspectManyHint"),
    several: copiesVerdict(several, "suspectOneHint", "suspectManyHint"),
    none: copiesVerdict(undefined, "suspectOneHint", "suspectManyHint"),
    alarm: corruptionAlarmText({ samples: one, errors_in: 340, minutes: 10 }),
    count: [1, 3, 5, 21].map((n) => fmt("suspectMacs", { count: plural("evAddresses", n) })),
    alarmSeveral: corruptionAlarmText({ samples: several, errors_in: null, minutes: 10 }),
    old: corruptionAlarmText(undefined),
  };
}
__done(out);
"""


class HintWordsTest(unittest.TestCase):
    def test_every_word_in_both_languages(self):
        from web_source import strings
        for lang in ("en", "ru"):
            words = strings(lang)
            for key in ("suspectOneHint", "suspectManyHint", "corruptionOne",
                        "corruptionMany", "corruptionErrorsIn"):
                with self.subTest(lang=lang, key=key):
                    self.assertTrue(words.get(key))
                    if key.endswith("One") or key == "suspectOneHint":
                        self.assertIn("{mac}{maker}", words[key])
        # the old advice is not the whole story any more
        self.assertNotIn("кабель", strings("ru")["suspectMacsHint"])

    def test_the_page_words_it(self):
        from web_source import NODE, run_page
        if not NODE:
            self.skipTest("needs node to run the page's JavaScript")
        out = run_page(HINT_PAGE)
        ru, en = out["ru"], out["en"]
        self.assertEqual(
            ru["one"],
            "Копии одного адреса 20:7b:d5:1a:31:8d (Realtek) — вероятнее всего, "
            "неисправен его сетевой адаптер. Проверьте подключение этого "
            "устройства через другой адаптер.",
        )
        self.assertIn("20:7b:d5:1a:31:8d —", ru["nameless"])
        self.assertIn("кабель, патч-корд, порт или неуправляемый коммутатор", ru["several"])
        self.assertEqual(ru["none"], "")
        self.assertEqual(
            ru["alarm"],
            "Копии одного адреса 20:7b:d5:1a:31:8d (Realtek) — вероятнее всего, "
            "неисправен его сетевой адаптер · входящих ошибок за 10 мин: 340",
        )
        self.assertNotIn("ошибок", ru["alarmSeveral"])
        self.assertEqual(ru["old"], "")
        # nothing English in the Russian ones but the address and the maker
        for text in (v for v in ru.values() if isinstance(v, str)):
            self.assertNotRegex(text.replace("20:7b:d5:1a:31:8d", "")
                                .replace("Realtek", ""), "[A-Za-z]", text)
        # the number agrees with its word: not "3 адресов"
        self.assertEqual([c.rsplit(": ", 1)[1] for c in ru["count"]],
                         ["1 адрес", "3 адреса", "5 адресов", "21 адрес"])
        self.assertEqual(en["count"][1], "Suspected frame corruption: 3 addresses")
        self.assertIn("network adapter", en["one"])
        self.assertIn("inbound errors in 10 min: 340", en["alarm"])



# Fourteen scans of the demo in a process of its own (the service reads
# its configuration once, at import): from the 13th on, its copies
# repeat — the case the old rule confirmed
DEMO_SCANS = r"""
import asyncio, json, logging
logging.disable(logging.CRITICAL)
from moonlan import demo, server
from asgi_client import call

out = {"scans": []}
port = (demo.CORRUPT_SWITCH, f"Gi0/{demo.CORRUPT_PORT}")
real = {demo.CORRUPT_REAL, demo.CORRUPT_NEIGHBOR}


async def scans():
    for _ in range(14):
        await server.run_scan()
        rows = server.db.hosts_by_mac()
        topo = server.state.as_dict()
        out["scans"].append({
            "drawn": sorted(h["mac"] for h in topo["hosts"]
                            if (h["switch"], h["port"]) == port),
            "grouped": [g["count"] for g in topo["offline_groups"]
                        if (g["switch"], g["port"]) == port],
            "pseudo": [p["id"] for p in topo["pseudo_switches"]
                       if (p["switch"], p["port"]) == port],
            "confirmed": sorted(
                mac for mac, row in rows.items()
                if (row["switch_ip"], row["port"]) == port
                and mac not in real and row["confirmed"]
                and not row["corrupt_of"]),
            "held": sum(1 for mac, row in rows.items()
                        if (row["switch_ip"], row["port"]) == port
                        and not row["confirmed"]),
        })

asyncio.run(scans())
out["events"] = [json.loads(e["details"])["n"] for e in call(
    server.app, "GET", "/api/journal").json()["events"]
    if e["event"] == "copies_removed"]
ports = call(server.app, "GET", f"/api/switch/{port[0]}/ports").json()["ports"]
out["samples"] = next(p for p in ports if p["name"] == port[1])["suspect_samples"]
out["alarms"] = [a.get("corruption") for a in call(
    server.app, "GET", "/api/alarms").json()["alarms"]
    if a["type"] == "port_frame_corruption"]
out["real"] = sorted(real)
print(json.dumps(out))
"""


class DemoTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        import subprocess
        import tempfile
        root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.yaml"
            config.write_text(f"db_path: {Path(tmp) / 'moonlan.db'}\n", encoding="utf-8")
            env = {**os.environ, "MOONLAN_DEMO": "1", "MOONLAN_CONFIG": str(config),
                   "PYTHONPATH": os.pathsep.join([str(root), str(root / "tests")])}
            done = subprocess.run([sys.executable, "-c", DEMO_SCANS], cwd=tmp, env=env,
                                  capture_output=True, text=True, timeout=300, check=True)
        cls.out = json.loads(done.stdout.strip().splitlines()[-1])

    def test_no_copy_on_the_map_in_any_scan(self):
        for n, scan in enumerate(self.out["scans"], start=1):
            with self.subTest(scan=n):
                self.assertEqual(scan["drawn"], self.out["real"])
                self.assertEqual(scan["confirmed"], [])
                self.assertEqual(scan["grouped"], [])
                self.assertEqual(scan["pseudo"], [])
        # they are there, held back: three new ones a scan, 32 in all
        self.assertEqual(self.out["scans"][-1]["held"], 32)

    def test_the_old_copies_go_in_one_entry(self):
        self.assertEqual(self.out["events"], [12])

    def test_the_panel_and_the_alarm_say_one_address(self):
        from moonlan import demo
        self.assertEqual([s["mac"] for s in self.out["samples"]], [demo.CORRUPT_REAL])
        (alarm,) = self.out["alarms"]
        self.assertEqual([s["mac"] for s in alarm["samples"]], [demo.CORRUPT_REAL])


if __name__ == "__main__":
    unittest.main()
