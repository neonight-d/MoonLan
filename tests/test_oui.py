"""The IEEE register of MAC blocks (v0.7.8): reading it, updating it
without ever being left without one, and looking a maker up — on small
registers made here, never from the network.

What the v0.7.8 brief asked of these tests, and where it is:
  MA-S over "IEEE Registration Authority"   LookupTest.test_ma_s_wins_over_ieee_itself
  MA-M over MA-L                            LookupTest.test_ma_m_wins_over_ma_l
  local and group addresses not looked up   LookupTest.test_local_and_group_are_not_looked_up
  case and separators                       LookupTest.test_case_and_separators
  a broken or empty file replaces nothing   UpdateTest.test_one_bad_file_replaces_nothing,
                                            UpdateTest.test_too_few_rows_replaces_nothing
  no register: the service starts, `vendor` ServiceTest.test_the_service_starts_without_a_register
    empty with the reason
"""

import contextlib
import csv
import dataclasses
import io
import logging
import os
import tempfile
import types
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import service_fixture  # noqa: F401  (sets MOONLAN_CONFIG first)
from asgi_client import call
from moonlan import diag, notify, oui, server
from moonlan.config import Config, load_config

HEADER = "Registry,Assignment,Organization Name,Organization Address\r\n"

# A slice of each register, with what makes the four of them necessary:
# 70B3D5 is IEEE's own MA-L block, carved into MA-S blocks; F0ACD7 is
# carved into MA-M blocks. (The makers under them are made up here.)
SAMPLE = {
    "MA-L": [
        ("10FFE0", "GIGA-BYTE TECHNOLOGY CO.,LTD."),
        ("088AF1", "MERCUSYS TECHNOLOGIES CO., LTD."),
        ("70B3D5", "IEEE Registration Authority"),
        ("F0ACD7", "IEEE Registration Authority"),
        ("00000C", "Cisco Systems, Inc"),
    ],
    "MA-M": [("F0ACD74", "Example Block Maker (MA-M)")],
    "MA-S": [("70B3D5F21", "Example Small Maker (MA-S)")],
    "IAB": [("0050C2A1B", "Example Old Block (IAB)")],
}


def csv_bytes(registry: str, rows) -> bytes:
    """A register file as IEEE writes it: quoted where a comma needs it."""
    out = io.StringIO()
    out.write(HEADER)
    writer = csv.writer(out, lineterminator="\r\n")
    for prefix, name in rows:
        writer.writerow([registry, prefix, name,
                         "1 Example Street, Example City"])
    return out.getvalue().encode("utf-8")


def write_sample(folder: Path, which=("MA-L", "MA-M", "MA-S", "IAB")) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for source in oui.SOURCES:
        if source.registry in which:
            (folder / source.file).write_bytes(
                csv_bytes(source.registry, SAMPLE[source.registry])
            )


def small_sources(fewest=3):
    """The real sources, with a threshold a test register can meet."""
    return tuple(dataclasses.replace(s, fewest=fewest) for s in oui.SOURCES)


class Folder(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name) / "oui"


class ParseTest(unittest.TestCase):
    def test_a_register(self):
        rows = oui.parse(csv_bytes("MA-L", SAMPLE["MA-L"]), oui.SOURCES[0])
        self.assertEqual(rows["10FFE0"], "GIGA-BYTE TECHNOLOGY CO.,LTD.")
        self.assertEqual(len(rows), 5)

    def test_the_name_is_kept_as_it_is(self):
        # not shortened, not re-cased: a search on the internet finds it
        rows = oui.parse(
            csv_bytes("MA-L", [("088AF1", "MERCUSYS TECHNOLOGIES CO., LTD.")]),
            oui.SOURCES[0],
        )
        self.assertEqual(rows["088AF1"], "MERCUSYS TECHNOLOGIES CO., LTD.")

    def test_a_byte_order_mark_and_lower_case_hex(self):
        data = b"\xef\xbb\xbf" + csv_bytes("MA-L", [("10ffe0", "X")])
        self.assertEqual(oui.parse(data, oui.SOURCES[0]), {"10FFE0": "X"})

    def test_what_is_not_a_register(self):
        mal = oui.SOURCES[0]
        for data in (b"", b"<!DOCTYPE html><html>Request rejected</html>",
                     HEADER.encode(),
                     csv_bytes("MA-L", [("70B3D5F21", "nine digits")]),
                     b"\xff\xfe\x00broken"):
            with self.subTest(data=data[:30]):
                with self.assertRaises(oui.RegistryError):
                    oui.parse(data, mal)


