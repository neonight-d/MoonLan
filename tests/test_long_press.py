"""A long press on the map keeps its menu (v0.8.9).

On Android a long press sends contextmenu and vis opened the node menu;
then the finger came up, the release went on to vis's onTap as a click
(holdIsAPress, v0.8.4), and the click closed the menu and opened the
card. Safari on an iPhone sends no contextmenu for a long press, so the
menu never opened there.

Now a finger held still opens the menu by itself, and contextmenu does
if it comes first — one of them per press. A press that opened the
menu, or released a pinned node in Arrange, ends without a click; a
finger that moves on is a drag that closes the menu. The press is told
apart by a flag started over at the first input of every press, never
by swallowing the next click. A mouse is as it was.

The emulation of a browser does not do a long press; these run the
page's own handlers on a stand-in for vis and Hammer.

Run with:  python -m unittest discover -s tests
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from web_source import APP, NODE, function, run_page  # noqa: E402

PAGE = r"""
(async () => {
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
let calls;
let nodeAt = "sw:10.0.0.10";
let selected = [];
const pinned = new Set();
const handlers = {};
const reset = () => { calls = { menu: [], tap: 0, unpin: [], close: 0 }; };
network = {
  canvas: { hammer: { on(type, fn) { handlers[type] = fn; } } },
  body: { eventListeners: { onTap: () => { calls.tap += 1; }, onHold: null } },
  getNodeAt: () => nodeAt,
  getSelectedNodes: () => selected,
  selectNodes: (ids) => { selected = ids; },
};
els.network = { getBoundingClientRect: () => ({ left: 0, top: 0 }) };
openNodeMenu = (ids, at) => { calls.menu.push([ids, at.x, at.y]); };
closeNodeMenu = () => { calls.close += 1; };
unpinNode = (id) => { calls.unpin.push(id); };
isPinned = (id) => pinned.has(id);
loosenForDrag = () => {};
holdCurves = () => {};
holdIsAPress();
longPressOpensMenu();
LONG_PRESS.ms = 20;

const finger = (kind = "touch") => ({ isFirst: true, pointerType: kind, center: { x: 120, y: 80 } });
const lift = (kind = "touch") => ({ isFinal: true, pointerType: kind, center: { x: 120, y: 80 } });
const context = () => onMapContext({
  event: { preventDefault() {}, clientX: 120, clientY: 80 }, pointer: { DOM: { x: 120, y: 80 } },
});
const out = {};

// 1. Android: the hold, contextmenu before our timer, the release
reset(); LONG_PRESS.ms = 10000;
handlers["hammer.input"](finger()); context(); handlers["hammer.input"](lift()); handlers.pressup({});
out.android = calls;

// 2. iOS: no contextmenu, the hold alone
reset(); LONG_PRESS.ms = 20;
handlers["hammer.input"](finger()); await sleep(50); handlers["hammer.input"](lift()); handlers.pressup({});
out.ios = calls;

// 3. the hold, then a late contextmenu: not a second menu
reset();
handlers["hammer.input"](finger()); await sleep(50); context(); handlers["hammer.input"](lift()); handlers.pressup({});
out.late = calls;

// 4. an unhurried press with no menu: a click, as before
reset(); LONG_PRESS.ms = 10000;
handlers["hammer.input"](finger()); handlers["hammer.input"](lift()); handlers.pressup({});
out.click = calls;

// 5. after a long press with its menu, the next press is a click again
reset(); LONG_PRESS.ms = 20;
handlers["hammer.input"](finger()); await sleep(50); handlers["hammer.input"](lift()); handlers.pressup({});
const before = calls.tap;
LONG_PRESS.ms = 10000;
handlers["hammer.input"](finger()); handlers["hammer.input"](lift()); handlers.pressup({});
out.nextTap = [before, calls.tap];

// 6. the finger moves on after the menu opened: a drag that closes it
reset(); LONG_PRESS.ms = 20;
handlers["hammer.input"](finger()); await sleep(50);
handlers["hammer.input"]({ distance: 40, pointerType: "touch", center: { x: 160, y: 80 } });
onMapDragStart({ nodes: [nodeAt] });
handlers["hammer.input"](lift());
out.drag = calls;

// moving before the time is up is a drag from the start: no menu
reset();
handlers["hammer.input"](finger());
handlers["hammer.input"]({ distance: 40, pointerType: "touch", center: { x: 160, y: 80 } });
await sleep(50); handlers["hammer.input"](lift());
out.movedEarly = calls;

// 7. Arrange, a pinned node: released, no menu, no card
reset(); arrangeMode = true; pinned.add(nodeAt);
handlers["hammer.input"](finger()); await sleep(50); handlers["hammer.input"](lift()); handlers.pressup({});
out.arrange = calls;
arrangeMode = false; pinned.clear();

// a long press on the empty map is a press, and its release a click
reset(); nodeAt = undefined;
handlers["hammer.input"](finger()); await sleep(50); handlers["hammer.input"](lift()); handlers.pressup({});
out.empty = calls;
nodeAt = "sw:10.0.0.10";

// a mouse: held still it is a click, never a menu …
reset();
handlers["hammer.input"](finger("mouse")); await sleep(50); handlers["hammer.input"](lift("mouse")); handlers.pressup({});
out.mouseHeld = calls;
reset();
// … and the right button, which starts no press, opens the menu even
// right after a long press answered a finger
handlers["hammer.input"](finger()); await sleep(50); handlers["hammer.input"](lift()); handlers.pressup({});
reset();
context();
out.rightButton = calls;
// within the selection the menu is the selection's
reset(); selected = ["sw:10.0.0.10", "sw:10.0.0.21"];
context();
out.selection = calls.menu[0][0];
__done(out);
})();
"""


@unittest.skipUnless(NODE, "needs node to run the page's JavaScript")
class PageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_page(PAGE)

    def menu(self, case):
        return [ids for ids, _x, _y in self.out[case]["menu"]]

    def test_android_hold_contextmenu_release(self):
        self.assertEqual(self.menu("android"), [["sw:10.0.0.10"]])
        self.assertEqual(self.out["android"]["menu"][0][1:], [120, 80])
        self.assertEqual(self.out["android"]["tap"], 0)   # no card

    def test_ios_the_hold_alone(self):
        self.assertEqual(self.menu("ios"), [["sw:10.0.0.10"]])
        self.assertEqual(self.out["ios"]["tap"], 0)

    def test_one_menu_per_press(self):
        self.assertEqual(len(self.menu("late")), 1)
        self.assertEqual(self.out["late"]["tap"], 0)

    def test_a_press_without_a_menu_is_a_click(self):
        self.assertEqual(self.menu("click"), [])
        self.assertEqual(self.out["click"]["tap"], 1)

    def test_no_real_click_is_lost(self):
        self.assertEqual(self.out["nextTap"], [0, 1])

    def test_a_drag_closes_the_menu(self):
        self.assertEqual(len(self.menu("drag")), 1)
        self.assertEqual(self.out["drag"]["close"], 1)
        self.assertEqual(self.menu("movedEarly"), [])
        self.assertEqual(self.out["movedEarly"]["close"], 0)

    def test_arrange_releases_a_pinned_node(self):
        self.assertEqual(self.out["arrange"]["unpin"], ["sw:10.0.0.10"])
        self.assertEqual(self.menu("arrange"), [])
        self.assertEqual(self.out["arrange"]["tap"], 0)

    def test_the_empty_map(self):
        self.assertEqual(self.menu("empty"), [])
        self.assertEqual(self.out["empty"]["tap"], 1)

    def test_a_mouse_as_it_was(self):
        self.assertEqual(self.menu("mouseHeld"), [])
        self.assertEqual(self.out["mouseHeld"]["tap"], 1)
        self.assertEqual(self.menu("rightButton"), [["sw:10.0.0.10"]])
        self.assertEqual(self.out["selection"], ["sw:10.0.0.10", "sw:10.0.0.21"])


class SourceTest(unittest.TestCase):
    def test_the_flag_starts_over_with_each_press(self):
        body = function("pressInput")
        self.assertIn("if (event.isFirst) {", body)
        self.assertIn("press.handled = false;", body)
        # never "swallow the next click"
        self.assertNotRegex(APP, r"swallow\w*\s*=\s*true")

    def test_the_handlers_are_the_named_ones(self):
        for line in ('network.on("oncontext", onMapContext);',
                     'network.on("click", onMapClick);',
                     'network.on("dragStart", onMapDragStart);'):
            self.assertIn(line, APP)
        self.assertIn('network.canvas.hammer.on("pressup", pressUp);', function("holdIsAPress"))


if __name__ == "__main__":
    unittest.main()
