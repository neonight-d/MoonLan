"""Names and notes on nodes (v0.8.0): the API, notes without a node,
and importing and exporting them by MAC.

A note is a person's word about a node — known nowhere else — so the
tests that matter most say what is never done to it: overwritten by an
import, put on an offline group, or deleted by MoonLan."""

import asyncio
import json
import unittest

import service_fixture  # noqa: F401  (sets MOONLAN_CONFIG first)
from asgi_client import call
from moonlan import access, notes, server
from moonlan.access import Principal
from moonlan.db import Database

HOST = "00:4d:4c:0b:ec:b5"
OFF_MAP = "00:4d:4c:0b:ec:b6"
CORE_MAC = "02:4d:4c:00:00:01"
CHASSIS = "18:fd:74:fd:b5:cf"
PSEUDO = "pseudo:10.0.0.2:Gi0/9"
OFFLINE = "offline:10.0.0.2:Gi0/5"
BRIDGE = "bridge:" + CHASSIS
STATE = ("switches", "hosts", "bridges", "pseudo_switches", "unlocated",
         "offline_groups", "external_networks", "trunk_groups")


class MapTest(unittest.TestCase):
    """Against a small map: two switches, a device on the map and one
    off it, an LLDP bridge, a switch without SNMP, an offline group."""

    def setUp(self):
        self._saved = {key: getattr(server.state, key) for key in STATE}
        server.state.switches = [
            {"ip": "10.0.0.1", "name": "core", "mac": CORE_MAC},
            {"ip": "10.0.0.2", "name": "edge", "mac": "02:4d:4c:00:00:02"},
        ]
        server.state.hosts = [{"mac": HOST, "switch": "10.0.0.2", "port": "Gi0/3"}]
        server.state.unlocated = [{"mac": OFF_MAP}]
        server.state.bridges = [{"id": BRIDGE, "chassis_id": CHASSIS, "name": "Sport"}]
        server.state.pseudo_switches = [{"id": PSEUDO, "switch": "10.0.0.2", "port": "Gi0/9"}]
        server.state.offline_groups = [{"id": OFFLINE, "switch": "10.0.0.2", "port": "Gi0/5"}]
        server.state.external_networks = []
        server.state.trunk_groups = []
        self.clear()

    def tearDown(self):
        for key, value in self._saved.items():
            setattr(server.state, key, value)
        self.clear()

    def clear(self):
        with server.db._lock, server.db._conn:
            server.db._conn.execute("DELETE FROM notes")

    def put(self, node_id, label="", note=""):
        return call(server.app, "PUT", "/api/notes/" + node_id,
                    json_body={"label": label, "note": note})

    def listed(self):
        return {n["node_id"]: n for n in call(server.app, "GET", "/api/notes").json()["notes"]}


