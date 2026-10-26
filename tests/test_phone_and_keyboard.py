"""A page that stays put on a phone, and the lists from the keyboard
(v0.8.2).

The page must not scroll as a whole: on a phone 100vh is taller than
what is seen, and a finger on the header moved everything. What is
longer than the screen scrolls inside itself. The lists are the
keyboard's way onto the map, a canvas: a row is a button, each list
takes one Tab, and the arrows go through the rows the search left —
run through `node` on the page's own functions (skipped without it)."""

import re
import unittest

from web_source import APP, INDEX, NODE, STYLE, function, run_js


def rule(selector):
    """The declarations of the first rule whose selector list is exactly `selector`"""
    found = re.search(r"(?:^|\n)" + re.escape(selector) + r" \{([^}]*)\}", STYLE)
    return found.group(1) if found else ""


class PhoneTest(unittest.TestCase):
    def test_the_page_never_scrolls(self):
        body = rule("body")
        self.assertIn("height: 100vh;", body)
        self.assertIn("height: 100dvh;", body)
        # dvh last: a browser that knows it takes it
        self.assertLess(body.index("100vh"), body.index("100dvh"))
        page = rule("html, body")
        self.assertIn("overflow: hidden;", page)
        self.assertIn("overscroll-behavior: none;", page)
        # zooming with two fingers stays
        viewport = re.search(r'<meta name="viewport" content="([^"]*)">', INDEX).group(1)
        self.assertEqual(viewport, "width=device-width, initial-scale=1")

    def test_what_is_longer_scrolls_inside_itself(self):
        for selector in (".panel", ".panel-scroll", "#sidebar", ".context-menu",
                         ".gate", ".gate-card"):
            with self.subTest(selector):
                self.assertIn("overscroll-behavior: contain;", rule(selector))
        self.assertIn("overflow-y: auto;", rule(".context-menu"))
        # a menu under its button is no taller than the room under it
        self.assertIn("menuUnder(els.moreMenu", function("openMoreMenu"))
        self.assertIn("window.innerHeight - top - 8", function("menuUnder"))

    def test_tap_areas_only_reach_where_there_is_room(self):
        self.assertIn("inset: var(--tap, 0);", STYLE)
        areas = function("tapAreas")
        self.assertIn('matchMedia("(max-width: 720px)")', areas)
        self.assertIn("const TAP_SIZE = 44;", APP)
        self.assertIn("tapAreas();", function("fitHeader"))


class ListSourceTest(unittest.TestCase):
    def test_a_row_is_a_button(self):
        row = function("li")
        self.assertIn('document.createElement("button")', row)
        self.assertIn('button.type = "button";', row)
        self.assertIn("button.tabIndex = -1;", row)
        self.assertIn('button.addEventListener("click", onClick);', row)
        self.assertIn(".device-list .li-btn:focus-visible", STYLE)

    def test_the_search_leads_to_the_first_row_found(self):
        self.assertIn("listTabStop(list);", function("applySearchFilter"))
        self.assertIn('els.sidebar.addEventListener("keydown", listKeys);', APP)
        # a refresh keeps the focus on its node
        self.assertIn("row.querySelector(\".li-btn\").focus({ preventScroll: true });",
                      function("renderSidebar"))


# A list of six rows, the second and the fifth hidden by the search, in
# objects shaped like the DOM the two functions use
LIST = r"""(() => {
  let focused = null;
  const list = { children: [] };
  list.querySelectorAll = () => list.children.map((item) => item.button);
  [0, 1, 2, 3, 4, 5].forEach((i) => {
    const item = { i, hidden: i === 1 || i === 4 };
    item.classList = { contains: (name) => name === "hidden-by-search" && item.hidden };
    const button = { tabIndex: -1, parentElement: item };
    button.closest = (selector) => (selector === "ul" ? list : button);
    button.focus = () => { focused = i; };
    item.button = button;
    item.querySelector = () => button;
    list.children.push(item);
  });
  const stops = () => list.children.filter((item) => item.button.tabIndex === 0).map((item) => item.i);
  const press = (key) => {
    let prevented = false;
    listKeys({ key, target: list.children[focused].button, preventDefault: () => { prevented = true; } });
    return [key, focused, prevented, stops()];
  };
  listTabStop(list);
  const first = stops();
  focused = 0;
  return { first, keys: [press("ArrowDown"), press("ArrowDown"), press("ArrowDown"),
                         press("ArrowDown"), press("Home"), press("End"), press("ArrowUp"),
                         press("a")] };
})()"""


@unittest.skipUnless(NODE, "needs node to run the page's JavaScript")
class ListKeysTest(unittest.TestCase):
    def test_arrows_home_and_end_over_the_rows_left(self):
        got = run_js(["listKeys", "listTabStop"], LIST)
        # Tab stops at the first row the search left
        self.assertEqual(got["first"], [0])
        self.assertEqual(got["keys"], [
            ["ArrowDown", 2, True, [2]],
            ["ArrowDown", 3, True, [3]],
            ["ArrowDown", 5, True, [5]],
            ["ArrowDown", 5, True, [5]],   # the last row stays the last
            ["Home", 0, True, [0]],
            ["End", 5, True, [5]],
            ["ArrowUp", 3, True, [3]],
            ["a", 3, False, [3]],          # any other key is the page's
        ])


if __name__ == "__main__":
    unittest.main()
