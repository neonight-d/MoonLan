"""The node menu: what it may link to, and what it may make the server do.

There is no sign-in yet, so whatever the menu can make the server do,
anybody who opens the page can make it do. The rules pinned down here:

- the server pings and traces only nodes it knows, at addresses it
  found itself: an unknown node id is refused, and so is an address
  sent in place of an id;
- one action names at most max_targets nodes — more is refused with
  the ceiling, not trimmed in silence;
- a link from config.yaml may use only whitelisted schemes, and never
  javascript: or data:, whatever the whitelist says;
- every value put into a link is URL-encoded.

Run with:  python -m unittest discover -s tests
"""

import asyncio
import sys
import unittest
from pathlib import Path

from moonlan import menu, probes

ALLOWED, _ = menu.allowed_schemes(["winbox"])


class SchemeTest(unittest.TestCase):
    def test_the_base_schemes_need_no_permission(self):
        for url in ("http://{ip}", "https://{ip}/", "ssh://admin@{ip}",
                    "telnet://{ip}"):
            self.assertIsNone(menu.check_url(url, menu.allowed_schemes([])[0]))

    def test_an_unlisted_scheme_is_refused_until_allowed(self):
        plain, _ = menu.allowed_schemes([])
        self.assertIn("not allowed", menu.check_url("winbox://{ip}", plain))
        self.assertIsNone(menu.check_url("winbox://{ip}", ALLOWED))

    def test_javascript_and_data_never_whatever_the_whitelist_says(self):
        allowed, problems = menu.allowed_schemes(["javascript", "data"])
        self.assertNotIn("javascript", allowed)
        self.assertNotIn("data", allowed)
        self.assertEqual(len(problems), 2)
        for url in ("javascript:alert(1)", "JavaScript:alert(1)",
                    "data:text/html,<script>alert(1)</script>"):
            self.assertIn("never allowed", menu.check_url(url, allowed))

    def test_a_scheme_hidden_by_a_tab_is_still_refused(self):
        """Browsers drop tabs and newlines before reading the scheme."""
        for url in ("java\tscript:alert(1)", "java\nscript:alert(1)",
                    " javascript:alert(1)"):
            self.assertIsNotNone(menu.check_url(url, ALLOWED))

    def test_the_scheme_cannot_come_from_a_value(self):
        self.assertIn("scheme", menu.check_url("{name}://x", ALLOWED))

    def test_an_unknown_placeholder_is_named(self):
        reason = menu.check_url("http://x/?q={adress}", ALLOWED)
        self.assertIn("{adress}", reason)


class ParseLinksTest(unittest.TestCase):
    def test_a_broken_item_is_left_out_with_a_reason(self):
        links, problems = menu.parse_links([
            {"label": "Winbox", "url": "winbox://{ip}",
             "applies_to": ["switch"]},
            {"label": "Evil", "url": "javascript:alert(1)"},
            {"label": "", "url": "http://x"},
            {"label": "No url"},
            {"label": "Where", "url": "http://x", "applies_to": ["router"]},
            "just a string",
        ], ALLOWED)
        self.assertEqual([link.label for link in links], ["Winbox"])
        self.assertEqual(len(problems), 5)
        self.assertTrue(any("Evil" in p and "javascript" in p
                            for p in problems))

    def test_applies_to(self):
        (everywhere, hosts), _ = menu.parse_links([
            {"label": "A", "url": "http://{ip}"},
            {"label": "B", "url": "http://{ip}", "applies_to": "host"},
        ], ALLOWED)
        self.assertTrue(everywhere.applies("group"))
        self.assertTrue(hosts.applies("host"))
        self.assertFalse(hosts.applies("switch"))