class ApiTest(MapTest):
    def test_written_read_and_on_the_map(self):
        answer = self.put(PSEUDO, "  Room 214 ", "Cameras\r\nsecond floor ")
        self.assertEqual(answer.status, 200)
        note = self.listed()[PSEUDO]
        self.assertEqual((note["label"], note["note"]), ("Room 214", "Cameras\nsecond floor"))
        self.assertEqual((note["source"], note["state"]), ("manual", "present"))
        topo = call(server.app, "GET", "/api/topology").json()
        self.assertEqual(topo["pseudo_switches"][0]["note"]["label"], "Room 214")
        # a node without one has no field at all
        self.assertNotIn("note", topo["switches"][0])

    def test_a_device_off_the_map_is_the_same_device(self):
        self.assertEqual(self.put("unloc:" + OFF_MAP, "Printer").status, 200)
        self.assertIn("host:" + OFF_MAP, self.listed())
        topo = call(server.app, "GET", "/api/topology").json()
        self.assertEqual(topo["unlocated"][0]["note"]["label"], "Printer")

    def test_limits(self):
        for label, note, code in (
            ("x" * 41, "", "label_long"),
            ("two\nlines", "", "label_control"),
            ("bell\x07", "", "label_control"),
            ("", "x" * 2001, "note_long"),
            ("", "nul\x00", "note_control"),
        ):
            with self.subTest(code=code):
                answer = self.put(PSEUDO, label, note)
                self.assertEqual((answer.status, answer.json()["error"]), (400, code))
        # the limits themselves are fine, and a note may have lines
        self.assertEqual(self.put(PSEUDO, "x" * 40, "a\nb\tc" + "x" * 1995).status, 200)

    def test_no_note_on_an_offline_group(self):
        answer = self.put(OFFLINE, "Somewhere")
        self.assertEqual((answer.status, answer.json()["error"]), (400, "offline_group"))
        self.assertEqual(self.listed(), {})

    def test_no_new_note_on_a_node_the_map_has_not(self):
        answer = self.put("pseudo:10.0.0.2:Gi0/30", "Nowhere")
        self.assertEqual((answer.status, answer.json()["error"]), (404, "no_node"))

    def test_empty_name_and_note_delete_the_note(self):
        self.put(PSEUDO, "Room 214")
        self.assertEqual(self.put(PSEUDO, " ", "").json()["note"], None)
        self.assertEqual(self.listed(), {})

    def test_delete(self):
        self.put(PSEUDO, "Room 214")
        self.assertEqual(call(server.app, "DELETE", "/api/notes/" + PSEUDO).status, 200)
        self.assertEqual(call(server.app, "DELETE", "/api/notes/" + PSEUDO).status, 404)

    def test_journal(self):
        self.put(PSEUDO, "Room 214")
        self.put(PSEUDO, "Room 214", "A note")
        call(server.app, "DELETE", "/api/notes/" + PSEUDO)
        events = [e for e in server.db.journal(10) if e["event"] == "note_changed"][:3]
        self.assertEqual([json.loads(e["details"]) for e in events][::-1], [
            {"label": "Room 214", "note": False}, {"note": True}, {"deleted": True},
        ])
        self.assertEqual({e["mac"] for e in events}, {PSEUDO})

    def test_move(self):
        self.put(PSEUDO, "Room 214")
        self.put(BRIDGE, "Sports hall")
        move = lambda to, replace=False: call(
            server.app, "POST", f"/api/notes/{PSEUDO}/move",
            json_body={"to": to, "replace": replace})
        # a node with a note of its own is replaced only when asked
        self.assertEqual((move(BRIDGE).status, move(BRIDGE).json()["error"]),
                         (409, "target_has_note"))
        self.assertEqual(move(OFFLINE).json()["error"], "offline_group")
        self.assertEqual(move("sw:10.0.0.9").status, 404)
        self.assertEqual(move(BRIDGE, replace=True).status, 200)
        listed = self.listed()
        self.assertNotIn(PSEUDO, listed)
        self.assertEqual(listed[BRIDGE]["label"], "Room 214")
        event = server.db.journal(1)[0]
        self.assertEqual((event["event"], json.loads(event["details"])),
                         ("note_moved", {"from": PSEUDO, "to": BRIDGE}))
        # …and on a node without a note, with no question asked
        call(server.app, "POST", f"/api/notes/{BRIDGE}/move", json_body={"to": "sw:10.0.0.1"})
        self.assertEqual(self.listed()["sw:10.0.0.1"]["label"], "Room 214")


class WithoutANodeTest(MapTest):
    def scan(self):
        asyncio.run(server._reconcile_notes())

    def test_one_scan_without_the_node_is_not_without_a_node(self):
        self.put(PSEUDO, "Room 214")
        server.state.pseudo_switches = []
        self.scan()
        self.assertEqual(self.listed()[PSEUDO]["state"], "present")

    def test_three_scans_are(self):
        self.put(PSEUDO, "Room 214")
        server.state.pseudo_switches = []
        for _ in range(notes.MISSING_SCANS):
            self.scan()
        note = self.listed()[PSEUDO]
        self.assertEqual(note["state"], "missing")
        self.assertIsNotNone(note["missing_since"])

    def test_the_node_comes_back_and_takes_its_note(self):
        self.put(PSEUDO, "Room 214")
        saved = server.state.pseudo_switches
        server.state.pseudo_switches = []
        for _ in range(notes.MISSING_SCANS + 2):
            self.scan()
        server.state.pseudo_switches = saved
        self.scan()
        note = self.listed()[PSEUDO]
        self.assertEqual((note["state"], note["missing_since"]), ("present", None))
        topo = call(server.app, "GET", "/api/topology").json()
        self.assertEqual(topo["pseudo_switches"][0]["note"]["label"], "Room 214")

    def test_never_deleted_by_moonlan(self):
        self.put(PSEUDO, "Room 214")
        server.state.pseudo_switches = []
        for _ in range(50):
            self.scan()
        self.assertIn(PSEUDO, self.listed())

    def test_a_note_without_a_node_moves(self):
        self.put(PSEUDO, "Room 214")
        server.state.pseudo_switches = []
        for _ in range(notes.MISSING_SCANS):
            self.scan()
        answer = call(server.app, "POST", f"/api/notes/{PSEUDO}/move",
                      json_body={"to": "host:" + HOST})
        self.assertEqual(answer.status, 200)
        self.assertEqual(self.listed()["host:" + HOST]["state"], "present")


