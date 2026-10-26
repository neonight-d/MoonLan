"""The map exported (v0.8.1): the model, Draw.io and SVG — and the
fonts of named nodes, which the export reads like the map.

The page builds all of it in the browser, from the layout on the
screen. Here it is the page's own code run through `node` (GitHub's
runners have it; the cases are skipped without) on the demo network:
one scan of it in a process of its own, the map built by
buildGraphData, laid out on a grid by a stand-in for vis. The same
script was run in a browser's Web Worker."""

import html
import re
import unittest
import xml.etree.ElementTree as ET

from web_source import APP, INDEX, NODE, demo_topology, function, run_page, strings

# what a person might type as a name: markup, an ampersand, both quotes
NAME = "<b>x</b> & \"y\" 'z'"
NOTE = "line one\nline two <i>"
SVG = "{http://www.w3.org/2000/svg}"

PAGE = r"""
lang = "en";
topology = __DATA.topology;
const out = {};

// a link the MAC tables could not order: none in the demo, so one here
topology.links.find((l) => l.source === "fdb" && !l.lag).order_unknown = true;
// a minute's load on every link, as the counters cycle leaves it
for (const link of topology.links) link.load = { in_mbps: 12.5, out_mbps: 0.4 };

// a name on every kind of node that can have one
const kinds = {
  switch: [topology.switches[0], (x) => "sw:" + x.ip],
  bridge: [topology.bridges[0], (x) => x.id],
  external: [topology.external_networks[0], (x) => x.id],
  pseudo: [topology.pseudo_switches.find((x) => x.note) || topology.pseudo_switches[0], (x) => x.id],
  offline: [topology.offline_groups.find((x) => x.note) || topology.offline_groups[0], (x) => x.id],
  host: [topology.hosts.find((x) => x.switch && !x.merged_into), (x) => "host:" + x.mac],
};
for (const [kind, [item]] of Object.entries(kinds)) {
  if (!item.note) item.note = { label: "Named " + kind, note: "" };
}
topology.bridges[0].note = { label: __DATA.name, note: __DATA.note };

const { nodes, edges } = buildGraphData();

// the font of each: markup read, and the same size selected or not
out.fonts = {};
for (const [kind, [item, idOf]] of Object.entries(kinds)) {
  const id = idOf(item);
  const node = nodes.find((n) => n.id === id);
  const before = nodeFont(id);
  selectedNodeId = id;
  const during = nodeFont(id);
  selectedNodeId = null;
  out.fonts[kind] = {
    drawn: !!node, multi: node && node.font.multi,
    size: [before.size || null, during.size || null],
    bold: [before.bold && before.bold.size, during.bold && during.bold.size],
  };
}

// vis, standing in: the nodes on a grid, straight lines, fixed sizes
const positions = {};
nodes.forEach((n, i) => { positions[n.id] = { x: (i % 12) * 170, y: Math.floor(i / 12) * 130 }; });
nodesDs = { get: () => nodes };
edgesDs = { get: () => edges };
network = {
  getPositions: () => positions,
  body: {
    nodes: Object.fromEntries(nodes.map((n) => [n.id, {
      x: positions[n.id].x, y: positions[n.id].y,
      options: { shape: n.shape || "dot" },
      shape: { width: n.shape === "box" ? 90 : 16, height: n.shape === "box" ? 40 : 16, resize() {} },
      labelModule: { getTextSize: () => ({ width: 70, height: 30 }) },
    }])),
    edges: {},
  },
  canvas: { frame: { canvas: { getContext: () => ({}) } } },
};

out.infrastructure = exportModel({ contents: "infrastructure", ports: true });
out.screenLinks = edges.filter((e) => e.id.startsWith("link:")).map((e) => e.label);
out.all = exportModel({ contents: "all", ports: true }).nodes.map((n) => n.id);
out.drawnHosts = nodes.filter((n) => n.id.startsWith("host:")).length;
portNumbersOnly = true;
out.numbersOnly = exportModel({ contents: "infrastructure", ports: true }).edges.map((e) => e.ports);
portNumbersOnly = false;
out.noPorts = exportModel({ contents: "infrastructure", ports: false }).edges.map((e) => e.ports);

out.drawio = drawioXml(out.infrastructure, "MoonLan — network map");
out.svg = schemaSvg(out.infrastructure, {
  measure: (text, size) => String(text).length * size * 0.6,
  legend: { title: t("legendTitle"), items: exportLegend(out.infrastructure) },
});
out.legend = exportLegend(out.infrastructure).map((item) => item.text);

lang = "ru";
out.devicesRu = [1, 3, 5, 21].map((n) => plural("exportDevices", n));
lang = "en";
out.devicesEn = [1, 3].map((n) => plural("exportDevices", n));
__done(out);
"""


