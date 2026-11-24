"""A phone: one row on top, the sections at the bottom (v0.8.6).

On a Pixel 10 (412 x 924) the header took four rows — the name, the
status with RU/EN, seven buttons, the account — and the "List and
search" bar under them: 184 px, a fifth of the screen, all of it out of
the thumb's reach. At 720 px and narrower the header is now the name
and a status button, and a bar at the bottom has Map, Search, Alarms,
Journal and More. Every view or panel opened on a phone is one entry of
the browser's history, so Android's Back closes the top one. Wider than
720 px nothing of it is shown.

The behaviour is run through `node` on the page's own code (skipped
without it); the layout is held to the style.

Run with:  python -m unittest discover -s tests
"""

import re
import unittest

from web_source import APP, INDEX, NODE, STYLE, function, run_page, strings


def phone_rules():
    """The style of the phone section of v0.8.6"""
    start = STYLE.index("/* ---------- a phone: one row on top")
    return STYLE[start:]


class LayoutTest(unittest.TestCase):
    def test_at_412_the_header_is_the_name_and_the_status(self):
        phone = phone_rules()
        media = phone[phone.index("@media (max-width: 720px)"):]
        self.assertIn(".tagline, .toolbar { display: none; }", media)
        self.assertIn("header { flex-wrap: nowrap; height: 48px;", media)
        self.assertIn(".status-btn {\n    display: inline-flex;", media)
        # the RU/EN, the seven buttons and the account are inside .toolbar
        toolbar = INDEX[INDEX.index('<div class="toolbar">'):INDEX.index("</header>")]
        for element in ('id="lang-switch"', 'id="freeze-btn"', 'id="arrange-btn"',
                        'id="port-labels-btn"', 'id="alarms-btn"', 'id="stp-btn"',
                        'id="journal-btn"', 'id="more-btn"', 'id="user-chip"'):
            self.assertIn(element, toolbar)
        self.assertNotIn('id="status-btn"', toolbar)

    def test_at_1280_none_of_it(self):
        phone = phone_rules()
        self.assertIn(
            ".status-btn, .status-card, .tabbar, .map-tools, .arrange-banner, "
            ".phone-view { display: none; }",
            phone[:phone.index("@media")],
        )
        # the tablet keeps its wrapping header
        tablet = STYLE[STYLE.index("@media (max-width: 860px)"):]
        self.assertIn("header { flex-wrap: wrap; }", tablet[:tablet.index("\n}\n")])

    def test_five_tabs_with_names(self):
        nav = INDEX[INDEX.index('<nav id="tabbar"'):INDEX.index("</nav>")]
        tabs = re.findall(r'<button type="button" id="tab-(\w+)" class="tab" data-tab="(\w+)">', nav)
        self.assertEqual([t for t, _ in tabs], ["map", "search", "alarms", "journal", "more"])
        self.assertEqual(len(re.findall(r'class="tab-label" data-i18n="tab\w+"', nav)), 5)
        self.assertIn('id="tab-alarms-badge" class="badge hidden"', nav)
        self.assertIn('els.tabbar.setAttribute("aria-label", t("tabbarLabel"));',
                      function("applyStatic"))
        phone = phone_rules()
        self.assertIn("env(safe-area-inset-bottom)", phone)
        self.assertIn('.tab[aria-current="page"] { color: var(--ok);', phone)
        self.assertIn("min-height: 54px;", phone)
        self.assertIn("width: 48px;\n    height: 48px;", phone)

    def test_the_list_bar_is_gone(self):
        self.assertNotIn("sidebar-toggle", INDEX)
        self.assertNotIn("SIDEBAR_KEY", APP)
        self.assertNotIn("sidebar-toggle", STYLE)
        self.assertIn('document.querySelectorAll("header button")', function("tapAreas"))

    def test_fit_header_does_nothing_on_a_phone(self):
        self.assertIn("if (!isPhone() && window.innerWidth > 860 && crowded())",
                      function("fitHeader"))

    def test_the_card_is_a_sheet_and_the_panels_fill_the_room(self):
        phone = phone_rules()
        self.assertIn("#map-panel #details {\n    top: auto;\n    max-height: 55%;", phone)
        self.assertIn("#map-panel .panel {\n    top: 0;", phone)
        self.assertIn("bottom: calc(18px + var(--sheet, 0px));", phone)

    def test_the_words_in_both_languages(self):
        keys = ["tabbarLabel", "tabMap", "tabSearch", "tabAlarms", "tabJournal",
                "tabMore", "fitMap", "arrangeBannerOff", "statusLate", "moreStp"]
        for lang in ("en", "ru"):
            table = strings(lang)
            for key in keys:
                self.assertTrue(table.get(key), (lang, key))
        self.assertEqual(strings("ru")["tabMore"], "Ещё")
        self.assertNotIn("sidebarToggle", strings("en"))


