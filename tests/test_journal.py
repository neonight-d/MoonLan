"""The journal: groups of events and pages (v0.8.6).

Of the last 1000 events on the production network 952 were host_down
and host_up. The API gave at most 1000 (12.9 hours there), the page
asked for 100 (about an hour), and there was no filter and no next
page: the copies_removed entry of 30.10 could not be found.

Now every event belongs to a group, kept in one place on the server
and sent with the event; /api/journal takes `group` (repeatable) and
`before`, and says where the next page starts. The page has a row of
filters and "Show more".

Run with:  python -m unittest discover -s tests
"""

import ast
import io
import sys
import time
import tokenize
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from service_fixture import SWITCHES  # noqa: E402,F401

from moonlan import server  # noqa: E402
from asgi_client import call  # noqa: E402
from web_source import APP, INDEX, NODE, STYLE, function, run_page, strings  # noqa: E402

CODE = Path(__file__).resolve().parent.parent / "moonlan"

# Events the code writes that are meant to be in no group: "Other".
# Empty today — a new event lands here only on purpose.
INTENDED_OTHER: set[str] = set()


def written_events() -> dict[str, list[str]]:
    """Every event name the code writes to the journal, read from the
    code: the first string argument of each add_event / journal(db, …)
    / account_event / _event / _journal_layout_action / _journal_note,
    the event of each INSERT INTO journal — and the other branch of a
    conditional one ("monitor_on" if … else "monitor_off")."""
    anchors = {"add_event", "account_event", "_event",
               "_journal_layout_action", "_journal_note", "journal"}
    found: dict[str, list[str]] = {}
    for path in sorted(CODE.glob("*.py")):
        tokens = [
            t for t in tokenize.generate_tokens(
                io.StringIO(path.read_text(encoding="utf-8")).readline
            )
            if t.type not in (tokenize.NL, tokenize.NEWLINE, tokenize.COMMENT,
                              tokenize.INDENT, tokenize.DEDENT)
        ]
        for i, tok in enumerate(tokens):
            insert = tok.type == tokenize.STRING and "INSERT INTO journal" in tok.string
            if not insert and not (tok.type == tokenize.NAME and tok.string in anchors):
                continue
            prev = tokens[i - 1].string if i else ""
            nxt = tokens[i + 1].string if i + 1 < len(tokens) else ""
            if prev == "def":
                continue
            if tok.string == "journal" and (prev == "." or nxt != "("):
                continue
            if tok.type == tokenize.NAME and nxt not in ("(", ","):
                continue
            depth, j = 0, i + 1
            if nxt == "(":
                j += 1
            event = None
            while j < len(tokens):
                t = tokens[j]
                if t.string in "([{" and t.type == tokenize.OP:
                    depth += 1
                elif t.string in ")]}" and t.type == tokenize.OP:
                    if depth == 0:
                        break
                    depth -= 1
                elif t.type == tokenize.STRING and depth <= (1 if insert else 0):
                    try:
                        value = ast.literal_eval(t.string)
                    except (ValueError, SyntaxError):
                        value = None
                    if isinstance(value, str) and value.replace("_", "").isalpha() \
                            and value.islower():
                        event = value
                        found.setdefault(value, []).append(f"{path.name}:{t.start[0]}")
                        # "a" if cond else "b": the other branch too
                        k = j + 1
                        if k < len(tokens) and tokens[k].string == "if":
                            while k < len(tokens) and tokens[k].string != "else":
                                k += 1
                            if k + 1 < len(tokens) and tokens[k + 1].type == tokenize.STRING:
                                other = ast.literal_eval(tokens[k + 1].string)
                                found.setdefault(other, []).append(
                                    f"{path.name}:{tokens[k + 1].start[0]}")
                        break
                j += 1
            del event
    return found