class LoadTest(Folder):
    def test_all_four(self):
        write_sample(self.folder)
        registry = oui.Registry.load(self.folder)
        self.assertTrue(registry.loaded)
        self.assertEqual(
            {f.source.registry: f.rows for f in registry.files},
            {"MA-L": 5, "MA-M": 1, "MA-S": 1, "IAB": 1},
        )
        self.assertEqual(registry.problems, [])

    def test_no_folder(self):
        registry = oui.Registry.load(self.folder)
        self.assertFalse(registry.loaded)
        self.assertEqual(registry.files, [])

    def test_a_broken_file_is_left_out_and_said(self):
        write_sample(self.folder)
        (self.folder / "mam.csv").write_text("<html>oops</html>")
        registry = oui.Registry.load(self.folder)
        self.assertTrue(registry.loaded)
        self.assertNotIn("MA-M", [f.source.registry for f in registry.files])
        self.assertIn("mam.csv", registry.problems[0])

    def test_the_line_at_startup(self):
        with self.assertLogs("moonlan", "INFO") as logged:
            oui.log_state(oui.Registry.load(self.folder))
        self.assertIn("python -m moonlan.oui update", logged.output[0])
        write_sample(self.folder)
        with self.assertLogs("moonlan", "INFO") as logged:
            oui.log_state(oui.Registry.load(self.folder))
        self.assertEqual(len(logged.output), 1)
        self.assertIn("MA-L 5, MA-M 1, MA-S 1, IAB 1", logged.output[0])
        old = time.time() - (oui.STALE_DAYS + 10) * 86400
        for path in self.folder.iterdir():
            os.utime(path, (old, old))
        with self.assertLogs("moonlan", "INFO") as logged:
            oui.log_state(oui.Registry.load(self.folder))
        self.assertIn("days old", logged.output[-1])
        self.assertTrue(logged.output[-1].startswith("WARNING"))


