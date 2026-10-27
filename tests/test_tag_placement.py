"""The layout of v0.7.8, tags that keep apart, numbers only (v0.7.10).

Three functions of the page are pure and run here through `node`: where
a line's support goes when a frozen node is dragged, where the port
tags go, and what a tag says with "numbers only". Every case is a JS
expression and the JSON it must come to (CASES), so the same scripts
can be checked in a browser where there is no node. The rest is held
to the page's source."""

import re
import unittest

from web_source import APP, NODE, function, run_js, strings

# made-up boxes and lines for the placement, and what to count in it:
# how many tags were placed, how many pairs touch (closer than the 2 px
# kept between tags) and how many touch their own node's box grown by
# the 6 px gap
SCENE = """
const box = (x, y, w, h) => ({ left: x - w / 2, right: x + w / 2, top: y - h / 2, bottom: y + h / 2 });
const line = (a, b) => Array.from({ length: 17 }, (_, k) => ({ x: a.x + (b.x - a.x) * k / 16, y: a.y + (b.y - a.y) * k / 16 }));
const overlap = (a, b, m) => a.x1 < b.x2 + m && b.x1 < a.x2 + m && a.y1 < b.y2 + m && b.y1 < a.y2 + m;
const rect = (t, p) => ({ x1: p.x - t.w / 2, x2: p.x + t.w / 2, y1: p.y - t.h / 2, y2: p.y + t.h / 2 });
const grown = (b, g) => ({ x1: b.left - g, x2: b.right + g, y1: b.top - g, y2: b.bottom + g });
const summary = (tags, mode) => {
  const rs = placePortTags(tags, mode).map((p, i) => p && rect(tags[i], p));
  let pairs = 0;
  let onOwn = 0;
  rs.forEach((r, i) => {
    if (!r) return;
    if (overlap(r, grown(tags[i].own, 5.99), 0)) onOwn++;
    rs.forEach((s, j) => { if (j > i && s && overlap(r, s, 1.99)) pairs++; });
  });
  return { placed: rs.filter(Boolean).length, pairs, onOwn };
};
// a device just beside a wide switch: half the line is inside the box
const beside = [{ w: 30, h: 15, line: line({ x: 0, y: 0 }, { x: 175, y: 10 }), own: box(0, 0, 280, 40), far: box(175, 10, 70, 30), shared: false, rank: 0, node: "sw" }];
// ten lines through one side of a switch
const fan = Array.from({ length: 10 }, (_, i) => ({ w: 34, h: 15, line: line({ x: 0, y: 0 }, { x: -225 + 50 * i, y: 200 }), own: box(0, 0, 120, 30), far: box(-225 + 50 * i, 200, 14, 14), shared: false, rank: 2, node: "sw" }));
// two tags and room for one
const tight = [0, 1].map(() => ({ w: 30, h: 15, line: line({ x: 0, y: 0 }, { x: 70, y: 0 }), own: box(0, 0, 40, 20), far: box(70, 0, 10, 10), shared: false, rank: 2, node: "sw" }));
// a link between two switches: a tag at each end, the middle between them
const link = [
  { w: 30, h: 15, line: line({ x: 0, y: 0 }, { x: 400, y: 0 }), own: box(0, 0, 100, 30), far: box(400, 0, 100, 30), shared: true, rank: 1, node: "a" },
  { w: 30, h: 15, line: line({ x: 400, y: 0 }, { x: 0, y: 0 }), own: box(400, 0, 100, 30), far: box(0, 0, 100, 30), shared: true, rank: 1, node: "b" },
];
"""

PLACE = ["placePortTags"]
VIA = ["movedVia"]
TEXT = ["portShort", "portTagText"]


def scene(expression):
    return "(() => {" + SCENE + "return " + expression + "; })()"


def via(before, after, point):
    seg = lambda s: "{from: {x: %s, y: %s}, to: {x: %s, y: %s}}" % s
    return "movedVia(%s, %s, {x: %s, y: %s})" % (seg(before), seg(after), *point)


def text(port, members, prefix, numbers_only):
    js = lambda v: "null" if v is None else repr(v).replace("'", '"')
    return "portTagText(%s, %s, %s, %s)" % (
        js(port), js(members), js(prefix), "true" if numbers_only else "false")