class EveryEventHasAGroupTest(unittest.TestCase):
    def test_the_reader_finds_the_known_ones(self):
        events = written_events()
        for event in ("alarm_raised", "host_down", "host_up", "new_mac",
                      "copies_removed", "monitor_on", "monitor_off", "login",
                      "logout", "user_enabled", "settings_changed",
                      "layout_pinned", "note_moved", "link_dropped",
                      "subscription_added", "ip_released"):
            self.assertIn(event, events)

    def test_every_written_event_is_in_a_group(self):
        events = written_events()
        orphans = {
            event: where for event, where in events.items()
            if event not in server.EVENT_GROUP and event not in INTENDED_OTHER
        }
        self.assertEqual(orphans, {}, "events the journal's filters cannot find")

    def test_groups_do_not_overlap_and_name_real_events(self):
        events = written_events()
        seen = set()
        for group, names in server.JOURNAL_GROUPS.items():
            for name in names:
                self.assertNotIn(name, seen, name)
                seen.add(name)
                self.assertIn(name, events, f"{group}: {name} is written nowhere")

    def test_users_is_the_pages_account_events(self):
        body = APP[APP.index("const ACCOUNT_EVENTS = new Set(["):]
        body = body[:body.index("]);")]
        import re
        page = set(re.findall(r'"([a-z_]+)"', body))
        self.assertEqual(page, set(server.JOURNAL_GROUPS["users"]))


class ApiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        server.rt.db._conn.execute("DELETE FROM journal")
        server.rt.db._conn.commit()
        now = time.time()
        # 250 events, the pings among them most of all, as on the network
        self.ids = {}
        for n in range(250):
            event = ("alarm_raised" if n % 25 == 0 else
                     "copies_removed" if n == 30 else
                     "settings_changed" if n == 77 else
                     "something_new" if n == 99 else
                     "host_down" if n % 2 else "host_up")
            server.rt.db.add_event(now - 250 + n, event, f"aa:bb:cc:00:00:{n % 256:02x}")
        rows = server.rt.db._conn.execute("SELECT id, event FROM journal").fetchall()
        for row in rows:
            self.ids.setdefault(row["event"], []).append(row["id"])

    async def journal(self, **query) -> dict:
        args = {"limit": 100, "group": None, "before": None, **query}
        answer = await server.api_journal(**args)
        if hasattr(answer, "status_code"):
            import json
            return {"status": answer.status_code, **json.loads(answer.body)}
        return answer

    async def test_every_event_carries_its_group(self):
        data = await self.journal(limit=1000)
        groups = {(e["event"], e["group"]) for e in data["events"]}
        self.assertIn(("host_down", "ping"), groups)
        self.assertIn(("alarm_raised", "alarms"), groups)
        self.assertIn(("copies_removed", "devices"), groups)
        self.assertIn(("something_new", "other"), groups)
        self.assertIsNone(data["next_before"])

    async def test_one_group(self):
        data = await self.journal(group=["alarms"])
        self.assertEqual({e["event"] for e in data["events"]}, {"alarm_raised"})
        self.assertEqual(len(data["events"]), 10)
        self.assertIsNone(data["next_before"])

    async def test_a_repeated_group(self):
        data = await self.journal(group=["alarms", "devices", "settings"])
        self.assertEqual(
            {e["event"] for e in data["events"]},
            {"alarm_raised", "copies_removed", "settings_changed"},
        )
        self.assertEqual(len(data["events"]), 12)

    async def test_other_is_what_no_group_has(self):
        data = await self.journal(group=["other"])
        self.assertEqual([e["event"] for e in data["events"]], ["something_new"])

    async def test_pages_follow_one_another(self):
        first = await self.journal(group=["ping"])
        self.assertEqual(len(first["events"]), 100)
        self.assertEqual(first["next_before"], first["events"][-1]["id"])
        second = await self.journal(group=["ping"], before=first["next_before"])
        self.assertEqual(len(second["events"]), 100)
        self.assertLess(second["events"][0]["id"], first["events"][-1]["id"])
        third = await self.journal(group=["ping"], before=second["next_before"])
        # 250 events: 10 alarms, 3 others — 237 pings, the last page short
        self.assertEqual(len(third["events"]), 37)
        self.assertIsNone(third["next_before"])
        ids = [e["id"] for page in (first, second, third) for e in page["events"]]
        self.assertEqual(ids, sorted(ids, reverse=True))
        self.assertEqual(len(set(ids)), 237)

    async def test_before_without_a_group(self):
        all_ids = sorted((i for ids in self.ids.values() for i in ids), reverse=True)
        data = await self.journal(limit=5, before=all_ids[10])
        self.assertEqual([e["id"] for e in data["events"]], all_ids[11:16])
        self.assertEqual(data["next_before"], all_ids[15])

    async def test_the_last_page_exactly_full(self):
        data = await self.journal(group=["alarms"], limit=10)
        self.assertEqual(len(data["events"]), 10)
        self.assertIsNone(data["next_before"])

    async def test_an_unknown_group_is_refused(self):
        data = await self.journal(group=["pings"])
        self.assertEqual(data["status"], 400)
        self.assertEqual(data["error"], "unknown_group")

    def test_over_http_the_group_repeats(self):
        answer = call(server.app, "GET",
                      "/api/journal?limit=50&group=alarms&group=devices")
        if answer.status == 401:
            self.skipTest("sign-in is on in this process")
        self.assertEqual(answer.status, 200)
        self.assertEqual(
            {e["group"] for e in answer.json()["events"]}, {"alarms", "devices"}
        )


