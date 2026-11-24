"""Icons in More on a phone (v0.8.7).

The mockup of v0.8.6 had an icon at every row of More; the text of that
TZ did not, so the rows went without. Now each one has its own — the STP
tree, Arranging, every item of moreItems(), Sign out — from one table
beside moreItems(): an inline 24×24 stroke SVG in the header's style,
no font, no CDN. The Actions menu on a computer stays as it was.

An item added to moreItems() without an icon fails here.

Run with:  python -m unittest discover -s tests
"""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from web_source import APP, NODE, STYLE, function, run_page  # noqa: E402

TABLE = APP[APP.index("const MORE_ICONS = {"):]
TABLE = TABLE[:TABLE.index("\n};\n")]
ICON_KEYS = set(re.findall(r"^  (\w+): \[", TABLE, re.M))
# the rows of More that are not moreItems()
OWN_ROWS = {"stp", "arrange", "textSize", "logout"}


class SourceTest(unittest.TestCase):
    def test_every_item_of_more_items_has_an_icon(self):
        keys = set(re.findall(r'key: "(\w+)"', function("moreItems")))
        self.assertGreaterEqual(len(keys), 9)
        self.assertEqual(sorted(keys - ICON_KEYS), [], "items of moreItems() without an icon")

    def test_the_rows_of_more_itself(self):
        view = function("renderMoreView")
        for key in OWN_ROWS:
            self.assertIn(f'key: "{key}"', view)
        self.assertEqual(sorted(OWN_ROWS - ICON_KEYS), [])

    def test_no_icon_for_nothing(self):
        keys = set(re.findall(r'key: "(\w+)"', function("moreItems"))) | OWN_ROWS
        self.assertEqual(sorted(ICON_KEYS - keys), [])

    def test_every_row_draws_its_icon(self):
        self.assertIn("moreIcon(item.key)", function("moreRow"))
        icon = function("moreIcon")
        self.assertIn('icon.setAttribute("class", "icon more-icon")', icon)
        self.assertIn('icon.setAttribute("aria-hidden", "true")', icon)
        self.assertIn('createElementNS(ns, "path")', icon)
        self.assertNotIn("innerHTML", icon)
        # the Actions menu on a computer: no icons, as before
        self.assertNotIn("moreIcon", function("menuButtons"))
        self.assertNotIn("moreIcon", function("openMoreMenu"))

    def test_the_colours(self):
        self.assertIn(".more-row .more-icon { flex: 0 0 auto; width: 20px; height: 20px; "
                      "stroke-width: 2; color: var(--text-dim); }", STYLE)
        self.assertIn(".more-row.off .more-icon,\n  .more-row.danger .more-icon { color: inherit; }",
                      STYLE)
        # stroke: currentColor, from the header's .icon
        rule = STYLE[STYLE.index("\n.icon {"):]
        self.assertIn("stroke: currentColor;", rule[:rule.index("}")])


PAGE = r"""
// an element that keeps what is put into it
const node = (tag) => ({
  tag, attrs: {}, children: [],
  setAttribute(k, v) { this.attrs[k] = String(v); },
  append(...items) { this.children.push(...items); },
});
document = { createElementNS: (ns, tag) => node(tag) };
const keys = moreItems().map((item) => item.key).concat(["stp", "arrange", "logout"]);
__done(Object.fromEntries(keys.map((key) => {
  const icon = moreIcon(key);
  return [key, {
    tag: icon.tag, cls: icon.attrs.class, hidden: icon.attrs["aria-hidden"],
    paths: icon.children.filter((c) => c.tag === "path" && c.attrs.d).length,
  }];
})));
"""


@unittest.skipUnless(NODE, "needs node to run the page's JavaScript")
class PageTest(unittest.TestCase):
    def test_each_row_gets_a_drawn_icon(self):
        out = run_page(PAGE)
        self.assertGreaterEqual(len(out), 12)
        for key, icon in out.items():
            with self.subTest(key=key):
                self.assertEqual(icon["tag"], "svg")
                self.assertEqual(icon["cls"], "icon more-icon")
                self.assertEqual(icon["hidden"], "true")
                self.assertGreater(icon["paths"], 0)


if __name__ == "__main__":
    unittest.main()