# (what, functions, expression, what it must come to)
CASES = [
    # a support keeps its place relative to the line
    ("rotated: the bend stays", VIA,
     via((0, 0, 100, 0), (0, 0, 0, 100), (50, 20)), {"x": -20, "y": 50}),
    ("stretched", VIA,
     via((0, 0, 100, 0), (0, 0, 200, 0), (50, 20)), {"x": 100, "y": 40}),
    ("both ends moved alike", VIA,
     via((0, 0, 100, 0), (10, 10, 110, 10), (50, 20)), {"x": 60, "y": 30}),
    ("a line of no length", VIA,
     via((5, 5, 5, 5), (0, 0, 10, 0), (8, 9)), {"x": 8, "y": 4}),

    # tags
    ("beside a wide switch, everywhere", PLACE,
     scene('summary(beside, "all")'), {"placed": 1, "pairs": 0, "onOwn": 0}),
    ("beside a wide switch, selected", PLACE,
     scene('summary(beside, "selected")'), {"placed": 1, "pairs": 0, "onOwn": 0}),
    ("a fan of ten lines", PLACE,
     scene('summary(fan, "all")'), {"placed": 10, "pairs": 0, "onOwn": 0}),
    ("no room, everywhere: left out", PLACE,
     scene('summary(tight, "all")'), {"placed": 1, "pairs": 0, "onOwn": 0}),
    ("no room, selected: put anyway", PLACE,
     scene('summary(tight, "selected")'), {"placed": 2, "pairs": 1, "onOwn": 0}),
    ("a link: each tag on its half", PLACE,
     scene('(([a, b]) => [a.x + 15 <= 200, b.x - 15 >= 200])(placePortTags(link, "all"))'),
     [True, True]),

    # numbers only
    ("GigabitEthernet1/0/3", TEXT,
     text("GigabitEthernet1/0/3", None, "GigabitEthernet1/0/", True), "3"),
    ("GigabitEthernet1/0/5", TEXT,
     text("GigabitEthernet1/0/5", None, "GigabitEthernet1/0/", True), "5"),
    ("ether1", TEXT, text("ether1", None, "ether", True), "1"),
    ("ether3", TEXT, text("ether3", None, "ether", True), "3"),
    ("1/1", TEXT, text("1/1", None, "1/", True), "1"),
    ("1/3", TEXT, text("1/3", None, "1/", True), "3"),
    ("Slot0/25", TEXT, text("Slot0/25", None, "Slot0/", True), "25"),
    ("an aggregate's members", TEXT,
     text("Po1", ["Slot0/23", "Slot0/24"], "Slot0/", True), "23,24"),
    ("more members", TEXT,
     text("Po1", ["Slot0/1", "Slot0/2", "Slot0/3"], "Slot0/", True), "1+2"),
    ("a LAG trunk's bridge-port", TEXT,
     text("bridge-port 3", None, "Gi0/", True), "LAG"),
    ("an aggregate's own name", TEXT, text("Po1", None, "Gi0/", True), "Po1"),
    ("no prefix: a stack keeps its unit", TEXT,
     text("1/5", None, None, True), "1/5"),
    ("tick off", TEXT, text("Slot0/25", None, "Slot0/", False), "Slot0/25"),
    ("tick off, long name", TEXT,
     text("GigabitEthernet1/0/3", None, "GigabitEthernet1/0/", False), "1/0/3"),
]


class PureFunctionsTest(unittest.TestCase):
    @unittest.skipUnless(NODE, "needs node to run the page's JavaScript")
    def test_cases(self):
        for what, names, expression, expected in CASES:
            with self.subTest(what):
                got = run_js(names, expression)
                if isinstance(expected, dict) and all(
                        isinstance(v, (int, float)) for v in expected.values()):
                    for key, value in expected.items():
                        self.assertAlmostEqual(got[key], value, places=6, msg=key)
                else:
                    self.assertEqual(got, expected)

    def test_pure(self):
        # nothing from the page in them: no vis, no map, no state
        for name in ("movedVia", "placePortTags", "portTagText"):
            source = function(name)
            for outside in (r"\bnetwork\b", r"\btopology\b", r"\bels\.",
                            r"\bportLabelMode\b", r"\bportNumbersOnly\b",
                            r"\blocalStorage\b"):
                self.assertIsNone(re.search(outside, source), (name, outside))