class UpdateTest(Folder):
    def setUp(self):
        super().setUp()
        patch = mock.patch.object(oui, "SOURCES", small_sources(fewest=1))
        patch.start()
        self.addCleanup(patch.stop)
        self.out = io.StringIO()

    def say(self, *args, **kwargs):
        print(*args, file=self.out, **{k: v for k, v in kwargs.items()
                                       if k in ("end", "flush")})

    def serve(self, broken=None, error=None):
        """A fake IEEE: every file, except `broken` (registry -> bytes)
        or `error` (registry -> exception)."""
        by_url = {s.url: s for s in oui.SOURCES}

        def get(url):
            registry = by_url[url].registry
            if error and registry in error:
                raise error[registry]
            if broken and registry in broken:
                return broken[registry]
            return csv_bytes(registry, SAMPLE[registry])
        return get

    def files(self):
        return {p.name: p.read_bytes() for p in self.folder.iterdir()}

    def test_a_fresh_folder(self):
        self.assertEqual(oui.update(self.folder, self.serve(), self.say), 0)
        self.assertEqual(sorted(self.files()),
                         ["iab.csv", "mam.csv", "oui.csv", "oui36.csv"])
        self.assertIn("restart it", self.out.getvalue())

    def test_one_bad_file_replaces_nothing(self):
        write_sample(self.folder)
        before = self.files()
        for broken in ({"MA-S": b"<html>Access denied</html>"},
                       {"MA-L": b""},
                       {"IAB": csv_bytes("IAB", [])}):
            with self.subTest(broken=list(broken)):
                code = oui.update(self.folder, self.serve(broken=broken),
                                  self.say)
                self.assertEqual(code, 1)
                self.assertEqual(self.files(), before)

    def test_too_few_rows_replaces_nothing(self):
        write_sample(self.folder)
        before = self.files()
        with mock.patch.object(oui, "SOURCES", small_sources(fewest=3)):
            # MA-M has one row in the sample: a file cut short
            self.assertEqual(
                oui.update(self.folder, self.serve(), self.say), 1
            )
        self.assertEqual(self.files(), before)
        self.assertIn("a complete file has over 3", self.out.getvalue())

    def test_refused_says_how_to_do_it_by_hand(self):
        refusal = urllib.error.HTTPError(
            oui.SOURCES[0].url, 418, "I'm a teapot", {}, None
        )
        code = oui.update(self.folder, self.serve(error={"MA-L": refusal}),
                          self.say)
        self.assertEqual(code, 1)
        text = self.out.getvalue()
        self.assertIn("refused: HTTP 418", text)
        self.assertIn("download these files in a browser", text)
        self.assertIn(str(self.folder), text)
        for source in oui.SOURCES:
            self.assertIn(f"{source.url}  ->  {source.file}", text)
        self.assertEqual([p.name for p in self.folder.iterdir()], [])

    def test_no_network(self):
        code = oui.update(
            self.folder,
            self.serve(error={"MA-M": urllib.error.URLError("no route")}),
            self.say,
        )
        self.assertEqual(code, 1)
        self.assertIn("failed: no route", self.out.getvalue())

    def test_a_meaningful_user_agent(self):
        # IEEE answers 418 to Python's own
        self.assertTrue(oui.USER_AGENT.startswith("MoonLan/"))
        self.assertNotIn("Python-urllib", oui.USER_AGENT)


class LookupTest(Folder):
    def setUp(self):
        super().setUp()
        write_sample(self.folder)
        # a "maker" for a locally administered and a group prefix too, to
        # show they are never looked up
        with open(self.folder / "oui.csv", "ab") as handle:
            handle.write(csv_bytes("MA-L", [("0A1B2C", "Nobody's local"),
                                            ("01005E", "Nobody's group")]
                                   )[len(HEADER):])
        self.registry = oui.Registry.load(self.folder)

    def maker(self, mac):
        return self.registry.lookup(mac)

    def test_ma_l(self):
        found = self.maker("10:ff:e0:00:00:01")
        self.assertEqual((found.status, found.name, found.registry, found.prefix),
                         ("found", "GIGA-BYTE TECHNOLOGY CO.,LTD.", "MA-L",
                          "10FFE0"))

    def test_ma_s_wins_over_ieee_itself(self):
        found = self.maker("70:b3:d5:f2:1a:bc")
        self.assertEqual((found.name, found.registry, len(found.prefix)),
                         ("Example Small Maker (MA-S)", "MA-S", 9))
        # outside that small block, the MA-L row is all there is
        self.assertEqual(self.maker("70:b3:d5:00:00:01").name,
                         "IEEE Registration Authority")

    def test_ma_m_wins_over_ma_l(self):
        found = self.maker("f0:ac:d7:4f:00:01")
        self.assertEqual((found.name, found.registry, len(found.prefix)),
                         ("Example Block Maker (MA-M)", "MA-M", 7))

    def test_iab(self):
        found = self.maker("00:50:c2:a1:bf:ff")
        self.assertEqual((found.name, found.registry),
                         ("Example Old Block (IAB)", "IAB"))

    def test_local_and_group_are_not_looked_up(self):
        self.assertEqual(self.maker("0a:1b:2c:00:00:01").status, "local")
        self.assertEqual(self.maker("da:a1:19:00:00:01").status, "local")
        self.assertEqual(self.maker("01:00:5e:00:00:fb").status, "group")
        self.assertEqual(self.maker("ff:ff:ff:ff:ff:ff").status, "group")
        self.assertIsNone(self.maker("0a:1b:2c:00:00:01").name)

    def test_not_in_the_register(self):
        self.assertEqual(self.maker("00:11:22:33:44:55").status,
                         "unregistered")

    def test_case_and_separators(self):
        for mac in ("10:FF:E0:12:34:56", "10-ff-e0-12-34-56", "10ffe0123456",
                    "10FFE0123456", "10ff.e012.3456", "  10:ff:e0:12:34:56 "):
            with self.subTest(mac=mac):
                self.assertEqual(self.maker(mac).name,
                                 "GIGA-BYTE TECHNOLOGY CO.,LTD.")
        for mac in ("", "10:ff:e0", "zz:ff:e0:12:34:56", "10ffe012345600"):
            with self.subTest(mac=mac):
                self.assertEqual(self.maker(mac).status, "invalid")

    def test_without_a_register(self):
        empty = oui.Registry.load(self.folder / "nowhere")
        self.assertEqual(empty.lookup("10:ff:e0:00:00:01").status,
                         "no_registry")
        # what an address is does not depend on the register
        self.assertEqual(empty.lookup("0a:1b:2c:00:00:01").status, "local")
        self.assertEqual(empty.fields("10:ff:e0:00:00:01"),
                         {"vendor": None, "vendor_status": "no_registry"})

    def test_the_api_fields(self):
        self.assertEqual(self.registry.fields("088af1000001"),
                         {"vendor": "MERCUSYS TECHNOLOGIES CO., LTD.",
                          "vendor_status": "found"})
        self.assertEqual(self.registry.fields("00:11:22:33:44:55"),
                         {"vendor": None, "vendor_status": "unregistered"})