class ExpandTest(unittest.TestCase):
    FACTS = {"ip": "10.0.0.21", "mac": "aa:bb:cc:00:00:01",
             "name": "pc 01/../admin?x=1&y", "switch": "10.0.0.10",
             "port": "Gi0/14"}

    def test_a_mac_is_filled_in_and_encoded(self):
        url, missing = menu.expand(
            "https://inventory.local/find?mac={mac}", self.FACTS
        )
        self.assertIsNone(missing)
        self.assertEqual(
            url, "https://inventory.local/find?mac=aa%3Abb%3Acc%3A00%3A00%3A01"
        )

    def test_a_value_cannot_change_the_shape_of_the_address(self):
        url, _ = menu.expand("https://x.local/{name}?p={port}", self.FACTS)
        self.assertEqual(
            url,
            "https://x.local/pc%2001%2F..%2Fadmin%3Fx%3D1%26y?p=Gi0%2F14",
        )

    def test_an_address_stays_readable(self):
        self.assertEqual(
            menu.expand("winbox://{ip}", self.FACTS)[0], "winbox://10.0.0.21"
        )

    def test_a_missing_value_is_named_not_left_empty(self):
        url, missing = menu.expand("ssh://{ip}", {"ip": "", "mac": "x"})
        self.assertIsNone(url)
        self.assertEqual(missing, "ip")


class PingOutputTest(unittest.TestCase):
    def test_iputils(self):
        out = (
            "4 packets transmitted, 3 received, 25% packet loss, time 3004ms\n"
            "rtt min/avg/max/mdev = 0.041/0.052/0.063/0.009 ms\n"
        )
        self.assertEqual(probes.parse_ping(out), {
            "sent": 4, "received": 3, "loss": 25,
            "min": 0.041, "avg": 0.052, "max": 0.063,
        })

    def test_busybox(self):
        out = (
            "4 packets transmitted, 4 packets received, 0% packet loss\n"
            "round-trip min/avg/max = 0.1/0.2/0.3 ms\n"
        )
        result = probes.parse_ping(out)
        self.assertEqual((result["loss"], result["avg"]), (0, 0.2))

    def test_nobody_answered(self):
        out = "4 packets transmitted, 0 received, 100% packet loss, time 3066ms\n"
        result = probes.parse_ping(out)
        self.assertEqual((result["loss"], result["avg"]), (100, None))

    def test_no_summary_is_not_zero_loss(self):
        self.assertIsNone(probes.parse_ping("ping: sendmsg: Network is unreachable"))


class CommandLineTest(unittest.TestCase):
    def test_only_an_address_reaches_the_command_line(self):
        for bad in ("10.0.0.1; reboot", "-f", "--help", "$(id)",
                    "10.0.0.256", "gw.demo.lan", ""):
            self.assertIsNone(probes.checked_address(bad), bad)
        self.assertEqual(probes.checked_address(" 10.0.0.1 "), "10.0.0.1")

    def test_the_address_is_one_argument_at_the_end(self):
        for argv in (probes.ping_argv("10.0.0.1"),
                     probes.trace_argv("traceroute", "10.0.0.1"),
                     probes.trace_argv("tracepath", "10.0.0.1")):
            self.assertEqual(argv[-1], "10.0.0.1")
            self.assertNotIn("sh", argv[0])

    def test_run_uses_no_shell(self):
        """A shell would expand this; exec passes it through verbatim."""
        code, out, timed_out = asyncio.run(probes.run(
            [sys.executable, "-c", "import sys; print(sys.argv[1])",
             "$(echo expanded)"], 10,
        ))
        self.assertEqual((code, out.strip(), timed_out),
                         (0, "$(echo expanded)", False))

    def test_run_gives_up_at_the_timeout(self):
        code, _, timed_out = asyncio.run(probes.run(
            [sys.executable, "-c", "import time; time.sleep(30)"], 0.5,
        ))
        self.assertTrue(timed_out)
        self.assertIsNone(code)


# One shared configuration for every test that imports the service —
# see tests/service_fixture.py for why it cannot be per-module.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from service_fixture import SWITCHES  # noqa: E402,F401

from moonlan import server  # noqa: E402

HOST_MAC = "aa:bb:cc:00:00:01"
QUIET_MAC = "aa:bb:cc:00:00:02"


class _FakeRequest:
    class client:  # noqa: N801 — the shape starlette's Request has
        host = "192.0.2.7"


