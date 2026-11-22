"""Port numbers on the lines (v0.7.9).

A tag at the end of a line with the port it leaves from: off, for the
selection, or everywhere. The short name of a port is a function of its
own, and where `node` is there (GitHub's runners have it) it is run on
the examples of the task; the rest is held to the page's source."""

import json
import re
import shutil
import subprocess
import unittest

from moonlan import demo
from moonlan.topology import build_topology
from web_source import APP, INDEX, STYLE, function, strings

NODE = shutil.which("node")

# (port, members of this side of an aggregate) -> the tag
EXAMPLES = [
    (("25", None), "25"),
    (("Gi0/9", None), "Gi0/9"),
    (("1/15", None), "1/15"),
    (("Te1/0/49", None), "Te1/0/49"),           # 8 characters: as it is
    (("GigabitEthernet1/0/3", None), "1/0/3"),
    (("MoonLan demo switch, 26 ports Port 3", None), "3"),
    (("Slot0/Port 3 Gigabit - Level", None), "3"),
    (("TenGigabitEthernet1/0/1", None), "1/0/1"),
    # an inferred aggregate, by its members since v0.8.6…
    (("LAG 1/25+1/26", None), "LAG"),
    (("LAG 1/1+1/2", None), "LAG"),
    # …and as an older server named it
    (("bridge-port 12", None), "LAG"),
    (("uplink-to-core", None), "uplink-…"),     # no number in it at all
    ((None, None), "?"),
    (("", None), "?"),
    (("Po1", ["3", "4"]), "3,4"),
    (("Po1", ["25", "26", "27", "28"]), "25+3"),
    (("Po1", ["Gi0/3", "Gi0/4"]), "3,4"),        # past 8: the shared prefix goes
    (("Po1", ["Gi0/1", "Gi0/25"]), "1,25"),
    (("Po1", ["Gi0/25", "Gi0/26", "Gi0/27"]), "Gi0/25+2"),
    (("Po1", []), "Po1"),                       # members unknown: the port
    (("LAG Gi0/3+Gi0/4", ["Gi0/3", "Gi0/4"]), "3,4"),
]


def run_port_short(cases):
    script = (
        function("portShort")
        + "\nconsole.log(JSON.stringify("
        + json.dumps([list(args) for args in cases])
        + ".map(([port, members]) => portShort(port, members))));"
    )
    done = subprocess.run([NODE, "-e", script], capture_output=True,
                          text=True, timeout=30, check=True)
    return json.loads(done.stdout)


class PortShortTest(unittest.TestCase):
    @unittest.skipUnless(NODE, "needs node to run the page's JavaScript")
    def test_the_examples(self):
        got = run_port_short([args for args, _ in EXAMPLES])
        for (args, want), tag in zip(EXAMPLES, got):
            with self.subTest(args=args):
                self.assertEqual(tag, want)

    def test_a_pure_function(self):
        # nothing from the page in it: no translation, no map, no state
        source = function("portShort")
        for outside in (r"\bt\(", r"\bfmt\(", r"\bnetwork\b", r"\btopology\b",
                        r"\bels\.", r"\blang\b", r"\bportLabelMode\b"):
            self.assertIsNone(re.search(outside, source), outside)


class LinkPortLabelTest(unittest.TestCase):
    """An inferred aggregate says what it is by its name (v0.8.6)."""

    @unittest.skipUnless(NODE, "needs node to run the page's JavaScript")
    def test_the_name_is_enough(self):
        script = (
            function("linkPortLabel")
            + "\nconst link = { lag: { trunk: true } };"
            + "\nconsole.log(JSON.stringify(["
            + "linkPortLabel(link, 'LAG 1/25+1/26'),"
            + "linkPortLabel(link, 'bridge-port 25'),"
            + "linkPortLabel({ lag: null }, '1/3')]));"
        )
        done = subprocess.run([NODE, "-e", script], capture_output=True,
                              text=True, timeout=30, check=True)
        self.assertEqual(
            json.loads(done.stdout),
            ["LAG 1/25+1/26", "bridge-port 25 (LAG)", "1/3"],
        )


