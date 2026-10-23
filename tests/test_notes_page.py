"""Names and notes on the page (v0.8.0), and switches found by maker.

The caption of a named node is read by vis as markup, so the test
that matters most is that a person's text cannot become markup there:
it runs the page's own namedCaption through `node` (GitHub's runners
have it; the case is skipped without), and the same expressions were
run in a browser. The rest is held to the page's source."""

import logging
import re
import unittest
from pathlib import Path

from moonlan import demo, notes
from moonlan.topology import build_topology
from web_source import APP, INDEX, NODE, function, run_js, strings

ROOT = Path(__file__).resolve().parent.parent
CAPTION = ["markupSafe", "namedCaption", "plainCaption"]

# (what, expression, what it must come to)
CASES = [
    # a name that looks like markup stays text: one <b>…</b> pair, the
    # one namedCaption puts round it, and nothing else vis would read
    ("a name with tags",
     'namedCaption("<b>x</b>", ["a*b<c", "<i>"], false)',
     "<b><​b>x<​/b></b>\na*b<​c\n<​i>"),
    ("a switch without SNMP: the name, then what it is, small",
     'namedCaption("Room 214", ["switch without SNMP"], true)',
     "<b>Room 214</b>\n<i>switch without SNMP</i>"),
    ("empty lines are left out",
     'namedCaption("Room", ["core", "", "10.0.0.1"], false)',
     "<b>Room</b>\ncore\n10.0.0.1"),
    ("the title of a named node is words",
     'plainCaption(namedCaption("<b>x</b> *y*", [], false))',
     "<b>x</b> *y*"),
]


def vis_tags(caption):
    """The tags vis's html mode reads, in order"""
    return re.findall(r"</?(?:b|i|code)>", caption)


class CaptionTest(unittest.TestCase):
    @unittest.skipUnless(NODE, "needs node to run the page's JavaScript")
    def test_cases(self):
        for what, expression, expected in CASES:
            with self.subTest(what):
                got = run_js(CAPTION, expression)
                self.assertEqual(got, expected)
                if what == "a name with tags":
                    self.assertEqual(vis_tags(got), ["<b>", "</b>"])

    def test_html_mode_only_for_named_nodes(self):
        font = function("nodeFont")
        self.assertIn('multi: "html"', font)
        self.assertIn("if (!nodeNames.has(id)) return base;", font)
        # markdown is not used: vis crashes on a backslash before a star
        self.assertNotIn('multi: "markdown"', APP)
        self.assertIn('return String(text).replace(/</g, "<\\u200b");', function("markupSafe"))

    def test_every_kind_of_named_node(self):
        build = function("buildGraphData")
        # switch, switch without SNMP, bridge, external network, offline
        # group, device — not a group behind a trunk (v0.8.1)
        self.assertEqual(build.count("namedCaption("), 6)
        self.assertIn('namedCaption(nameOf(ps), [t("pseudoKind")], true)', build)
        for lang in ("en", "ru"):
            self.assertIn("pseudoKind", strings(lang))
        self.assertEqual(strings("ru")["pseudoKind"], "коммутатор без SNMP")


class SearchTest(unittest.TestCase):
    def test_a_switch_by_maker_mac_and_sysdescr(self):
        sidebar = function("renderSidebar")
        self.assertIn(
            "[sw.name, sw.ip, sw.mac, sw.vendor, sw.model, sw.descr, noteSearchText(sw)]",
            sidebar)
        self.assertIn("noteSearchText(h)", sidebar)

    def test_n_of_m(self):
        search = function("applySearchFilter")
        self.assertIn('fmt("searchCount", { n: shown, total: items.length })', search)
        self.assertEqual(strings("ru")["searchCount"], "{n} из {total}")
        self.assertEqual(strings("en")["searchCount"], "{n} of {total}")
        for count in ("els.namedCount", "els.switchCount", "els.hostCount",
                      "els.unlocatedCount"):
            self.assertIn(count, function("searchedLists"))

    def test_the_named_list(self):
        self.assertIn('id="named-section" class="hidden"', INDEX)
        self.assertLess(INDEX.index('id="named-list"'), INDEX.index('id="switch-list"'))
        named = function("namedNodes")
        for kind in ("pseudo", "bridge", "external", "offline"):
            self.assertIn(f'"{kind}"', named)
            self.assertIn("kindLine_" + kind, strings("ru"))
        self.assertEqual(strings("ru")["namedHeader"], "Подписанные")


