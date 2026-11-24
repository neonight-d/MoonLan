"""Two fingers move the map (v0.8.10).

vis-network 9.1.9 zooms on a pinch around the point between the fingers
where it is now, and drops a pan while a pinch is on: two fingers zoomed
the map but could not move it. The page now moves the view on every
pinchmove by as much as the centre between the fingers moved, through
vis's public getViewPosition / getScale / moveTo.

Hammer sends pinchmove before pinch, on which vis zooms; with the move
first, the point of the map that was under the fingers is taken to where
they are now and the zoom about that place leaves it there — exact. The
stand-in here zooms the way vis 9.1.9 does (translation' = (1 − k)·p +
translation·k, k = new scale / old scale), and the same steps in the
other order miss by more than a pixel, which is what the order is for.
The first step counts from where the second finger landed, as Hammer's
scale does, not from Hammer's pinchstart a move later.

A second finger also makes the press a gesture: no long-press menu, and
no click when the fingers come up.

The emulation of a browser does not pinch; these run the page's own
handlers on a stand-in for vis and Hammer, as test_long_press does.

Run with:  python -m unittest discover -s tests
"""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from web_source import APP, NODE, function, run_page  # noqa: E402

PAGE = r"""
(async () => {
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const W = 400, H = 300;
// the view as vis keeps it: a point of the map is at map * scale + t
// on the screen
const view = { scale: 2, tx: 40, ty: -30 };
let calls;
let nodeAt = "sw:10.0.0.10";
let selected = [];
const handlers = {};
const reset = () => { calls = { menu: [], tap: 0, close: 0, moves: [] }; };
network = {
  // Hammer calls every listener of an event, in the order they came
  canvas: { hammer: { on(type, fn) {
    const before = handlers[type];
    handlers[type] = before ? (ev) => { before(ev); fn(ev); } : fn;
  } } },
  body: { eventListeners: { onTap: () => { calls.tap += 1; }, onHold: null } },
  getNodeAt: () => nodeAt,
  getSelectedNodes: () => selected,
  selectNodes: (ids) => { selected = ids; },
  getScale: () => view.scale,
  getViewPosition: () => ({ x: (W / 2 - view.tx) / view.scale, y: (H / 2 - view.ty) / view.scale }),
  moveTo: (options) => {
    calls.moves.push(JSON.parse(JSON.stringify(options)));
    view.scale = options.scale;
    view.tx = W / 2 - options.position.x * options.scale;
    view.ty = H / 2 - options.position.y * options.scale;
  },
};
// vis-network 9.1.9's own zoom about a point, which its onPinch calls
// with pinch.scale * event.scale and the centre between the fingers
const visZoom = (scale, at) => {
  const k = scale / view.scale;
  view.tx = (1 - k) * at.x + view.tx * k;
  view.ty = (1 - k) * at.y + view.ty * k;
  view.scale = scale;
};
const onScreen = (p) => ({ x: p.x * view.scale + view.tx, y: p.y * view.scale + view.ty });
const onMap = (s) => ({ x: (s.x - view.tx) / view.scale, y: (s.y - view.ty) / view.scale });
els.network = { getBoundingClientRect: () => ({ left: 0, top: 0 }) };
openNodeMenu = (ids, at) => { calls.menu.push([ids, at.x, at.y]); };
closeNodeMenu = () => { calls.close += 1; };
unpinNode = () => {};
isPinned = () => false;
loosenForDrag = () => {};
holdCurves = () => {};
holdIsAPress();
longPressOpensMenu();
twoFingersMoveTheMap();
const out = { hooked: Object.keys(handlers).sort(), longPressMs: LONG_PRESS.ms };

const touch = (x, y, more = {}) => ({ pointerType: "touch", center: { x, y }, ...more });
const one = (x, y, more = {}) => touch(x, y, { pointers: [{}], maxPointers: 1, ...more });
const two = (x, y, more = {}) => touch(x, y, { pointers: [{}, {}], maxPointers: 2, ...more });
const context = () => onMapContext({
  event: { preventDefault() {}, clientX: 120, clientY: 80 }, pointer: { DOM: { x: 120, y: 80 } },
});

// 1. a move of the centre is a move of the view, the scale as it was
reset();
const before = network.getViewPosition();
handlers.pinchstart(two(100, 100));
handlers.pinchmove(two(130, 120));
const after = network.getViewPosition();
out.pan = {
  moved: [after.x - before.x, after.y - before.y],
  scale: view.scale,
  options: calls.moves[0],
};
handlers.pinchend(two(130, 120));

// 2. a pinch the way Hammer sends it: the second finger lands (scale 1,
// no pinch yet), then on every move hammer.input, pinchstart or
// pinchmove, vis's pinch, and pinchend at the last; the map point that
// was under the fingers when they landed stays under them — the first
// move included, which is where Hammer sees the pinch begin
const pinchPath = [
  [100, 100, 1.0], [104, 102, 1.05], [112, 104, 1.08], [130, 120, 1.25],
  [155, 150, 1.6], [170, 190, 1.4], [150, 210, 0.9], [120, 200, 0.7],
];
const pinchOnce = (moveFirst) => {
  view.scale = 2; view.tx = 40; view.ty = -30;
  const base = view.scale;    // vis: pinch.scale, taken at the first touch
  const held = onMap({ x: 100, y: 100 });
  let worst = 0;
  handlers["hammer.input"](one(100, 100, { isFirst: true }));
  pinchPath.forEach(([x, y, scale], i) => {
    const ev = two(x, y, { scale });
    handlers["hammer.input"](ev);
    if (i === 0) return;      // the touch: Hammer has no pinch yet
    const name = i === 1 ? "pinchstart" : "pinchmove";
    if (moveFirst) handlers[name](ev);
    visZoom(base * scale, ev.center);
    if (!moveFirst) handlers[name](ev);
    const now = onScreen(held);
    worst = Math.max(worst, Math.hypot(now.x - x, now.y - y));
  });
  handlers.pinchend(two(120, 200, { scale: 0.7 }));
  handlers["hammer.input"](two(120, 200, { isFinal: true }));
  return worst;
};
reset();
out.followed = { worst: pinchOnce(true), scale: view.scale };
out.otherOrder = pinchOnce(false);

// 3. after pinchend or pinchcancel a pinchmove with no pinchstart moves
// nothing
reset();
handlers.pinchstart(two(100, 100));
handlers.pinchend(two(100, 100));
handlers.pinchmove(two(160, 160));
handlers.pinchstart(two(100, 100));
handlers.pinchcancel(two(100, 100));
handlers.pinchmove(two(160, 160));
out.stopped = calls.moves.length;

// 4. a second finger during the press: no menu, the press's own time
reset();
handlers["hammer.input"](one(120, 80, { isFirst: true }));
handlers["hammer.input"](two(121, 80, { distance: 1 }));
await sleep(600);
out.secondFinger = calls;
// two fingers landing in one event
reset();
handlers["hammer.input"](two(120, 80, { isFirst: true }));
await sleep(600);
handlers["hammer.input"](two(120, 80, { isFinal: true }));
handlers.pressup(two(120, 80));
out.together = calls;
LONG_PRESS.ms = 20;

// 5. held one finger, added a second, let go: no click, no menu …
reset();
handlers["hammer.input"](one(120, 80, { isFirst: true }));
handlers["hammer.input"](two(121, 80, { distance: 1 }));
handlers["hammer.input"](one(120, 80, { maxPointers: 2 }));
handlers["hammer.input"](one(120, 80, { maxPointers: 2, isFinal: true }));
handlers.pressup(one(120, 80));
out.heldAndLetGo = calls;
// … and none after a pinch either; Android's contextmenu in the middle
// of it opens nothing
reset();
handlers["hammer.input"](one(120, 80, { isFirst: true }));
handlers["hammer.input"](two(121, 80, { distance: 1 }));
handlers.pinchstart(two(121, 80));
context();
handlers.pinchmove(two(150, 100));
handlers.pinchend(two(150, 100));
handlers["hammer.input"](one(120, 80, { maxPointers: 2, isFinal: true }));
handlers.pressup(one(120, 80));
await sleep(50);
out.afterPinch = { menu: calls.menu.length, tap: calls.tap };

// 6. the next press is a press of its own: a click again
reset();
handlers["hammer.input"](one(120, 80, { isFirst: true }));
handlers["hammer.input"](one(120, 80, { isFinal: true }));
handlers.pressup(one(120, 80));
out.nextPress = calls.tap;

// 7. a long press that opened the menu, then a second finger and a
// pinch: the menu goes, as it does for a drag
reset();
handlers["hammer.input"](one(120, 80, { isFirst: true }));
await sleep(50);
handlers["hammer.input"](two(121, 80, { distance: 1 }));
handlers.pinchstart(two(121, 80));
handlers.pinchend(two(121, 80));
handlers["hammer.input"](one(120, 80, { maxPointers: 2, isFinal: true }));
handlers.pressup(one(120, 80));
out.menuThenPinch = { menu: calls.menu.length, close: calls.close, tap: calls.tap };

// 8. one finger with Hammer's own counts: the long press as in v0.8.9
reset();
handlers["hammer.input"](one(120, 80, { isFirst: true }));
handlers["hammer.input"](one(122, 81, { distance: 2 }));
await sleep(50);
handlers["hammer.input"](one(122, 81, { isFinal: true }));
handlers.pressup(one(122, 81));
out.oneFinger = { menu: calls.menu.length, tap: calls.tap };
__done(out);
})();
"""