CSV = (
    "mac;label;note\r\n"
    f"{HOST};Accounting;Desk 105\r\n"                       # a device
    "02-4D-4C-00-00-01;Server room;\r\n"                    # a switch's bridge MAC
    "18fd.74fd.b5cf;Sports hall;\"Two\r\nlines\"\r\n"       # an LLDP bridge
    "02005E100001;Printer;\r\n"                             # nobody's
)


class ParseTest(unittest.TestCase):
    def parse(self, text, encoding="utf-8"):
        return notes.parse_csv(text.encode(encoding))

    def test_encodings(self):
        text = "mac;подпись;заметка\r\n" + HOST + ";Бухгалтерия;Иванова И. И.\r\n"
        for data in (text.encode("utf-8"), b"\xef\xbb\xbf" + text.encode("utf-8"),
                     text.encode("cp1251")):
            with self.subTest(data=data[:4]):
                rows, errors, _ = notes.parse_csv(data)
                self.assertEqual(errors, [])
                self.assertEqual((rows[0].label, rows[0].note), ("Бухгалтерия", "Иванова И. И."))

    def test_separators_and_headers(self):
        for text in (
            f"mac;label;note\n{HOST};Room;x\n",
            f"MAC,Label,Note\n{HOST},Room,x\n",
            f"mac\tподпись\tзаметка\n{HOST}\tRoom\tx\n",
            f"Mac;Подпись\n{HOST};Room\n",        # the note column left out
        ):
            with self.subTest(text=text.split("\n")[0]):
                rows, errors, _ = self.parse(text)
                self.assertEqual(errors, [])
                self.assertEqual(rows[0].label, "Room")

    def test_a_note_over_lines_in_quotes(self):
        rows, errors, _ = self.parse(CSV)
        self.assertEqual(errors, [])
        self.assertEqual(rows[2].note, "Two\nlines")

    def test_every_writing_of_a_mac(self):
        rows, _, _ = self.parse(CSV)
        self.assertEqual([r.mac for r in rows], [
            HOST, CORE_MAC, CHASSIS, "02:00:5e:10:00:01",
        ])

    def test_errors_by_line(self):
        text = ("mac;label;note\n"
                "nonsense;x;\n"
                f"{HOST};{'x' * 41};\n"
                f"{HOST};;\n"
                f"{HOST};\"a\nb\";\n")
        _, errors, _ = self.parse(text)
        self.assertEqual([(e["line"], e["error"]) for e in errors], [
            (2, "bad_mac"), (3, "label_long"), (4, "empty"), (5, "label_control"),
        ])

    def test_a_bad_header(self):
        _, errors, _ = self.parse("address;name\nx;y\n")
        self.assertEqual(errors, [{"line": 1, "error": "bad_header"}])

    def test_a_mac_twice_the_last_line_counts(self):
        rows, _, warnings = self.parse(f"mac;label\n{HOST};First\n{HOST};Second\n")
        self.assertEqual([r.label for r in rows], ["Second"])
        self.assertEqual(warnings, [{"line": 3, "error": "duplicate", "mac": HOST, "first": 2}])