@unittest.skipUnless(NODE, "needs node to run the page's JavaScript")
class ExportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.topology = demo_topology()
        cls.out = run_page(PAGE, {"topology": cls.topology, "name": NAME, "note": NOTE})
        cls.model = cls.out["infrastructure"]
        cls.bridge = cls.topology["bridges"][0]["id"]

    def test_fonts_of_named_nodes(self):
        for kind, font in self.out["fonts"].items():
            with self.subTest(kind):
                self.assertTrue(font["drawn"])
                self.assertEqual(font["multi"], "html")
                # brighter when selected, never bigger or smaller
                self.assertEqual(font["size"][0], font["size"][1])
                self.assertEqual(font["bold"][0], font["bold"][1])

    def test_infrastructure(self):
        ids = [node["id"] for node in self.model["nodes"]]
        self.assertFalse([i for i in ids if i.startswith("host:")])
        self.assertIn("sw:10.0.0.10", ids)
        core = next(n for n in self.model["nodes"] if n["id"] == "sw:10.0.0.10")
        self.assertRegex(core["lines"][-1], r"^\d+ devices$")
        # no line to a device that is not there
        self.assertFalse([e for e in self.model["edges"] if e["to"].startswith("host:")])
        # the offline room keeps its name (task 1) and the export says it
        offline = [n for n in self.model["nodes"] if n["kind"] == "offline" and n["name"]]
        self.assertEqual([n["name"] for n in offline], ["Room 305"])

    def test_everything(self):
        hosts = [i for i in self.out["all"] if i.startswith("host:")]
        self.assertTrue(hosts)
        self.assertEqual(len(hosts), self.out["drawnHosts"])

    def test_what_the_lines_mean(self):
        meanings = {tag for edge in self.model["edges"] for tag in edge["semantic"]}
        for tag in ("lldp", "fdb", "blocking", "lacp", "guess", "parallel"):
            self.assertIn(tag, meanings)

    def test_no_load_on_paper(self):
        # the map shows the minute's load …
        self.assertTrue(all("↓" in label for label in self.out["screenLinks"]))
        # … the paper does not: it is untrue an hour later
        labels = [e["label"] for e in self.model["edges"]]
        self.assertFalse([label for label in labels if "↓" in label or "↑" in label])
        self.assertIn("LACP 2×1 Gbit/s", labels)
        # what is not load stays: STP blocking
        self.assertTrue([label for label in labels if label.endswith("BLOCKING")])
        self.assertNotRegex(self.out["drawio"] + self.out["svg"], "[↓↑]")

    def test_port_numbers(self):
        named = [text for e in self.model["edges"] for text in e["ports"].values()]
        numbers = [text for ports in self.out["numbersOnly"] for text in ports.values()]
        self.assertEqual(len(named), len(numbers))
        # "numbers only" shortens the ports of a switch whose ports share
        # a beginning ("Gi0/24" → "24"); every other name is as it was
        changed = [(a, b) for a, b in zip(named, numbers) if a != b]
        self.assertTrue(changed)
        for full, short in changed:
            self.assertRegex(short, r"^\d+([,+]\d+)?$")
            self.assertTrue(re.search("[A-Za-z]", full), full)
        self.assertEqual([p for p in self.out["noPorts"] if p], [])

    def test_drawio(self):
        root = ET.fromstring(self.out["drawio"].encode("utf-8"))
        objects = root.findall(".//object")
        cells = root.findall(".//mxCell")
        ids = [o.get("id") for o in objects] + [c.get("id") for c in cells if c.get("id")]
        self.assertEqual(len(ids), len(set(ids)))
        nodes = {o.get("id") for o in objects}
        lines = [c for c in cells if c.get("edge") == "1"]
        self.assertEqual(len(lines), len(self.model["edges"]))
        for line in lines:
            self.assertIn(line.get("source"), nodes)
            self.assertIn(line.get("target"), nodes)
        # the name, through XML and then HTML, is the name again
        bridge = next(o for o in objects if o.get("moonlan_id") == self.bridge)
        bold = re.match(r"<b>(.*?)</b>", bridge.get("label")).group(1)
        self.assertEqual(html.unescape(bold), NAME)
        self.assertEqual(bridge.get("note"), NOTE)
        self.assertEqual(bridge.get("tooltip"), NOTE)

    def test_svg(self):
        root = ET.fromstring(self.out["svg"].encode("utf-8"))
        texts = [t.text for t in root.iter(SVG + "text")]
        self.assertIn(NAME, texts)
        self.assertIn("Legend", texts)
        # the legend has what the drawing has
        self.assertIn("STP blocking: a standby path", self.out["legend"])
        self.assertNotIn("device", self.out["legend"])

    def test_devices_counted_in_both_languages(self):
        self.assertEqual(self.out["devicesRu"],
                         ["1 устройство", "3 устройства", "5 устройств", "21 устройство"])
        self.assertEqual(self.out["devicesEn"], ["1 device", "3 devices"])