PAGE = r"""
(async () => {
lang = "en";
const out = {};
__define("matchMedia", (query) => ({
  matches: true, media: query, addEventListener() {}, removeEventListener() {},
}));
// the browser's history: entries, and a popstate after each go()
const entries = [{ state: null }];
let at = 0;
__define("history", {
  get state() { return entries[at].state; },
  get length() { return entries.length; },
  pushState(state) { entries.splice(at + 1); entries.push({ state }); at++; },
  replaceState(state) { entries[at] = { state }; },
  go(n) {
    at = Math.max(0, Math.min(entries.length - 1, at + n));
    const state = entries[at].state;
    setTimeout(() => phonePopState({ state }), 0);
  },
  back() { this.go(-1); },
});
const tick = () => new Promise((resolve) => setTimeout(resolve, 5));

// elements that know their classes, attributes and children
function fake(tag, classes = []) {
  const set = new Set(classes);
  const attrs = {};
  const listeners = {};
  const el = {
    tag, dataset: {}, children: [], textContent: "", title: "", style: { setProperty() {} },
    offsetHeight: 300,
    classList: {
      add: (...c) => c.forEach((x) => set.add(x)), remove: (...c) => c.forEach((x) => set.delete(x)),
      contains: (c) => set.has(c),
      toggle: (c, on) => ((on === undefined ? !set.has(c) : on) ? set.add(c) : set.delete(c)),
    },
    setAttribute: (k, v) => { attrs[k] = String(v); if (k.startsWith("data-")) el.dataset[k.slice(5)] = String(v); },
    getAttribute: (k) => (k in attrs ? attrs[k] : null),
    removeAttribute: (k) => { delete attrs[k]; },
    addEventListener: (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); },
    click: async () => { for (const fn of listeners.click || []) await fn({ target: el }); },
    append: (...nodes) => { el.children.push(...nodes.filter((n) => typeof n === "object")); },
    replaceChildren: (...nodes) => { el.children = nodes; },
    querySelector: () => null,
    querySelectorAll: () => [],
    contains: () => false,
    focus() {}, getBoundingClientRect: () => ({ top: 0, bottom: 48, left: 0, right: 412 }),
  };
  Object.defineProperty(el, "className", {
    get: () => [...set].join(" "),
    set: (v) => { set.clear(); String(v).split(/\s+/).filter(Boolean).forEach((c) => set.add(c)); },
  });
  return el;
}
const stubDocument = document;
__define("document", new Proxy({
  createElement: (tag) => fake(tag),
  createElementNS: (ns, tag) => fake(tag),
  createTextNode: (text) => ({ textContent: text }),
}, { get: (target, key) => (key in target ? target[key] : stubDocument[key]) }));

for (const key of ["details", "alarms", "journal", "ports", "stp", "notesPanel", "account",
                   "users", "settings", "about", "actions"]) {
  els[key] = fake("div", ["panel", "hidden"]);
  els[key].id = key;
}
PHONE_PANELS.splice(0, PHONE_PANELS.length, els.details, els.alarms, els.journal, els.ports,
  els.stp, els.notesPanel, els.account, els.users, els.settings, els.about, els.actions);
els.main = fake("main");
els.moreView = fake("div", ["phone-view", "hidden"]);
els.moreBody = fake("div");
els.statusCard = fake("div", ["hidden"]);
els.alarmsBadge = fake("span", ["badge", "hidden"]);
els.tabAlarmsBadge = fake("span", ["badge", "hidden"]);
els.alarmsBtn = fake("button");
els.tabAlarms = fake("button");
const tabs = ["map", "search", "alarms", "journal", "more"].map((tab) => {
  const button = fake("button", ["tab"]);
  button.dataset.tab = tab;
  return button;
});
els.tabbar = fake("nav");
els.tabbar.querySelectorAll = () => tabs;

// the server: alarms, a journal, settings, what the panels ask for
api = async (path) => ({ ok: true, json: async () => (
  path.startsWith("/api/alarms") ? { active: [], cleared: [] }
  : path.startsWith("/api/journal") ? { events: [], next_before: null }
  : {}) });
loadSettings = async () => {};
renderStp = () => {};
lastStp = {};

const snap = () => ({
  stack: phoneStack.map((entry) => entry.key),
  depth: historyDepth,
  tab: tabs.filter((b) => b.getAttribute("aria-current") === "page").map((b) => b.dataset.tab),
  open: ["details", "alarms", "journal", "settings", "stp"].filter((k) => !els[k].classList.contains("hidden"))
    .concat(els.main.classList.contains("show-search") ? ["search"] : [])
    .concat(els.moreView.classList.contains("hidden") ? [] : ["more"]),
});

// every tab opens its view and closes the others
out.tabs = {};
for (const tab of ["search", "alarms", "journal", "more", "map"]) {
  await phoneGoTab(tab);
  await tick();
  out.tabs[tab] = snap();
}

// Back: More -> Settings, popstate -> More, popstate -> the map
await phoneGoTab("more");
await tick();
const settingsRow = els.moreBody.children.find((row) => row.dataset && row.dataset.key === "settings");
await settingsRow.click();
await tick();
out.back = [snap()];
history.back(); await tick();
out.back.push(snap());
history.back(); await tick();
out.back.push(snap());

// a row of the search: the map, the node selected, its card open
let focused = null;
network = {
  focus: (id) => { focused = id; }, selectNodes: () => {}, getSelectedNodes: () => [focused],
  unselectAll() {}, setOptions() {},
};
showDetails = (id) => { shownDetails = { type: "node", id }; els.details.classList.remove("hidden"); };
setSelectedNode = () => {};
await phoneGoTab("search");
await tick();
focusNode("sw:10.0.0.21");
phoneSync();
await tick();
out.searchRow = { ...snap(), focused };

// the ports panel from Alarms: x comes back to Alarms
await phoneGoTab("alarms");
await tick();
els.alarms.classList.add("hidden");
askPanel();
phoneSync();
els.ports.classList.remove("hidden");
portsIp = "10.0.0.21";
phoneSync();
await tick();
out.portsFromAlarms = [snap()];
els.ports.classList.add("hidden");
phoneSync();
await tick();
out.portsFromAlarms.push(snap());
await phoneGoTab("map");
await tick();

// the count on the tab is the bell's
activeAlarms = [{ severity: "warning" }, { severity: "critical" }, { severity: "info" }];
renderBadge();
out.badge = {
  tab: [els.tabAlarmsBadge.textContent, els.tabAlarmsBadge.classList.contains("critical"),
        els.tabAlarmsBadge.classList.contains("hidden")],
  bell: [els.alarmsBadge.textContent, els.alarmsBadge.classList.contains("critical"),
         els.alarmsBadge.classList.contains("hidden")],
  label: els.tabAlarms.getAttribute("aria-label"),
};

// More: the Actions menu's items, by group; a viewer sees them grey
const rowsOf = () => els.moreBody.children.filter((row) => row.dataset && row.dataset.key);
const seps = () => els.moreBody.children.map((row) => (row.getAttribute && row.getAttribute("role") === "separator" ? "|" : row.dataset && row.dataset.key)).filter(Boolean);
me = { sign_in: true, name: "claude", role: "viewer" };
renderMoreView();
out.viewer = {
  keys: rowsOf().map((row) => row.dataset.key),
  order: seps(),
  off: rowsOf().filter((row) => row.classList.contains("off")).map((row) => [
    row.dataset.key,
    (row.children.find((c) => c.className === "why") || {}).textContent || "",
  ]),
  menu: moreItems().map((item) => [item.key, item.group]),
  account: els.moreBody.children[0].children.map((c) => c.className),
};
me = { sign_in: true, name: "admin", role: "admin" };
renderMoreView();
out.admin = { off: rowsOf().filter((row) => row.classList.contains("off")).map((row) => row.dataset.key) };
me = { sign_in: false, create_admin: "python -m moonlan.users add" };
renderMoreView();
out.open = {
  keys: rowsOf().map((row) => row.dataset.key),
  note: els.moreBody.children[0].children[0].textContent,
};
__done(out);
})();
"""