class IndexTest(unittest.TestCase):
    def test_the_event_id_index_is_there(self):
        indexes = {
            row[1] for row in server.rt.db._conn.execute("PRAGMA index_list(journal)")
        }
        self.assertIn("journal_event_id", indexes)
        plan = " ".join(
            str(tuple(row)) for row in server.rt.db._conn.execute(
                "EXPLAIN QUERY PLAN SELECT id FROM journal WHERE event = ? "
                "AND id < ? ORDER BY id DESC LIMIT ?", ("host_up", 10 ** 9, 101)
            )
        )
        self.assertIn("journal_event_id", plan)
        self.assertNotIn("TEMP B-TREE", plan)


PAGE = r"""
(async () => {
lang = "en";
const out = {};
const requests = [];
// two pages of alarms, one of pings; ids going down
const page = (group, before) => {
  if (group === "alarms" && before == null) {
    return { events: [{ id: 50, ts: 1, event: "alarm_raised", mac: "a", group: "alarms" },
                      { id: 40, ts: 1, event: "alarm_cleared", mac: "a", group: "alarms" }],
             next_before: 40 };
  }
  if (group === "alarms" && before === 40) {
    return { events: [{ id: 30, ts: 1, event: "alarm_raised", mac: "b", group: "alarms" }],
             next_before: null };
  }
  return { events: [{ id: 60, ts: 1, event: "host_down", mac: "c", group: "ping" }],
           next_before: null };
};
api = async (path) => {
  requests.push(path);
  const query = new URLSearchParams(path.split("?")[1]);
  const before = query.get("before");
  return { ok: true, json: async () => page(query.get("group"), before == null ? null : +before) };
};
// a list that remembers what is in it
const items = [];
els.journalList = {
  replaceChildren: (...nodes) => { items.length = 0; items.push(...nodes); },
  append: (...nodes) => items.push(...nodes),
};
const filterButtons = [];
els.journalFilters = {
  setAttribute: () => {},
  replaceChildren: (...nodes) => { filterButtons.length = 0; filterButtons.push(...nodes); },
};
const moreClasses = new Set(["hidden"]);
els.journalMore = { classList: { toggle: (c, on) => (on ? moreClasses.add(c) : moreClasses.delete(c)) } };
const made = [];
const createElement = (tag) => {
  const attrs = {};
  const el = { tag, dataset: {}, className: "", textContent: "", title: "",
    setAttribute: (k, v) => { attrs[k] = v; }, getAttribute: (k) => attrs[k],
    addEventListener: () => {}, append: (...n) => { el.children = (el.children || []).concat(n); },
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false } };
  made.push(el);
  return el;
};
// the stub document takes assignments and keeps none: a document of
// our own for what the journal builds, the stub for everything else
const stubDocument = document;
__define("document", new Proxy({ createElement }, {
  get: (target, key) => (key in target ? target[key] : stubDocument[key]),
}));

await setJournalGroup("alarms");
out.afterFilter = { request: requests[requests.length - 1], items: items.length,
  pressed: filterButtons.filter((b) => b.getAttribute("aria-pressed") === "true").map((b) => b.dataset.group),
  more: !moreClasses.has("hidden"), kept: localStorage.getItem("moonlan-journal-group") };
await moreJournal();
out.afterMore = { request: requests[requests.length - 1], items: items.length,
  more: !moreClasses.has("hidden"), events: lastEvents.map((e) => e.id) };
// the language: the same filter, the same events, the words changed
// the journal open: its registration moved onto a panel that says so
const stubJournal = els.journal;
els.journal = { classList: { contains: (c) => c !== "hidden" } };
panelRedraws.set(els.journal, panelRedraws.get(stubJournal));
panelRedraws.delete(stubJournal);
const asked = requests.length;
setLang("ru");
out.afterLanguage = { requests: requests.length - asked,
  labels: filterButtons.map((b) => b.textContent),
  pressed: filterButtons.filter((b) => b.getAttribute("aria-pressed") === "true").map((b) => b.dataset.group),
  items: items.length };
await setJournalGroup("all");
out.afterAll = { request: requests[requests.length - 1], items: items.length };
__done(out);
})();
"""


