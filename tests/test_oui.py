"""The IEEE register of MAC blocks (v0.7.8): reading it, updating it
without ever being left without one, and looking a maker up — on small
registers made here, never from the network."""

import csv
import dataclasses
import io
import logging
import os
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from moonlan import oui
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