# ---------- where the maker is seen ----------

GIGA = "10:ff:e0:00:00:01"
MERCUSYS = ("08:8a:f1:00:00:01", "08:8a:f1:00:00:02")
PHONE = "da:a1:19:00:00:01"      # locally administered: a random address
NOBODY = "00:11:22:33:44:55"     # global, and not in the sample register


class ServiceTest(Folder):
    def setUp(self):
        super().setUp()
        write_sample(self.folder)
        self.use(oui.Registry.load(self.folder))
        saved = (server.state.switches, server.state.hosts,
                 server.state.unlocated, server.state.bridges)
        self.addCleanup(self.restore, saved)
        server.state.switches = [
            {"ip": "10.0.0.1", "name": "core", "mac": "10:ff:e0:00:00:aa"},
        ]
        server.state.hosts = [
            {"mac": mac, "switch": "10.0.0.1", "port": f"Gi0/{n}"}
            for n, mac in enumerate((GIGA, *MERCUSYS, PHONE, NOBODY), 1)
        ]
        server.state.unlocated = []
        server.state.bridges = [
            {"id": "bridge:08:8a:f1:00:00:09", "chassis_id": "08:8a:f1:00:00:09",
             "name": "ap-hall", "switch": "10.0.0.1", "port": "Gi0/9"},
            {"id": "bridge:router-7", "chassis_id": "router-7",
             "name": "router-7", "switch": "10.0.0.1", "port": "Gi0/10"},
        ]

    def use(self, registry):
        patch = mock.patch.object(server, "vendors", registry)
        patch.start()
        self.addCleanup(patch.stop)

    def restore(self, saved):
        (server.state.switches, server.state.hosts, server.state.unlocated,
         server.state.bridges) = saved

    def topology(self):
        return call(server.app, "GET", "/api/topology").json()

    def test_every_host_says_who_made_it(self):
        hosts = {h["mac"]: (h["vendor"], h["vendor_status"])
                 for h in self.topology()["hosts"]}
        self.assertEqual(hosts[GIGA],
                         ("GIGA-BYTE TECHNOLOGY CO.,LTD.", "found"))
        self.assertEqual(hosts[MERCUSYS[1]][1], "found")
        # three different ways of having no name, kept apart
        self.assertEqual(hosts[PHONE], (None, "local"))
        self.assertEqual(hosts[NOBODY], (None, "unregistered"))

    def test_bridges_and_switches_by_their_chassis_mac(self):
        topo = self.topology()
        bridges = {b["chassis_id"]: b for b in topo["bridges"]}
        self.assertEqual(bridges["08:8a:f1:00:00:09"]["vendor"],
                         "MERCUSYS TECHNOLOGIES CO., LTD.")
        # a chassis id that is a name says nothing about a maker
        self.assertNotIn("vendor_status", bridges["router-7"])
        self.assertEqual(topo["switches"][0]["vendor"],
                         "GIGA-BYTE TECHNOLOGY CO.,LTD.")

    def test_without_a_register_the_service_runs_and_says_so(self):
        self.use(oui.Registry.load(self.folder / "nowhere"))
        hosts = {h["mac"]: (h["vendor"], h["vendor_status"])
                 for h in self.topology()["hosts"]}
        self.assertEqual(hosts[GIGA], (None, "no_registry"))
        self.assertEqual(hosts[PHONE], (None, "local"))

    def test_the_service_starts_without_a_register(self):
        # the configuration the tests run with has no register: the
        # service imported, answers, and says in one line what to do
        self.assertFalse(oui.Registry.load(server.config.oui_folder()).loaded)
        self.use(oui.Registry.load(server.config.oui_folder()))
        self.assertEqual(call(server.app, "GET", "/api/health").status, 200)
        with self.assertLogs("moonlan", "INFO") as logged:
            server._log_config()
        lines = [line for line in logged.output if "Vendors:" in line]
        self.assertEqual(len(lines), 1)
        self.assertIn("no IEEE register", lines[0])
        self.assertIn("python -m moonlan.oui update", lines[0])
        host = self.topology()["hosts"][0]
        self.assertEqual((host["vendor"], host["vendor_status"]),
                         (None, "no_registry"))

    def test_search_by_maker(self):
        found = call(server.app, "GET", "/api/search?q=MercuSys").json()
        self.assertEqual(sorted(r["mac"] for r in found["results"]),
                         sorted(MERCUSYS))

    def test_the_journal_names_the_maker(self):
        with server.db._lock, server.db._conn:
            server.db._conn.execute(
                "INSERT INTO journal (ts, event, mac, details) "
                "VALUES (?, 'new_mac', ?, '10.0.0.1 / Gi0/1')",
                (time.time() + 3600, GIGA),
            )
        self.addCleanup(lambda: server.db._conn.execute(
            "DELETE FROM journal WHERE mac = ?", (GIGA,)))
        events = call(server.app, "GET", "/api/journal?limit=5").json()
        event = next(e for e in events["events"] if e["mac"] == GIGA)
        self.assertEqual(event["vendor"], "GIGA-BYTE TECHNOLOGY CO.,LTD.")

    def test_a_new_device_is_announced_with_its_maker(self):
        where = {"switch_ip": "10.0.0.1", "port": "Gi0/15"}
        text = server._new_device_text(GIGA, where)
        self.assertEqual(
            text, "new device by GIGA-BYTE TECHNOLOGY CO.,LTD. on core "
                  "(10.0.0.1) Gi0/15"
        )
        # what Telegram and the mail carry
        self.assertIn("GIGA-BYTE TECHNOLOGY CO.,LTD.", notify.format_text(
            "new_mac", GIGA, "info", text, False))
        self.assertIn("random or hand-set address",
                      server._new_device_text(PHONE, where))
        self.assertIn("not in the IEEE register",
                      server._new_device_text(NOBODY, where))
        self.use(oui.Registry.load(self.folder / "nowhere"))
        self.assertEqual(server._new_device_text(GIGA, where),
                         "new device on core (10.0.0.1) Gi0/15")

    def test_lldp_neighbours_in_the_ports_panel(self):
        neighbour = types.SimpleNamespace(
            chassis_id="08:8a:f1:00:00:09", port_id="1", port_desc="",
            sys_name="ap-hall", sys_desc="", cap_enabled=set(),
            cap_known=False, mgmt_ip="", mgmt_ips=[], rows=1,
        )
        self.assertEqual(server._lldp_dict(neighbour)["vendor"],
                         "MERCUSYS TECHNOLOGIES CO., LTD.")
        neighbour.chassis_id = "router-7"
        self.assertIsNone(server._lldp_dict(neighbour)["vendor"])