@unittest.skipUnless(NODE, "needs node to run the page's JavaScript")
class PageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run_page(PAGE)

    def test_a_filter_asks_again_with_its_group(self):
        self.assertEqual(self.out["afterFilter"], {
            "request": "/api/journal?limit=100&group=alarms", "items": 2,
            "pressed": ["alarms"], "more": True, "kept": "alarms",
        })

    def test_show_more_appends(self):
        self.assertEqual(self.out["afterMore"], {
            "request": "/api/journal?limit=100&group=alarms&before=40",
            "items": 3, "more": False, "events": [50, 40, 30],
        })

    def test_a_language_change_keeps_the_filter(self):
        after = self.out["afterLanguage"]
        self.assertEqual(after["requests"], 0)
        self.assertEqual(after["pressed"], ["alarms"])
        self.assertEqual(after["items"], 3)
        self.assertEqual(after["labels"][:3], ["Все", "Тревоги", "Устройства"])

    def test_all_asks_without_a_group(self):
        self.assertEqual(self.out["afterAll"],
                         {"request": "/api/journal?limit=100", "items": 1})


class SourceTest(unittest.TestCase):
    def test_the_filters_are_in_the_panel(self):
        self.assertIn('id="journal-filters" class="journal-filters" role="group"', INDEX)
        self.assertIn('id="journal-more"', INDEX)
        self.assertIn(
            # "Addresses" after "Ping" (v0.8.7)
            'const JOURNAL_FILTERS = ["all", "alarms", "devices", "ping", "addresses", "map", '
            '"users", "settings"];',
            APP,
        )
        self.assertIn("overflow-x: auto;", STYLE[STYLE.index(".journal-filters {"):])
        self.assertIn('button.setAttribute("aria-pressed"', function("renderJournalFilters"))
        self.assertIn("localStorage.setItem(JOURNAL_GROUP_KEY", function("setJournalGroup"))

    def test_every_filter_has_words_in_both_languages(self):
        for lang in ("en", "ru"):
            table = strings(lang)
            for group in ("all", "alarms", "devices", "ping", "addresses", "map",
                          "users", "settings"):
                self.assertTrue(table.get("journalGroup_" + group), (lang, group))
            for key in ("journalMore", "journalFilters", "journalNoneInGroup"):
                self.assertTrue(table.get(key), (lang, key))

    def test_the_groups_on_the_page_are_the_servers(self):
        self.assertEqual(
            set(server.JOURNAL_GROUPS) | {"all"},
            {"all", "alarms", "devices", "ping", "addresses", "map", "users",
             "settings"},
        )


if __name__ == "__main__":
    unittest.main()
