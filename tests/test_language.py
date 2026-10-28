"""Not one English sentence in the Russian interface (v0.8.2).

The server sends what a person reads as a code and its numbers, and the
page words it: the STP verdict, a withdrawn link in the journal, the
hint of a port that looks like a way out, how a loop-detection profile
was matched. What an SNMP agent, a ping or the operating system said is
theirs and stays as it is. The words themselves are run through `node`
on the page's own code (skipped without it)."""

import inspect
import json
import re
import unittest

import service_fixture  # noqa: F401  (sets MOONLAN_CONFIG first)
from moonlan import server, topology
from moonlan.stp import STATE_FORWARDING, StpData, StpPort, judge, judge_network
from web_source import APP, INDEX, NODE, function, run_page, strings

ROOT = "34:0a:33:bc:ca:f0"
OWN = "aa:00:00:00:00:01"


def ports(state=STATE_FORWARDING, enabled=True):
    return {i: StpPort(bridge_port=i, if_index=i, state=state, enabled=enabled, link_up=True)
            for i in range(1, 4)}


def verdicts():
    """Each of the nine branches of judge() and judge_network()"""
    own_root = dict(supported=True, designated_root=f"32768/{OWN}", own_macs={OWN},
                    sys_uptime=400_000)
    cases = {
        "not_supported": judge(StpData(supported=False)),
        "foreign_root": judge(StpData(
            supported=True, designated_root=f"4096/{ROOT}", root_cost=20000,
            own_macs={OWN}, sys_uptime=400_000, ports=ports())),
        "reports_nothing": judge(StpData(
            supported=True, designated_root="0/00:00:00:00:00:00", own_macs={ROOT},
            sys_uptime=400_000, ports=ports(state=1))),
        "no_port_enabled": judge(StpData(**own_root, ports=ports(enabled=False))),
        "all_disabled": judge(StpData(**own_root, ports=ports(state=1))),
        "no_root": judge(StpData(
            supported=True, designated_root="", own_macs={OWN},
            sys_uptime=413_991_900, ports=ports())),
        "never_converged": judge(StpData(
            **own_root, time_since_change=400_000, top_changes=0, ports=ports())),
        "converged": judge(StpData(
            **own_root, time_since_change=1000, top_changes=3, ports=ports())),
    }
    network = {
        "10.0.0.10": judge(StpData(
            supported=True, designated_root="0/00:00:00:00:00:00", own_macs={ROOT},
            sys_uptime=400_000, ports=ports(state=1))),
        "10.0.0.17": cases["foreign_root"],
        "10.3.6.5": judge(StpData(
            supported=True, designated_root=f"4096/{ROOT}", root_cost=40000,
            own_macs={"bb:00:00:00:00:02"}, sys_uptime=400_000, ports=ports())),
    }
    judge_network(network)
    cases["root_confirmed"] = network["10.0.0.10"]
    return cases


# what the page passes to fmt() for a reason
PLACES = {"root", "cost", "changes", "since", "uptime", "count", "neighbours"}


def placeholders(text):
    return set(re.findall(r"\{(\w+)\}", text))


class StpReasonTest(unittest.TestCase):
    def test_every_branch_has_its_code_and_numbers(self):
        cases = verdicts()
        for code, data in cases.items():
            with self.subTest(code):
                self.assertEqual(data.reason_code, code)
                # the English sentence stays, for the log and diag --stp
                self.assertTrue(data.reason)
        self.assertEqual(cases["foreign_root"].reason_params, {"root": f"4096/{ROOT}", "cost": 20000})
        self.assertEqual(cases["no_root"].reason_params, {"root": "", "cost": 0})
        self.assertEqual(cases["converged"].reason_params,
                         {"changes": 3, "since": 10.0, "uptime": 4000.0})
        self.assertEqual(cases["never_converged"].reason_params["changes"], 0)
        self.assertEqual(cases["root_confirmed"].reason_params,
                         {"count": 2, "neighbours": ["10.0.0.17", "10.3.6.5"]})

    def test_words_for_every_code_in_both_languages(self):
        en, ru = strings("en"), strings("ru")
        for code in verdicts():
            key = "stpReason_" + code
            with self.subTest(code):
                self.assertIn(key, en)
                self.assertIn(key, ru)
                self.assertEqual(placeholders(en[key]), placeholders(ru[key]))
                self.assertLessEqual(placeholders(ru[key]), PLACES)

    def test_the_api_sends_them(self):
        # the page reads them …
        self.assertIn('t(key) === key) return sw.reason || ""', function("stpReasonText"))
        self.assertIn("td.title = stpReasonText(sw)", function("renderStp"))
        # … and the server puts them in /api/stp
        report = inspect.getsource(server)
        self.assertIn('"reason_code": data.reason_code', report)
        self.assertIn('"reason_params": data.reason_params', report)