class ServiceTest(unittest.TestCase):
    """The endpoints, against a small map and a fake ping."""

    def setUp(self):
        self._saved = (
            server.state.switches, server.state.hosts, server.state.bridges,
            server.state.pseudo_switches, server.tools, server.jobs,
            server.config.context_menu.links,
            dict(server.config.switch_web_scheme),
        )
        server.state.switches = [
            {"ip": "10.0.0.1", "name": "core", "mac": "02:00:00:00:00:01"},
            {"ip": "10.0.0.2", "name": "edge", "mac": "02:00:00:00:00:02"},
        ]
        server.state.hosts = [
            {"mac": HOST_MAC, "switch": "10.0.0.2", "port": "Gi0/3"},
            {"mac": QUIET_MAC, "switch": "10.0.0.2", "port": "Gi0/4"},
        ]
        server.state.bridges = []
        server.state.pseudo_switches = [
            {"id": "pseudo:10.0.0.2:Gi0/9", "switch": "10.0.0.2",
             "port": "Gi0/9"},
        ]
        now = 1_790_000_000.0
        with server.db._lock, server.db._conn:
            server.db._conn.execute(
                "INSERT OR REPLACE INTO hosts (mac, ip, name, first_seen, "
                "last_seen, ping_up, last_ping_ok) VALUES (?,?,?,?,?,?,?)",
                (HOST_MAC, "10.0.0.50", "pc-01", now, now, 1, now),
            )
            server.db._conn.execute(
                "INSERT OR REPLACE INTO hosts (mac, ip, name, first_seen, "
                "last_seen) VALUES (?,?,?,?,?)",
                (QUIET_MAC, "", "", now, now),
            )
        self.calls: list[list[str]] = []

        async def fake_runner(argv, timeout):
            self.calls.append(argv)
            return 0, (
                "4 packets transmitted, 4 received, 0% packet loss\n"
                "rtt min/avg/max/mdev = 1.0/2.0/3.0/0.5 ms\n"
            ), False

        server.tools = {"ping": "/bin/ping",
                        "traceroute": ("traceroute", "/bin/traceroute")}
        server.jobs = probes.Jobs(server.tools, fake_runner)
        server.config.switch_web_scheme["10.0.0.2"] = "https"
        server.config.context_menu.links, _ = menu.parse_links([
            {"label": "Inventory", "url": "https://inv.local/?mac={mac}",
             "applies_to": ["host"]},
            {"label": "Winbox", "url": "winbox://{ip}",
             "applies_to": ["switch"]},
        ], ALLOWED)

    def tearDown(self):
        (server.state.switches, server.state.hosts, server.state.bridges,
         server.state.pseudo_switches, server.tools, server.jobs,
         server.config.context_menu.links, web) = self._saved
        server.config.switch_web_scheme.clear()
        server.config.switch_web_scheme.update(web)
        with server.db._lock, server.db._conn:
            server.db._conn.execute(
                "DELETE FROM hosts WHERE mac IN (?, ?)", (HOST_MAC, QUIET_MAC)
            )

    # --- what a node resolves to ---

    def test_the_address_comes_from_moonlan_not_from_the_request(self):
        nodes = server._node_directory()
        self.assertEqual(nodes["host:" + HOST_MAC]["ip"], "10.0.0.50")
        self.assertEqual(nodes["sw:10.0.0.2"]["ip"], "10.0.0.2")
        self.assertEqual(nodes["pseudo:10.0.0.2:Gi0/9"]["kind"], "group")

    def test_the_menu_of_an_unknown_node_is_refused(self):
        response = asyncio.run(server.api_node_menu(id="host:de:ad:be:ef:00:00"))
        self.assertEqual(response.status_code, 404)

    def test_links_are_filled_in_and_encoded(self):
        answer = asyncio.run(server.api_node_menu(id="host:" + HOST_MAC))
        self.assertEqual(
            [c["url"] for c in answer["custom"]],
            ["https://inv.local/?mac=aa%3Abb%3Acc%3A00%3A00%3A01"],
        )
        web = next(link for link in answer["links"] if link["key"] == "web")
        self.assertEqual(web["url"], "http://10.0.0.50")

    def test_a_switch_can_say_it_is_https(self):
        answer = asyncio.run(server.api_node_menu(id="sw:10.0.0.2"))
        web = next(link for link in answer["links"] if link["key"] == "web")
        self.assertEqual(web["url"], "https://10.0.0.2")
        self.assertEqual([c["label"] for c in answer["custom"]], ["Winbox"])

    def test_a_link_without_its_value_says_which(self):
        answer = asyncio.run(server.api_node_menu(id="host:" + QUIET_MAC))
        ssh = next(link for link in answer["links"] if link["key"] == "ssh")
        self.assertEqual((ssh["url"], ssh["missing"]), (None, "ip"))

    # --- starting an action ---

    def _start(self, action, nodes):
        async def scenario():
            answer = await server.api_start_action(
                server.ActionBody(action=action, nodes=nodes), _FakeRequest()
            )
            if isinstance(answer, dict):
                # let the job finish inside this loop
                job = server.jobs.get(answer["id"])
                while not job.finished:
                    await asyncio.sleep(0.01)
                return job.as_dict()
            return answer
        return asyncio.run(scenario())

    def test_an_unknown_node_is_refused(self):
        response = self._start("ping", ["host:de:ad:be:ef:00:00"])
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.calls, [])

    def test_an_address_in_place_of_a_node_is_refused_and_named(self):
        response = self._start("ping", ["10.0.0.50"])
        self.assertEqual(response.status_code, 404)
        self.assertIn(b'"addresses":["10.0.0.50"]', response.body)
        self.assertEqual(self.calls, [])

    def test_over_the_ceiling_is_refused_not_trimmed(self):
        ids = [f"host:aa:00:00:00:{n // 256:02x}:{n % 256:02x}"
               for n in range(100)]
        response = self._start("ping", ids)
        self.assertEqual(response.status_code, 400)
        self.assertIn(b'"limit":64', response.body)
        self.assertIn(b'"count":100', response.body)
        self.assertEqual(self.calls, [])

    def test_a_ping_runs_against_the_address_moonlan_knows(self):
        job = self._start("ping", ["host:" + HOST_MAC])
        self.assertEqual(self.calls, [probes.ping_argv("10.0.0.50")])
        target = job["targets"][0]
        self.assertEqual(target["status"], "done")
        self.assertEqual(target["result"]["avg"], 2.0)
        # …with what the continuous ping knows, as a separate source
        self.assertTrue(target["monitor"]["ping_up"])

    def test_a_node_without_an_address_is_a_row_not_a_process(self):
        job = self._start("ping", ["host:" + HOST_MAC, "host:" + QUIET_MAC,
                                   "sw:10.0.0.1"])
        statuses = {t["node"]: t["status"] for t in job["targets"]}
        self.assertEqual(statuses["host:" + QUIET_MAC], "no_ip")
        self.assertEqual(len(self.calls), 2)

    def test_traceroute_is_for_one_node(self):
        response = self._start("traceroute", ["sw:10.0.0.1", "sw:10.0.0.2"])
        self.assertEqual(response.status_code, 400)

    def test_a_missing_tool_is_said_so(self):
        server.tools = {"ping": "/bin/ping", "traceroute": None}
        response = self._start("traceroute", ["sw:10.0.0.1"])
        self.assertEqual(response.status_code, 503)
        self.assertIn(b"traceroute", response.body)

    def test_an_unknown_action_is_refused(self):
        response = self._start("reboot", ["sw:10.0.0.1"])
        self.assertEqual(response.status_code, 400)

    def test_one_request_too_many_is_refused_not_queued(self):
        release = asyncio.Event()

        async def slow_runner(argv, timeout):
            await release.wait()
            return 0, "", False

        server.jobs = probes.Jobs(server.tools, slow_runner)
        limit = server.config.context_menu.max_running

        async def scenario():
            started = []
            for _ in range(limit):
                started.append(await server.api_start_action(
                    server.ActionBody(action="ping", nodes=["sw:10.0.0.1"]),
                    _FakeRequest(),
                ))
            refused = await server.api_start_action(
                server.ActionBody(action="ping", nodes=["sw:10.0.0.1"]),
                _FakeRequest(),
            )
            release.set()
            await asyncio.sleep(0.05)
            return started, refused

        started, refused = asyncio.run(scenario())
        self.assertTrue(all(isinstance(s, dict) for s in started))
        self.assertEqual(refused.status_code, 429)


if __name__ == "__main__":
    unittest.main()