class WindowTest(unittest.TestCase):
    """The export window, as the page's source has it"""

    def test_for_every_role(self):
        items = function("moreItems")
        line = next(l for l in items.splitlines() if '"exportMap"' in l)
        self.assertIn('group: "network"', line)
        self.assertNotIn("disabled", line)
        # About: the last item, a group of its own
        self.assertTrue(items.rstrip().endswith(
            '{ key: "about", group: "about", label: t("menuAbout"), run: openAbout },\n  ];\n}'))

    def test_everything_on_paper_is_said_to_be_small(self):
        dialog = function("renderExportDialog")
        self.assertIn('t("exportAllPaperHint")', dialog)
        self.assertIn('format !== "pdf" || form.elements["export-contents"].value !== "all"', dialog)
        self.assertEqual(strings("ru")["exportAllPaperHint"],
                         "Для экрана: на листе подписи устройств будут нечитаемо мелкими.")

    def test_settling_is_what_the_eye_sees(self):
        settling = function("layoutSettling")
        self.assertNotIn("stabilized", settling)
        self.assertIn("performance.now() - layoutMovedAt < LAYOUT_STILL_MS", settling)
        self.assertIn("setInterval(watchLayoutMotion, 1000);", APP)

    def test_nothing_in_the_journal(self):
        for name in ("runExport", "printSchema", "openExportDialog"):
            self.assertNotIn("/api/", function(name).replace('api("/api/health")', ""))

    def test_the_sheet_is_printed_alone(self):
        self.assertIn('<div id="print-sheet"></div>', INDEX)
        self.assertIn("@page { size: ${pageW}mm ${pageH}mm;", function("printSchema"))

    def test_words(self):
        for lang in ("en", "ru"):
            texts = strings(lang)
            for key in ("menuExportMap", "exportSettling", "exportPdfHint", "legendTitle",
                        "sheetNotesTitle", "exportDevices_other", "menuAbout", "aboutCopy",
                        "aboutFido2Missing"):
                self.assertIn(key, texts, f"{lang}: {key}")
        self.assertEqual(strings("ru")["exportSettling"],
                         "Карта ещё раскладывается — схема будет такой, как на экране сейчас.")
        self.assertEqual(strings("ru")["exportPdfHint"],
                         "В диалоге печати выберите «Сохранить как PDF».")
        self.assertIn("{n}", strings("ru")["exportDevices_many"])
        # the page that was to save files is not the page that loads code
        self.assertNotIn("eval(", APP)


if __name__ == "__main__":
    unittest.main()