# ---------- diag ----------

class DiagTest(Folder):
    def setUp(self):
        super().setUp()
        write_sample(self.folder)
        self.cfg = Config(oui_path=str(self.folder))

    def run_diag(self, function, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            function(*args)
        return out.getvalue()

    def test_why_this_maker(self):
        text = self.run_diag(diag.run_oui_lookup, "70-B3-D5-F2-1A-BC", self.cfg)
        self.assertIn("address:   70:B3:D5:F2:1A:BC", text)
        self.assertIn("MA-L 5, MA-M 1, MA-S 1, IAB 1", text)
        self.assertIn("maker:     Example Small Maker (MA-S)", text)
        self.assertIn("matched:   MA-S, 9 hex digits (36 bits): 70B3D5F21",
                      text)
        self.assertIn("carved from the MA-L block 70B3D5: IEEE Registration "
                      "Authority", text)

    def test_what_is_not_looked_up(self):
        text = self.run_diag(diag.run_oui_lookup, "da:a1:19:00:00:01", self.cfg)
        self.assertIn("random or hand-set", text)
        self.assertIn("bit 1 (locally administered) is set", text)
        text = self.run_diag(diag.run_oui_lookup, "00:11:22:33:44:55", self.cfg)
        self.assertIn("not in the IEEE register", text)
        self.assertIn("001122334 (MA-S, IAB), 0011223 (MA-M), 001122 (MA-L)",
                      text)
        with self.assertRaises(SystemExit):
            self.run_diag(diag.run_oui_lookup, "10:ff:e0", self.cfg)

    def test_config_says_what_is_read(self):
        text = self.run_diag(diag._print_oui, self.cfg)
        self.assertIn(f"(oui.path): {self.folder}", text)
        self.assertRegex(text, r"MA-L  oui\.csv\s+5 rows, \d{4}-\d\d-\d\d")
        (self.folder / "iab.csv").unlink()
        text = self.run_diag(diag._print_oui, self.cfg)
        self.assertIn("IAB   iab.csv    missing", text)
        self.assertIn("a register is missing", text)
        old = time.time() - (oui.STALE_DAYS + 1) * 86400
        for path in self.folder.iterdir():
            os.utime(path, (old, old))
        (self.folder / "iab.csv").write_bytes(csv_bytes("IAB", SAMPLE["IAB"]))
        os.utime(self.folder / "iab.csv", (old, old))
        text = self.run_diag(diag._print_oui, self.cfg)
        self.assertIn(f"older than {oui.STALE_DAYS} days", text)
        self.assertIn("old: the newest makers are missing", text)
        empty = Config(oui_path=str(self.folder / "nowhere"))
        self.assertIn("python -m moonlan.oui update",
                      self.run_diag(diag._print_oui, empty))

    def test_hosts_counts_the_makers(self):
        macs = {GIGA, "10:ff:e0:00:00:02", *MERCUSYS, PHONE, NOBODY}
        text = self.run_diag(diag._print_makers, self.cfg, macs)
        self.assertRegex(text, r"GIGA-BYTE TECHNOLOGY CO\.,LTD\.\s+2")
        self.assertRegex(text, r"MERCUSYS TECHNOLOGIES CO\., LTD\.\s+2")
        self.assertIn("random or hand-set addresses (no maker): 1", text)
        self.assertIn("not in the IEEE register: 1", text)


class ConfigTest(unittest.TestCase):
    def test_next_to_the_database_unless_told(self):
        cfg = Config(db_path="/srv/moonlan/moonlan.db")
        self.assertEqual(cfg.oui_folder(), Path("/srv/moonlan/oui"))
        cfg.oui_path = "/opt/ieee"
        self.assertEqual(cfg.oui_folder(), Path("/opt/ieee"))

    def test_read_from_config_yaml(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.yaml"
            path.write_text("oui:\n  path: /opt/ieee\n", encoding="utf-8")
            self.assertEqual(load_config(path).oui_folder(), Path("/opt/ieee"))


if __name__ == "__main__":
    unittest.main()