@unittest.skipUnless(NODE, "needs node to run the page's JavaScript")
class PageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_page(PAGE)

    def test_the_pinch_events_are_heard(self):
        for name in ("pinchstart", "pinchmove", "pinchend", "pinchcancel"):
            self.assertIn(name, self.out["hooked"])

    def test_a_move_of_the_centre_moves_the_view(self):
        pan = self.out["pan"]
        # (+30, +20) on the screen at scale 2: the view's centre goes
        # (−30/2, −20/2) on the map, the map follows the fingers
        self.assertAlmostEqual(pan["moved"][0], -15)
        self.assertAlmostEqual(pan["moved"][1], -10)
        self.assertEqual(pan["scale"], 2)
        self.assertEqual(pan["options"]["scale"], 2)
        self.assertIs(pan["options"]["animation"], False)

    def test_the_point_under_the_fingers_stays_there(self):
        self.assertLessEqual(self.out["followed"]["worst"], 1)
        self.assertAlmostEqual(self.out["followed"]["scale"], 2 * 0.7)

    def test_the_order_is_what_makes_it_exact(self):
        # the same pinch with the move after vis's zoom misses
        self.assertGreater(self.out["otherOrder"], 1)

    def test_end_and_cancel_stop_it(self):
        self.assertEqual(self.out["stopped"], 0)

    def test_a_second_finger_stops_the_long_press(self):
        self.assertEqual(self.out["longPressMs"], 500)
        self.assertEqual(self.out["secondFinger"]["menu"], [])
        self.assertEqual(self.out["together"]["menu"], [])
        self.assertEqual(self.out["together"]["tap"], 0)

    def test_no_click_after_two_fingers(self):
        self.assertEqual(self.out["heldAndLetGo"]["tap"], 0)
        self.assertEqual(self.out["heldAndLetGo"]["menu"], [])
        self.assertEqual(self.out["afterPinch"], {"menu": 0, "tap": 0})

    def test_the_next_press_is_a_click(self):
        self.assertEqual(self.out["nextPress"], 1)

    def test_a_pinch_closes_the_menu_of_a_long_press(self):
        self.assertEqual(self.out["menuThenPinch"], {"menu": 1, "close": 1, "tap": 0})

    def test_one_finger_as_it_was(self):
        self.assertEqual(self.out["oneFinger"], {"menu": 1, "tap": 0})


class SourceTest(unittest.TestCase):
    def test_hooked_up_with_the_long_press(self):
        start = APP.index("if (visInternals.hold === true) {")
        block = APP[start:APP.index("}", start)]
        for call in ("holdIsAPress();", "longPressOpensMenu();", "twoFingersMoveTheMap();"):
            self.assertIn(call, block)

    def test_only_the_public_view_api(self):
        body = function("pinchMove")
        for name in ("network.getScale()", "network.getViewPosition()", "network.moveTo("):
            self.assertIn(name, body)
        self.assertNotRegex(body, r"\bbody\b|translation|\bview\.")
        self.assertIn("animation: false", body)

    def test_the_end_moves_nothing(self):
        self.assertRegex(APP, r'hammer\.on\("pinchend", pinchStop\);')
        self.assertRegex(APP, r'hammer\.on\("pinchcancel", pinchStop\);')
        self.assertNotIn("moveTo", function("pinchStop"))

    def test_the_release_after_a_gesture_is_no_click(self):
        self.assertIn("press.gesture", function("pressUp"))
        self.assertTrue(re.search(r"press\.gesture = false;", function("pressInput")))


if __name__ == "__main__":
    unittest.main()
