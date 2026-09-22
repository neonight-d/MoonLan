"""The node menu: what it may link to, and what it may make the server do.

There is no sign-in yet, so whatever the menu can make the server do,
anybody who opens the page can make it do. The rules pinned down here:

- the server pings and traces only nodes it knows, at addresses it
  found itself: an unknown node id is refused, and so is an address
  sent in place of an id;
- one action names at most max_targets nodes — more is refused with
  the ceiling, not trimmed in silence.

Run with:  python -m unittest discover -s tests
"""

import asyncio
import sys
import unittest
from pathlib import Path

from moonlan import probes


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

    def tearDown(self):
        (server.state.switches, server.state.hosts, server.state.bridges,
         server.state.pseudo_switches, server.tools, server.jobs,
         ) = self._saved
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