class EditorTest(unittest.TestCase):
    def test_greyed_with_the_reason(self):
        reason = function("noteReason")
        self.assertIn('nodeId.startsWith("offline:")', reason)
        self.assertIn('needRole("user")', reason)
        self.assertIn("disabled: noteReason(single)", function("menuItemsFor"))
        self.assertIn("gateButton(button, noteReason(nodeId));", function("renderNoteSlot"))

    def test_keys_and_errors(self):
        editor = function("noteEditor")
        self.assertIn('event.key === "Enter"', editor)
        self.assertIn('event.key === "Escape"', editor)
        self.assertIn('maxlength: 40', editor)
        save = function("saveNote")
        # a refusal stays under the fields; what was typed stays too
        self.assertIn("error.textContent = response ? noteErrorText(data.error)", save)

    def test_a_refresh_keeps_the_editor(self):
        self.assertIn("const draft = noteDraft(nodeId);", function("showDetails"))
        self.assertNotIn("showDetails", function("loadTopologyOnce"))

    def test_names_from_the_group(self):
        names = function("groupNames")
        self.assertIn(".slice(0, 8)", names)
        self.assertIn("h.lldp && h.lldp.sys_name", names)
        self.assertIn("a.by - b.by || ipKey(a.ip) - ipKey(b.ip)", names)

    def test_words_in_both_languages(self):
        for lang in ("en", "ru"):
            texts = strings(lang)
            for key in ("noteBtn", "noteLabel", "noteText", "notePickName",
                        "noteMoveHere", "menuOrphans", "menuImportNotes",
                        "menuExportNotes", "orphansTitle", "orphansNotFound",
                        "reasonOfflineGroup", "ev_note_changed", "ev_note_moved",
                        "ev_notes_imported", "importWill", "exportLeftOut"):
                self.assertIn(key, texts, f"{lang}: {key}")
            for code in ("label_control", "label_long", "note_long",
                         "offline_group", "no_node", "target_has_note"):
                self.assertIn("noteErr_" + code, texts, f"{lang}: {code}")
            for code in ("bad_mac", "label_long", "empty", "bad_header"):
                self.assertIn("csvErr_" + code, texts, f"{lang}: {code}")
        self.assertEqual(strings("ru")["noteBtn"], "Подпись и заметка…")


class DemoTest(unittest.TestCase):
    def test_the_demo_has_names_and_a_note_without_a_node(self):
        logging.disable(logging.WARNING)
        try:
            switches, links, hosts, pseudo, *_ = build_topology(demo.demo_network())
        finally:
            logging.disable(logging.NOTSET)
        pseudo_ids = {p["id"] for p in pseudo}
        named = [node_id for node_id, *_ in demo.DEMO_NOTES if node_id.startswith("pseudo:")]
        # two drawn, and one whose devices are all off: its offline group
        # carries the name (v0.8.1)
        self.assertEqual(len([n for n in named if n in pseudo_ids]), 2)
        self.assertEqual([n for n in named if n not in pseudo_ids], ["pseudo:10.0.0.24:Gi0/6"])
        device = [node_id for node_id, _, note in demo.DEMO_NOTES
                  if node_id.startswith("host:") and note]
        self.assertEqual(len(device), 1)
        self.assertIn(device[0][len("host:"):], {h["mac"] for h in hosts})
        self.assertNotIn(demo.DEMO_GONE_NOTE[0], pseudo_ids)

    def test_the_example_file(self):
        rows, errors, warnings = notes.parse_csv((ROOT / "docs" / "notes_example.csv").read_bytes())
        self.assertEqual(len(rows), 5)
        self.assertEqual([e["error"] for e in errors], ["bad_mac"])
        self.assertEqual([w["error"] for w in warnings], ["duplicate"])


if __name__ == "__main__":
    unittest.main()