class ModesTest(unittest.TestCase):
    def test_three_modes_kept_in_the_browser(self):
        self.assertIn('const PORT_LABELS_KEY = "moonlan-port-labels";', APP)
        self.assertIn('const PORT_LABEL_MODES = ["off", "selected", "all"];', APP)
        # "for the selection" unless something else was chosen
        self.assertIn(': "selected";', APP[APP.index("let portLabelMode"):][:200])
        for lang in ("en", "ru"):
            texts = strings(lang)
            for key in ("portLabelsBtn", "portLabelsMenu", "portLabels_off",
                        "portLabels_selected", "portLabels_all"):
                self.assertIn(key, texts, f"{lang}: {key}")
        self.assertEqual(
            [strings("ru")["portLabels_" + m] for m in ("off", "selected", "all")],
            ["Выключено", "Для выделенного", "Везде"],
        )

    def test_the_button_and_its_menu(self):
        self.assertIn('id="port-labels-btn" type="button" class="icon-btn"', INDEX)
        self.assertIn('aria-controls="port-labels-menu"', INDEX)
        self.assertIn('id="port-labels-menu" class="context-menu hidden" role="menu"', INDEX)
        # next to Freeze and Arrange
        self.assertLess(INDEX.index('id="arrange-btn"'), INDEX.index('id="port-labels-btn"'))
        self.assertLess(INDEX.index('id="port-labels-btn"'), INDEX.index('id="alarms-btn"'))
        # the shared menu, with the keys, and the current mode ticked
        self.assertIn("menuButtons(portLabelItems()", function("openPortLabelsMenu"))
        self.assertIn("menuKeys(event, els.portLabelsMenu, closePortLabelsMenu)", APP)
        self.assertIn("checked: mode === portLabelMode", function("portLabelItems"))
        self.assertIn('"menuitemradio"', function("menuButtons"))
        self.assertIn("[role^=\"menuitem\"]", function("menuKeys"))
        # lit when not off
        self.assertIn('classList.toggle("active", portLabelMode !== "off")',
                      function("applyPortLabelMode"))
        self.assertIn("#port-labels-btn.active", STYLE)


class SelectionTest(unittest.TestCase):
    def test_which_ends(self):
        tags = function("portTagsFor")
        # nodes first: vis selects a node's lines along with it
        self.assertLess(tags.index("getSelectedNodes()"), tags.index("getSelectedEdges()"))
        # a switch: its own end only; anything else: the switch end
        self.assertIn("id in entry.ends", tags)
        self.assertIn('id.startsWith("sw:")', tags)

    def test_drawn_after_vis_and_on_selection(self):
        self.assertIn('network.on("afterDrawing", drawPortTags);', APP)
        self.assertIn('network.on("select", () => network.redraw());', APP)
        self.assertIn('network.on("deselect", () => network.redraw());', APP)

    def test_drawing(self):
        draw = function("drawPortTags")
        # 11 px on the screen at any zoom
        self.assertIn("PORT_TAG_FONT / scale", draw)
        self.assertIn("tag.alarm ? colors.alarm : colors.ok", draw)
        self.assertIn('portLabelMode === "all" && scale < PORT_LABELS_MIN_SCALE', draw)
        self.assertIn("const PORT_LABELS_MIN_SCALE = 0.5;", APP)
        # one getPositions() a frame, and placed again only on a change
        in_place = function("portTagsInPlace")
        self.assertEqual(in_place.count("getPositions("), 1)
        self.assertIn("if (key === portTagPlacement.key) return portTagPlacement.tags;", in_place)
        self.assertIn("getBoundingBox(id)", in_place)
        # the curve of a line comes from vis, behind one function
        self.assertIn("edge.edgeType.getPoint", function("edgePointFn"))

    def test_ports_recorded_per_end(self):
        build = function("buildGraphData")
        self.assertIn("edge.portAt = { ...edge.portAt, [end]: { port, members: members || null, prefix } };", build)
        # an end that is not the switch itself gets no tag
        self.assertIn("if (edge.from !== end && edge.to !== end) return edge;", build)
        self.assertIn("isLacp && link.lag.a_members", build)
        # …and since v0.8.7 the line of an empty node to the port its
        # management MAC is seen on
        self.assertEqual(build.count("edges.push(fromPort({"), 7)


class DemoTest(unittest.TestCase):
    def test_a_link_with_a_long_port_name(self):
        with self.assertLogs("moonlan", "WARNING"):
            _, links, *_ = build_topology(demo.demo_network())
        longest = max(max(len(link["a_port"] or ""), len(link["b_port"] or ""))
                      for link in links)
        self.assertGreater(longest, 8)


if __name__ == "__main__":
    unittest.main()