@unittest.skipUnless(NODE, "needs node to run the page's JavaScript")
class PhonePageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_page(PAGE)

    def test_every_tab_opens_its_own_view(self):
        tabs = self.out["tabs"]
        self.assertEqual(tabs["search"], {"stack": ["search"], "depth": 1, "tab": ["search"], "open": ["search"]})
        self.assertEqual(tabs["alarms"], {"stack": ["alarms"], "depth": 1, "tab": ["alarms"], "open": ["alarms"]})
        self.assertEqual(tabs["journal"], {"stack": ["journal"], "depth": 1, "tab": ["journal"], "open": ["journal"]})
        self.assertEqual(tabs["more"], {"stack": ["more"], "depth": 1, "tab": ["more"], "open": ["more"]})
        # Map closes everything, and puts nothing in the history
        self.assertEqual(tabs["map"], {"stack": [], "depth": 0, "tab": ["map"], "open": []})

    def test_back_closes_the_top_view(self):
        more_settings, more, the_map = self.out["back"]
        self.assertEqual(more_settings["stack"], ["more", "settings"])
        self.assertEqual(more_settings["depth"], 2)
        self.assertEqual(more_settings["tab"], ["more"])
        self.assertEqual(more, {"stack": ["more"], "depth": 1, "tab": ["more"], "open": ["more"]})
        self.assertEqual(the_map, {"stack": [], "depth": 0, "tab": ["map"], "open": []})

    def test_a_row_of_the_search_opens_the_node_on_the_map(self):
        row = self.out["searchRow"]
        self.assertEqual(row["focused"], "sw:10.0.0.21")
        self.assertEqual(row["tab"], ["map"])
        self.assertEqual(row["open"], ["details"])
        self.assertEqual(row["stack"], ["details"])
        self.assertEqual(row["depth"], 1)

    def test_closing_the_ports_comes_back_to_the_alarms(self):
        opened, closed = self.out["portsFromAlarms"]
        self.assertEqual(opened["stack"], ["alarms", "ports"])
        self.assertEqual(opened["tab"], ["alarms"])
        self.assertEqual(closed, {"stack": ["alarms"], "depth": 1, "tab": ["alarms"], "open": ["alarms"]})

    def test_the_tab_counts_what_the_bell_counts(self):
        badge = self.out["badge"]
        self.assertEqual(badge["tab"], badge["bell"])
        self.assertEqual([str(badge["tab"][0])] + badge["tab"][1:], ["3", True, False])
        self.assertEqual(badge["label"], "Alarms: 3")

    def test_more_has_the_menus_items_in_its_order(self):
        viewer = self.out["viewer"]
        menu_keys = [key for key, _group in viewer["menu"]]
        # the text size after arranging (v0.8.8)
        self.assertEqual(viewer["keys"][:3], ["stp", "arrange", "textSize"])
        self.assertEqual(viewer["keys"][3:-1], menu_keys)
        self.assertEqual(viewer["keys"][-1], "logout")
        # a line between the groups, as in the menu
        groups = []
        for _key, group in viewer["menu"]:
            if not groups or groups[-1] != group:
                groups.append(group)
        order = viewer["order"]
        body = order[order.index("|") + 1:order.index("logout") - 1]
        expected = []
        for i, group in enumerate(groups):
            if i:
                expected.append("|")
            expected += [key for key, g in viewer["menu"] if g == group]
        self.assertEqual(body, expected)

    def test_a_viewer_sees_grey_with_the_reason(self):
        off = dict(self.out["viewer"]["off"])
        for key in ("arrange", "rescan", "users", "import", "reset"):
            self.assertIn(key, off)
            self.assertTrue(off[key], key)
        # what a viewer may use is not grey
        for key in ("stp", "exportMap", "export", "settings", "about", "logout"):
            self.assertNotIn(key, off)
        self.assertEqual(self.out["admin"]["off"], ["orphans"])

    def test_the_account_and_the_language(self):
        self.assertEqual(self.out["viewer"]["account"], ["more-account-btn", "lang-switch more-lang"])
        # nobody created: what the header said, and no sign-out
        self.assertEqual(self.out["open"]["note"], strings("en")["noticeOpen"])
        self.assertNotIn("logout", self.out["open"]["keys"])


