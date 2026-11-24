"""A phone: two fingers zoom the map and nothing else (v0.8.8).

A pinch anywhere zoomed the whole page, and with the tab bar fixed to
the bottom (v0.8.6) that pushed the header, the tabs and the card past
the edge of the screen, out of reach until an exact pinch back. On a
phone the viewport now says maximum-scale=1 and touch-action keeps
panning only, the map taking every gesture itself; wider than 720 px
nothing changes. The zoom's one use — larger text — is "Text size" in
More: the root font, 15 px × 100 / 115 / 130 %, kept in the browser.

Run with:  python -m unittest discover -s tests
"""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from web_source import INDEX, NODE, STYLE, function, run_page, strings  # noqa: E402


def phone_blocks():
    """The bodies of the style's (max-width: 720px) blocks"""
    blocks = []
    for match in re.finditer(r"@media \(max-width: 720px\) \{", STYLE):
        depth, at = 1, match.end()
        while depth:
            char = STYLE[at]
            depth += {"{": 1, "}": -1}.get(char, 0)
            at += 1
        blocks.append(STYLE[match.end():at - 1])
    return blocks


class SourceTest(unittest.TestCase):
    def test_touch_action_only_on_a_phone(self):
        phone = "\n".join(phone_blocks())
        self.assertIn("html, body, body * { touch-action: pan-x pan-y; }", phone)
        self.assertIn("#network, #network * { touch-action: none; }", phone)
        # nowhere else: a computer's touch screen zooms as before
        self.assertEqual(STYLE.count("touch-action:"), phone.count("touch-action:"))

    def test_the_page_starts_without_a_limit(self):
        self.assertIn('content="width=device-width, initial-scale=1"', INDEX)
        self.assertNotIn("maximum-scale", INDEX)

    def test_fields_stay_at_16_px_or_more(self):
        phone = "\n".join(phone_blocks())
        self.assertIn("input, select, textarea { font-size: max(16px, 1rem); }", phone)
        self.assertNotRegex(phone, r"input[^{]*\{[^}]*font-size: 16px;")

    def test_the_row_comes_after_arranging(self):
        view = function("renderMoreView")
        self.assertLess(view.index('key: "arrange"'), view.index('key: "textSize"'))
        self.assertLess(view.index('key: "textSize"'), view.index("more-sep"))

    def test_the_words(self):
        for lang in ("en", "ru"):
            table = strings(lang)
            self.assertTrue(table.get("textSize"), lang)
            self.assertIn("{n}", table.get("textSizeValue", ""), lang)
        self.assertEqual(strings("ru")["textSize"], "Размер текста")


PAGE = r"""
const meta = { attrs: { content: "width=device-width, initial-scale=1" },
               setAttribute(k, v) { this.attrs[k] = String(v); } };
const root = { style: { fontSize: "" } };
const pageDocument = document;
document = new Proxy(pageDocument, {
  get(target, key) {
    if (key === "querySelector") {
      return (selector) => (String(selector).includes("viewport") ? meta : target.querySelector(selector));
    }
    if (key === "documentElement") return root;
    return target[key];
  },
});
let phone = false;
__define("matchMedia", (query) => ({
  matches: phone, media: query, addEventListener() {}, removeEventListener() {},
}));
const out = {};
applyPhoneScale();
out.computer = [meta.attrs.content, root.style.fontSize];
phone = true;
applyPhoneScale();
out.phone = [meta.attrs.content, root.style.fontSize];
setTextSize(130);
out.at130 = [root.style.fontSize, localStorage.getItem("moonlan-text-size")];
// a reload: what the page reads at its start
textSize = savedTextSize();
root.style.fontSize = "";
applyPhoneScale();
out.reloaded = root.style.fontSize;
setTextSize(115);
out.at115 = root.style.fontSize;
setTextSize(50);
out.refused = textSize;
setTextSize(100);
out.at100 = root.style.fontSize;
// a computer: the size is kept, and not used
setTextSize(130);
phone = false;
applyPhoneScale();
out.wide = [meta.attrs.content, root.style.fontSize, localStorage.getItem("moonlan-text-size")];
localStorage.setItem("moonlan-text-size", "999");
out.nonsense = savedTextSize();
out.value = { en: (lang = "en", fmt("textSizeValue", { n: 115 })),
              ru: (lang = "ru", fmt("textSizeValue", { n: 115 })) };
__done(out);
"""


@unittest.skipUnless(NODE, "needs node to run the page's JavaScript")
class PageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_page(PAGE)

    def test_the_viewport(self):
        self.assertEqual(self.out["computer"], ["width=device-width, initial-scale=1", ""])
        self.assertEqual(self.out["phone"][0],
                         "width=device-width, initial-scale=1, maximum-scale=1")
        self.assertEqual(self.out["wide"][0], "width=device-width, initial-scale=1")

    def test_the_text_size(self):
        self.assertEqual(self.out["phone"][1], "")         # 100 %: the style's 15 px
        self.assertEqual(self.out["at130"], ["19.5px", "130"])
        self.assertEqual(self.out["reloaded"], "19.5px")
        self.assertEqual(self.out["at115"], "17.25px")
        self.assertEqual(self.out["refused"], 115)
        self.assertEqual(self.out["at100"], "")
        self.assertEqual(self.out["wide"][1:], ["", "130"])
        self.assertEqual(self.out["nonsense"], 100)
        self.assertEqual(self.out["value"], {"en": "115%", "ru": "115 %"})


if __name__ == "__main__":
    unittest.main()