class DroppedLinkTest(unittest.TestCase):
    def test_a_new_entry_is_data(self):
        details = json.loads(server._dropped_link_details({
            "a": "10.0.0.10", "b": "10.0.0.22", "a_port": "Gi0/2", "b_port": "Gi0/24",
            "source": "fdb", "reason": "behind", "behind": "10.0.0.21",
        }))
        self.assertEqual(details["reason"], "behind")
        self.assertEqual(details["behind"], "10.0.0.21")
        ring = json.loads(server._dropped_link_details({
            "a": "10.0.0.10", "b": "10.0.0.22", "a_port": "Gi0/2", "b_port": "Gi0/24",
            "source": "both", "reason": "cycle", "cycle": "10.0.0.10 [Gi0/2] — 10.0.0.22 [Gi0/24]",
        }))
        self.assertEqual(set(ring), {"a", "b", "a_port", "b_port", "source", "reason", "cycle"})

    def test_the_words_for_it(self):
        for lang in ("en", "ru"):
            texts = strings(lang)
            for key in ("evLinkEnds", "evLinkSource_fdb", "evLinkSource_lldp",
                        "evLinkSource_both", "evLinkWhy_behind", "evLinkWhy_cycle",
                        "loopHow_sysObjectID", "loopHow_probe", "andMore"):
                self.assertIn(key, texts, f"{lang}: {key}")


class ElsewhereTest(unittest.TestCase):
    def test_the_uplink_hint_counts_instead_of_saying_more(self):
        source = inspect.getsource(topology)
        self.assertIn('"more": len(foreign) - 1,', source)
        self.assertIn('fmt("andMore", { n: p.uplink_hint.more })', APP)

    def test_names_for_a_screen_reader(self):
        closes = re.findall(r"<button[^>]*>×</button>", INDEX)
        # eleven in v0.8.2, and Settings since v0.8.4
        self.assertEqual(len(closes), 12)
        self.assertEqual([b for b in closes if "data-close" not in b], [])
        static = function("applyStatic")
        self.assertIn('document.querySelectorAll("[data-close]")', static)
        self.assertIn('button.setAttribute("aria-label", t("close"))', static)
        self.assertIn('els.search.setAttribute("aria-label", t("searchLabel"))', static)
        self.assertNotIn('"Language"', APP + INDEX)
        for lang in ("en", "ru"):
            self.assertIn("searchLabel", strings(lang))
            self.assertIn("langSwitch", strings(lang))
        self.assertEqual(strings("ru")["searchLabel"], "Поиск по карте")


# Latin a Russian sentence may have: the names of MIB objects and of
# the protocol's own units, addresses and numbers
ALLOWED = re.compile(
    r"dot1dStp\*|dot1dStpPortEnable|enabled|disabled|BRIDGE-MIB|Bridge ID|BPDU|discarding"
    r"|LLDP|MAC|Gi\d+/\d+|[0-9a-f]{2}(?::[0-9a-f]{2}){5}|\d+(?:\.\d+){3}")

PAGE = r"""
lang = "ru";
const out = { stp: {}, unknown: "", dropped: [], old: "" };
for (const [code, params] of Object.entries(__DATA.stp)) {
  out.stp[code] = stpReasonText({ reason_code: code, reason_params: params, reason: "English" });
}
out.unknown = stpReasonText({ reason_code: "from_the_future", reason_params: {}, reason: "the server's own words" });
out.none = stpReasonText({ reason: "an older server" });
for (const details of __DATA.dropped) {
  out.dropped.push(linkDroppedText({ event: "link_dropped", details: JSON.stringify(details) }));
}
out.old = linkDroppedText({ event: "link_dropped", details: "10.0.0.10 [Gi0/2] — 10.0.0.22 [Gi0/24] (fdb): LLDP puts it behind" });
__done(out);
"""


@unittest.skipUnless(NODE, "needs node to run the page's JavaScript")
class RussianWordsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        stp = {code: data.reason_params for code, data in verdicts().items()}
        dropped = [
            {"a": "10.0.0.10", "a_port": "Gi0/2", "b": "10.0.0.22", "b_port": "Gi0/24",
             "source": "fdb", "reason": "behind", "behind": "10.0.0.21"},
            {"a": "10.0.0.10", "a_port": "Gi0/2", "b": "10.0.0.22", "b_port": "Gi0/24",
             "source": "both", "reason": "cycle", "cycle": "10.0.0.10 [Gi0/2] — 10.0.0.22 [Gi0/24]"},
        ]
        cls.out = run_page(PAGE, {"stp": stp, "dropped": dropped})

    def test_no_english_in_the_stp_tooltips(self):
        self.assertEqual(len(self.out["stp"]), 9)
        for code, text in self.out["stp"].items():
            with self.subTest(code):
                self.assertTrue(text)
                self.assertNotRegex(ALLOWED.sub("", text), "[A-Za-z]", text)

    def test_an_unknown_code_shows_the_server_s_words(self):
        self.assertEqual(self.out["unknown"], "the server's own words")
        self.assertEqual(self.out["none"], "an older server")

    def test_a_withdrawn_link_in_words(self):
        behind, ring = self.out["dropped"]
        self.assertIn("таблицы MAC", behind)
        self.assertIn("10.0.0.21", behind)
        self.assertIn("кольца", ring)
        for text in (behind, ring):
            self.assertNotRegex(ALLOWED.sub("", text), "[A-Za-z]", text)
        # an entry from before v0.8.2 is the line it was written as
        self.assertEqual(self.out["old"], "10.0.0.10 [Gi0/2] — 10.0.0.22 [Gi0/24] (fdb): LLDP puts it behind")


if __name__ == "__main__":
    unittest.main()