class CurvesTest(unittest.TestCase):
    def test_the_curves_of_v0_7_8(self):
        self.assertIn('edges: { smooth: { type: "dynamic" } },', APP)
        self.assertNotIn("smooth: false", APP)
        curves = function("curveParallelEdges")
        # a single line says it outright, a parallel pair is fixed curves
        self.assertIn('group[0].smooth = { enabled: true, type: "dynamic" };', curves)
        self.assertIn('type: clockwise ? "curvedCW" : "curvedCCW"', curves)

    def test_a_dragged_node_takes_its_curves_along(self):
        self.assertIn("holdCurves(params.nodes);", APP)
        self.assertIn('network.on("dragging", (params) => followCurves(params.nodes));', APP)
        # only with the physics off: with it on, vis moves the supports
        self.assertIn("if (!layoutFrozen || !nodeIds.length) return;", function("holdCurves"))
        follow = function("followCurves")
        self.assertIn("movedVia(held.before, support.line(), held.via)", follow)
        self.assertIn("support.toMiddle()", follow)

    def test_vis_internals_in_one_place(self):
        support = function("curveSupport")
        # used there, and looked for once the network is made (v0.8.3)
        check = function("checkVisInternals")
        for internal in ("type.via", "type.positionBezierNode()"):
            self.assertIn(internal, support)
            self.assertEqual(APP.count(internal), support.count(internal) + check.count(internal),
                             internal)
        self.assertIn("if (visInternals.curves !== true) return null;", support)


class TagsTest(unittest.TestCase):
    def test_no_tag_inside_its_own_box(self):
        # the early return that put a tag on its own switch is gone
        place = function("placePortTags")
        self.assertNotIn("if (inside(point(most))) return point(most);", APP)
        self.assertIn("if (o.onOwn(c)) continue;", place)
        # walked along the line, kept apart in a grid
        for word in ("const STEP = 3;", "const MARGIN = 2;", "const GAP = 6;",
                     "grid.get(key)"):
            self.assertIn(word, place)
        self.assertIn('if (!chosen && mode === "selected") chosen = fallback;', place)

    def test_placed_again_only_on_a_change(self):
        in_place = function("portTagsInPlace")
        self.assertIn("if (key === portTagPlacement.key) return portTagPlacement.tags;", in_place)
        for part in ("portLabelMode", "portNumbersOnly", "scale", "portDataVersion",
                     "getSelectedNodes()", "getSelectedEdges()"):
            self.assertIn(part, in_place[in_place.index("const key = ["):])
        # "everywhere" places what is on and around the screen
        self.assertIn("network.DOMtoCanvas", in_place)


class NumbersOnlyTest(unittest.TestCase):
    def test_the_tick(self):
        self.assertIn('const PORT_NUMBERS_KEY = "moonlan-port-labels-numbers";', APP)
        self.assertIn('let portNumbersOnly = localStorage.getItem(PORT_NUMBERS_KEY) === "1";', APP)
        items = function("portLabelItems")
        self.assertIn('{ key: "numbers", group: "text", label: t("portLabelsNumbers"),', items)
        self.assertIn("checkbox: true, checked: portNumbersOnly", items)
        # below a line, under the three modes
        self.assertIn('menuButtons(portLabelItems(), ["mode", "text"]', function("openPortLabelsMenu"))
        self.assertIn('"menuitemcheckbox"', function("menuButtons"))

    def test_words(self):
        self.assertEqual(strings("ru")["portLabelsNumbers"], "Только номера")
        self.assertEqual(strings("en")["portLabelsNumbers"], "Numbers only")

    def test_the_prefix_reaches_the_tags(self):
        build = function("buildGraphData")
        self.assertIn("sw.port_prefix", build)
        self.assertIn("portTagText(at.port, at.members, at.prefix, portNumbersOnly)",
                      function("portTagsInPlace"))


if __name__ == "__main__":
    unittest.main()
