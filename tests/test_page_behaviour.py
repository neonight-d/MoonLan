"""The page's own behaviour, from the v0.8.3 audit: a late answer opens
nothing that was closed, a double press sends one request, fmt() fills
every place as given, every button has a type. Run through `node` on
the page's own code where it has to be run (skipped without it)."""

import re
import unittest

from web_source import APP, INDEX, NODE, function, run_page

PAGE = r"""
(async () => {
lang = "en";
topology = __DATA.topology;
const out = {};

// a panel that knows whether it is open
const panel = () => {
  const classes = new Set(["hidden"]);
  return { classList: {
    add: (c) => classes.add(c), remove: (c) => classes.delete(c), contains: (c) => classes.has(c),
    toggle: (c, on) => ((on === undefined ? !classes.has(c) : on) ? classes.add(c) : classes.delete(c)),
  } };
};
els.ports = panel();
els.alarms = panel();
els.stp = panel();
els.journal = panel();
// the ports' timers, counted
const live = new Set();
let next = 1;
setInterval = (fn) => { const id = next++; if (fn === refreshPorts) live.add(id); return id; };
clearInterval = (id) => { live.delete(id); };
// the server answers when it is told to, and every request is counted
let waiting = [];
const requests = [];
api = async (path, options) => {
  requests.push([(options && options.method) || "GET", path]);
  await new Promise((resolve) => waiting.push(resolve));
  return { ok: false, json: async () => ({ switch: "s", ports: [], columns: {}, limits: {}, error: "no" }) };
};
const answer = async () => { const now = waiting; waiting = []; now.forEach((r) => r()); await new Promise((r) => setTimeout(r, 0)); };

// open, close before the answer
let first = openPorts("10.0.0.10");
closePorts();
await answer(); await first;
out.closedFirst = { open: !els.ports.classList.contains("hidden"), timers: live.size };

// A, then B, the answers in that order and the other
for (const order of ["ab", "ba"]) {
  const a = openPorts("10.0.0.10");
  const b = openPorts("10.0.0.21");
  await answer(); await Promise.all([a, b]);
  out["twoSwitches_" + order] = { open: !els.ports.classList.contains("hidden"), timers: live.size, ip: portsIp };
  closePorts();
}

// alarms asked for, then a card opened before the answer
const alarms = toggleAlarms();
showDetails("sw:10.0.0.10");
await answer(); await alarms;
out.alarmsAfterACard = !els.alarms.classList.contains("hidden");

// a double press: one request
requests.length = 0;
const node = topology.pseudo_switches[0].id;
const one = saveNote(node);
const two = saveNote(node);
await answer(); await Promise.all([one, two]);
out.notePuts = requests.filter(([method]) => method === "PUT").length;

// busy(): one run, the buttons held, everything back after a failure
let runs = 0;
let finish;
const button = { disabled: false };
const run = busy("k", () => { runs++; return new Promise((r) => { finish = r; }); }, [button]);
const again = await busy("k", () => { runs++; }, [button]);
const held = button.disabled;
finish("done");
out.busy = { runs, held, again: again === undefined, result: await run, after: button.disabled };
try { await busy("k", () => { throw new Error("no"); }, [button]); } catch (e) { /* the caller's */ }
let ranAfter = false;
await busy("k", () => { ranAfter = true; });
out.busyAfterFailure = { ran: ranAfter, button: button.disabled };

// fmt(): every place, the value as given
I18N.en.zzTwice = "{n} and {n}: {name}";
out.fmt = [
  fmt("zzTwice", { n: 2, name: "$&" }),
  fmt("zzTwice", { n: "$1", name: "$'x$`" }),
  fmt("zzTwice", { n: null, name: undefined }),
];
__done(out);
})();
"""


@unittest.skipUnless(NODE, "needs node to run the page's JavaScript")
class PageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from web_source import demo_topology
        cls.out = run_page(PAGE, {"topology": demo_topology()})

    def test_a_late_answer_opens_nothing_closed(self):
        self.assertEqual(self.out["closedFirst"], {"open": False, "timers": 0})

    def test_two_switches_one_timer(self):
        for order in ("ab", "ba"):
            with self.subTest(order):
                self.assertEqual(self.out["twoSwitches_" + order],
                                 {"open": True, "timers": 1, "ip": "10.0.0.21"})

    def test_a_card_opened_meanwhile_stays(self):
        self.assertFalse(self.out["alarmsAfterACard"])

    def test_a_double_press_sends_one_request(self):
        self.assertEqual(self.out["notePuts"], 1)
        self.assertEqual(self.out["busy"],
                         {"runs": 1, "held": True, "again": True, "result": "done", "after": False})
        self.assertEqual(self.out["busyAfterFailure"], {"ran": True, "button": False})

    def test_fmt(self):
        self.assertEqual(self.out["fmt"], ["2 and 2: $&", "$1 and $1: $'x$`", " and : "])


class SourceTest(unittest.TestCase):
    def test_every_button_has_a_type(self):
        buttons = re.findall(r"<button\b[^>]*>", INDEX)
        self.assertGreater(len(buttons), 15)
        self.assertEqual([b for b in buttons if "type=" not in b], [])

    def test_the_writers_go_through_busy(self):
        for name in ("saveNote", "moveNoteHere", "storeByHand", "resetLayout", "userAction",
                     "removeOwnKey", "toggleMonitor", "clearAlarm", "rememberNeighbours"):
            with self.subTest(name):
                self.assertIn("return busy(", function(name))

    def test_the_ports_timer_goes_before_the_request(self):
        opened = function("openPorts")
        self.assertLess(opened.index("clearInterval(portsTimer);"), opened.index("await refreshPorts()"))
        self.assertIn("if (!stillAsked() || portsIp !== ip) return;", opened)
        self.assertIn("if (portsIp !== ip) return;", function("refreshPorts"))
        for name in ("toggleAlarms", "toggleStp", "toggleJournal"):
            self.assertIn("if (!stillAsked()) return;", function(name))

    def test_phone_fields_and_names(self):
        from web_source import STYLE
        self.assertIn("input, select, textarea { font-size: 16px; }", STYLE)
        self.assertIn("-webkit-backdrop-filter: blur(3px);", STYLE)
        self.assertIn('<th><span class="sr-only" data-i18n="colState"></span></th>', INDEX)
        self.assertIn('s = s.replaceAll("{" + name + "}", () =>', function("fmt"))


if __name__ == "__main__":
    unittest.main()
