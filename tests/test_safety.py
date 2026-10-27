"""What comes from the network is text (v0.8.3).

A switch's sysName, an LLDP neighbour's name, a DNS name — anything the
network says — went into the cards as markup where an escape had been
left out. The cards are html`…` now, escaped by default; every answer
carries a Content-Security-Policy; vis-network is served by MoonLan;
and the map survives a vis without the internals it reaches for. The
cards and the stand-in vis are run through `node` on the page's own
code (skipped without it)."""

import base64
import hashlib
import re
import unittest
from html.parser import HTMLParser
from pathlib import Path

import service_fixture  # noqa: F401  (sets MOONLAN_CONFIG first)
from asgi_client import call
from moonlan import https, server
from web_source import APP, INDEX, NODE, demo_topology, run_page

ROOT = Path(__file__).resolve().parent.parent
EVIL = "<img src=x onerror=alert(1)>\"'&"
VIS = ROOT / "web" / "vendor" / "vis-network-9.1.9"


class Tags(HTMLParser):
    """The elements and attributes a string of HTML makes, and its text"""

    def __init__(self, markup):
        super().__init__(convert_charrefs=True)
        self.tags, self.attrs, self.text = [], [], []
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attrs += [name for name, _ in attrs]

    def handle_data(self, data):
        self.text.append(data)


HOSTILE = r"""
(() => {
lang = "en";
topology = __DATA.topology;
const EVIL = __DATA.evil;
// every string the network gives: names, descriptions, ports, makers
const fields = new Set(["name", "descr", "model", "sys_name", "sys_desc", "dns_name", "vendor",
  "port", "a_port", "b_port", "remote_port", "via_name", "port_id", "chassis_id", "via_name"]);
const evilize = (o) => {
  if (Array.isArray(o)) { o.forEach(evilize); return; }
  if (!o || typeof o !== "object") return;
  for (const key of Object.keys(o)) {
    if (fields.has(key) && typeof o[key] === "string") o[key] = EVIL;
    else if ((key === "a_members" || key === "b_members") && Array.isArray(o[key])) o[key] = o[key].map(() => EVIL);
    else evilize(o[key]);
  }
};
evilize(topology);
for (const host of topology.hosts) host.vendor_status = "found";
const { nodes } = buildGraphData();
nodesDs = { get: (id) => (id === undefined ? nodes : nodes.find((n) => n.id === id) || null) };
let html = "";
els.detailsBody = {
  set innerHTML(value) { html = value; }, get innerHTML() { return html; },
  querySelector: () => null, querySelectorAll: () => [],
};
els.detailsTitle = { replaceChildren() {} };
const cards = {};
for (const prefix of [...STRUCTURE_PREFIXES, "host:"]) {
  const node = nodes.find((n) => n.id.startsWith(prefix));
  html = "";
  if (node) showDetails(node.id);
  cards[prefix] = node ? html : null;
}
html = "";
showLinkDetails(linkId(topology.links[0]));
cards.link = html;
const lag = topology.links.find((l) => l.lag && l.lag.count > 1);
html = "";
showLinkDetails(linkId(lag));
cards["link of an aggregate"] = html;
__done({ cards, structure: [...STRUCTURE_PREFIXES], groups: [...GROUP_PREFIXES] });
})();
"""


@unittest.skipUnless(NODE, "needs node to run the page's JavaScript")
class HostileNamesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_page(HOSTILE, {"topology": demo_topology(), "evil": EVIL})

    def test_every_kind_of_node_has_a_card(self):
        # a kind of node added without a card, or without one here, fails
        self.assertLessEqual(set(self.out["groups"]), set(self.out["structure"]))
        for kind, card in self.out["cards"].items():
            with self.subTest(kind):
                self.assertTrue(card, f"no card for {kind}")

    def test_a_hostile_name_is_text(self):
        for kind, card in self.out["cards"].items():
            with self.subTest(kind):
                parsed = Tags(card)
                self.assertNotIn("img", parsed.tags)
                self.assertFalse([a for a in parsed.attrs if a.startswith("on")], parsed.attrs)
                # seen as it is, characters and all
                self.assertIn(EVIL, "".join(parsed.text))


class HeadersTest(unittest.TestCase):
    def test_every_answer_says_what_may_run(self):
        for path in ("/", "/app.js", "/api/topology", "/api/health"):
            with self.subTest(path):
                answer = call(server.app, "GET", path)
                self.assertEqual(answer.header("content-security-policy"),
                                 [https.CONTENT_SECURITY_POLICY])
                self.assertEqual(answer.header("x-content-type-options"), ["nosniff"])
                self.assertEqual(answer.header("referrer-policy"), ["same-origin"])

    def test_the_policy(self):
        policy = https.CONTENT_SECURITY_POLICY
        self.assertIn("script-src 'self';", policy)
        self.assertNotIn("unsafe-eval", policy)
        self.assertNotIn("script-src 'self' 'unsafe-inline'", policy)
        for directive in ("object-src 'none'", "base-uri 'none'", "frame-ancestors 'none'",
                          "form-action 'self'", "connect-src 'self'"):
            self.assertIn(directive, policy)

    def test_no_script_from_elsewhere(self):
        self.assertEqual(re.findall(r'<script[^>]+src="(?:https?:)?//', INDEX), [])
        self.assertIn('<script src="vendor/vis-network-9.1.9/vis-network.min.js"></script>', INDEX)
        # nothing inline either: the policy would not run it
        self.assertEqual(re.findall(r"<script>(?!</script>)", INDEX), [])