class ImportTest(MapTest):
    def importing(self, text, dry_run=False):
        path = "/api/notes/import" + ("?dry_run=1" if dry_run else "")
        return call(server.app, "POST", path, data=text.encode("utf-8"),
                    headers={"content-type": "text/csv"}).json()

    def test_where_each_mac_goes(self):
        report = self.importing(CSV)
        self.assertEqual((report["written"], report["on_map"], report["not_found"]), (4, 3, 1))
        listed = self.listed()
        self.assertEqual(listed["host:" + HOST]["label"], "Accounting")
        self.assertEqual(listed["sw:10.0.0.1"]["label"], "Server room")
        self.assertEqual(listed[BRIDGE]["note"], "Two\nlines")
        printer = listed["host:02:00:5e:10:00:01"]
        self.assertEqual((printer["state"], printer["source"]), ("not_found", "import"))
        event = server.db.journal(1)[0]
        self.assertEqual((event["event"], json.loads(event["details"])),
                         ("notes_imported", {"written": 4, "skipped": 0, "errors": 0}))

    def test_a_dry_run_writes_nothing(self):
        report = self.importing(CSV, dry_run=True)
        self.assertEqual(report["written"], 4)
        self.assertEqual(self.listed(), {})

    def test_a_note_written_by_hand_is_not_touched(self):
        self.put("host:" + HOST, "By hand", "Mine")
        report = self.importing(f"mac;label;note\n{HOST};Imported;Theirs\n")
        self.assertEqual(len(report["skipped_manual"]), 1)
        self.assertEqual(self.listed()["host:" + HOST]["label"], "By hand")

    def test_an_import_overwrites_an_import_and_an_empty_cell_nothing(self):
        self.importing(f"mac;label;note\n{HOST};First;Kept\n")
        self.importing(f"mac;label;note\n{HOST};Second;\n")
        note = self.listed()["host:" + HOST]
        self.assertEqual((note["label"], note["note"]), ("Second", "Kept"))
        # edited by hand, it is a hand-written note from then on
        self.put("host:" + HOST, "Edited", "Kept")
        self.importing(f"mac;label;note\n{HOST};Third;\n")
        self.assertEqual(self.listed()["host:" + HOST]["label"], "Edited")

    def test_export_then_import_gives_the_same(self):
        self.importing(CSV)
        self.put(PSEUDO, "Room 214")
        exported = call(server.app, "GET", "/api/notes/export")
        self.assertTrue(exported.body.startswith(b"\xef\xbb\xbf"))
        preview = call(server.app, "GET", "/api/notes/export?preview=1").json()
        self.assertEqual(preview, {"exported": 4, "left_out": 1})
        before = {k: (v["label"], v["note"]) for k, v in self.listed().items() if k != PSEUDO}
        self.clear()
        call(server.app, "POST", "/api/notes/import", data=exported.body)
        after = {k: (v["label"], v["note"]) for k, v in self.listed().items()}
        self.assertEqual(after, before)


class RightsTest(unittest.TestCase):
    def test_who_may(self):
        roles = (Principal("v", "viewer"), Principal("u", "user"), Principal("a", "admin"))
        allowed = lambda key: [who.role for who in roles if access.decide(key, who, True) is None]
        for key in ("GET /api/notes", "GET /api/notes/export"):
            self.assertEqual(allowed(key), ["viewer", "user", "admin"], key)
        for key in ("PUT /api/notes/{node_id:path}", "DELETE /api/notes/{node_id:path}",
                    "POST /api/notes/{node_id:path}/move"):
            self.assertEqual(allowed(key), ["user", "admin"], key)
        self.assertEqual(allowed("POST /api/notes/import"), ["admin"])

    def test_the_console_imports_and_writes_nothing_else(self):
        console = Principal("@console", "viewer", console=True)
        self.assertIsNone(access.decide("POST /api/notes/import", console, True))
        self.assertIsNone(access.decide("GET /api/notes/export", console, True))
        for key in ("PUT /api/notes/{node_id:path}", "DELETE /api/notes/{node_id:path}",
                    "POST /api/notes/{node_id:path}/move", "DELETE /api/layout"):
            self.assertIsNotNone(access.decide(key, console, True), key)


class DatabaseTest(unittest.TestCase):
    def test_an_import_not_found_yet_is_seen_when_its_device_appears(self):
        db = Database(":memory:")
        db.set_note("host:" + HOST, "Printer", "", "import", "", 1.0, seen=False)
        self.assertEqual(notes.state(db.note("host:" + HOST)), "not_found")
        db.reconcile_notes(set(), 2.0)
        self.assertEqual(notes.state(db.note("host:" + HOST)), "not_found")
        db.reconcile_notes({"host:" + HOST}, 3.0)
        self.assertEqual(notes.state(db.note("host:" + HOST)), "present")


if __name__ == "__main__":
    unittest.main()