class SourceTest(unittest.TestCase):
    def test_more_is_redrawn_in_the_new_language(self):
        self.assertIn("redrawOnLanguage(els.moreView, renderMoreView);", APP)
        self.assertIn("redrawOnLanguage(els.statusCard, renderStatusCard);", APP)

    def test_more_takes_the_menus_data(self):
        self.assertIn("moreViewItems(moreItems(), MORE_GROUPS)", function("renderMoreView"))
        self.assertIn('needRole("user")', function("renderMoreView"))

    def test_the_status_button_says_what_the_tooltip_says(self):
        button = function("renderStatusButton")
        self.assertIn('["partial", "failed", "offline"].includes(cls)', button)
        self.assertIn("topology.scan_over_budget", button)
        self.assertIn('fmt("statusLate"', function("renderStatusCard"))

    def test_the_tools_and_the_banner(self):
        self.assertIn("network.fit({ animation:", function("fitMap"))
        self.assertIn('els.arrangeBanner.classList.toggle("hidden", !arrangeMode);',
                      function("applyArrangeMode"))
        self.assertIn("els.toolFreeze.classList.toggle(\"active\", layoutFrozen);",
                      function("applyFreeze"))
        self.assertIn("els.toolPorts", function("applyPortLabelMode"))

    def test_back_and_escape(self):
        self.assertIn('window.addEventListener("popstate", phonePopState);', APP)
        self.assertIn("history.pushState({ moonlanDepth: historyDepth }", function("syncHistory"))
        self.assertIn("if (isPhone() && phoneStack.length) phoneBack();", APP)


if __name__ == "__main__":
    unittest.main()