class VendorTest(unittest.TestCase):
    def test_the_file_is_the_one_published(self):
        source = (VIS / "SOURCE").read_text(encoding="utf-8")
        sri = re.search(r"sha512-(\S+)", source).group(1)
        digest = base64.b64encode(hashlib.sha512((VIS / "vis-network.min.js").read_bytes()).digest())
        self.assertEqual(digest.decode(), sri)
        for licence in ("LICENSE-MIT", "LICENSE-APACHE-2.0"):
            self.assertTrue((VIS / licence).is_file())

    def test_it_is_served(self):
        answer = call(server.app, "GET", "/vendor/vis-network-9.1.9/vis-network.min.js")
        self.assertEqual(answer.status, 200)
        self.assertIn("javascript", answer.header("content-type")[0])


# vis with nothing but its published API: no body, no canvas, no
# edgeType, no labelModule
BARE_VIS = r"""
(() => {
lang = "en";
topology = __DATA.topology;
const warnings = [];
console.warn = (...args) => warnings.push(args.join(" "));
class DataSet {
  constructor(items) { this.items = new Map((items || []).map((i) => [i.id, { ...i }])); }
  get(id) {
    if (id === undefined) return [...this.items.values()];
    if (Array.isArray(id)) return id.map((x) => this.items.get(x)).filter(Boolean);
    return this.items.get(id) || null;
  }
  update(items) { for (const i of [].concat(items)) this.items.set(i.id, { ...(this.items.get(i.id) || {}), ...i }); }
  add(items) { for (const i of [].concat(items)) this.items.set(i.id, { ...i }); }
  remove(ids) { for (const id of [].concat(ids)) this.items.delete(typeof id === "object" ? id.id : id); }
  clear() { this.items.clear(); }
  getIds() { return [...this.items.keys()]; }
  get length() { return this.items.size; }
  forEach(fn) { this.items.forEach((v) => fn(v)); }
  on() {}
  off() {}
}
class Network {
  constructor(container, data) { this.data = data; this.handlers = {}; this.selected = []; }
  on(event, fn) { (this.handlers[event] = this.handlers[event] || []).push(fn); }
  off() {}
  fire(event, params) { for (const fn of this.handlers[event] || []) fn(params); }
  getPositions(ids) {
    const out = {};
    this.data.nodes.get().forEach((n, i) => {
      if (!ids || ids.includes(n.id)) out[n.id] = { x: n.x ?? (i % 20) * 60, y: n.y ?? Math.floor(i / 20) * 60 };
    });
    return out;
  }
  getScale() { return 1; }
  getViewPosition() { return { x: 0, y: 0 }; }
  getBoundingBox() { return { left: -20, right: 20, top: -10, bottom: 10 }; }
  getConnectedEdges(id) { return this.data.edges.get().filter((e) => e.from === id || e.to === id).map((e) => e.id); }
  getNodeAt() { return undefined; }
  getSelectedNodes() { return this.selected; }
  getSelectedEdges() { return []; }
  selectNodes(ids) { this.selected = ids; }
  unselectAll() { this.selected = []; }
  DOMtoCanvas(p) { return p; }
  canvasToDOM(p) { return p; }
  moveTo() {}
  focus() {}
  redraw() {}
  setOptions() {}
  setSize() {}
  stabilize() {}
}
vis = { DataSet, Network };
const errors = [];
const attempt = (what, fn) => { try { fn(); } catch (e) { errors.push(what + ": " + e); } };
attempt("drawing the map", () => renderGraph());
const id = "sw:10.0.0.10";
const pointer = { DOM: { x: 0, y: 0 }, canvas: { x: 0, y: 0 } };
const event = { srcEvent: {}, preventDefault() {}, clientX: 0, clientY: 0 };
attempt("a click", () => network.fire("click", { nodes: [id], edges: [], event, pointer }));
attempt("a click on a line", () => network.fire("click", { nodes: [], edges: [edgesDs.getIds().find((e) => e.startsWith("link:"))], event, pointer }));
attempt("a drag", () => {
  network.fire("dragStart", { nodes: [id], event, pointer });
  network.fire("dragging", { nodes: [id], event, pointer });
  network.fire("dragEnd", { nodes: [id], event, pointer });
});
attempt("a long press", () => network.fire("hold", { nodes: [id], edges: [], event, pointer }));
attempt("a hover", () => network.fire("hoverNode", { node: id }));
setPortLabelMode("all");
attempt("drawing", () => { network.fire("beforeDrawing", __stub()); network.fire("afterDrawing", __stub()); });
attempt("an export", () => exportModel({ contents: "all", ports: true }));
__done({ errors, internals: visInternals, warnings, nodes: nodesDs.length });
})();
"""


@unittest.skipUnless(NODE, "needs node to run the page's JavaScript")
class BareVisTest(unittest.TestCase):
    def test_the_map_works_without_vis_internals(self):
        out = run_page(BARE_VIS, {"topology": demo_topology()})
        self.assertGreater(out["nodes"], 0)
        self.assertEqual(out["errors"], [])
        # each feature off, and said once
        self.assertEqual(set(out["internals"].values()), {"warned"})
        said = [w for w in out["warnings"] if "vis-network has no" in w]
        self.assertEqual(len(said), len(out["internals"]))

    def test_every_internal_is_behind_its_check(self):
        for name in ("curves", "curvePoints", "hold", "labels", "geometry", "fixed"):
            self.assertRegex(APP, rf"visInternals\.{name} [!=]== true")
        self.assertIn("checkVisInternals(network);", APP)


if __name__ == "__main__":
    unittest.main()
