"""Reading a spanning tree off agents that disagree about how to report it.

Three things are pinned here, all of them found on one production
network the day RSTP was switched on:

- one root reported two ways. An Edge-Core answers
  `00 10 34 0a 33 bc ca f0` where an HPE answers
  `10 00 34 0a 33 bc ca f0` for the same root: the priority is in the
  wrong byte. Read honestly that is 16 against 4096, and the network
  was reported as having two spanning trees and raised
  stp_fragmented on a network with one;
- four switches called "not operating" while their own CLI showed
  RSTP running and one of them being the root. The old test asked for
  history — topology changes, or a change newer than the uptime — and
  a tree switched on an hour ago has none;
- blocking ports with no cable in them, which RouterOS reports on
  every spare socket.

Run with:  python -m unittest discover -s tests
"""

import unittest

from moonlan.stp import (
    STATE_BLOCKING,
    STATE_FORWARDING,
    StpData,
    StpPort,
    format_bridge_id,
    judge,
    judge_network,
    network_verdict,
    parse_bridge_id,
    rootless,
)

ROOT_MAC = "34:0a:33:bc:ca:f0"
# the three encodings seen on the wire, all naming the same bridge
STANDARD = bytes.fromhex("100034 0a33bcca f0".replace(" ", ""))
SHIFTED = bytes.fromhex("001034 0a33bcca f0".replace(" ", ""))


def raw_id(prefix: str, mac: str) -> bytes:
    return bytes.fromhex(prefix) + bytes(
        int(part, 16) for part in mac.split(":")
    )


class BridgeIdTest(unittest.TestCase):
    def test_the_standard_encoding(self):
        parsed = parse_bridge_id(raw_id("1000", ROOT_MAC))
        self.assertEqual(parsed.priority, 4096)
        self.assertEqual(parsed.sys_id_ext, 0)
        self.assertEqual(parsed.mac, ROOT_MAC)
        self.assertFalse(parsed.nonstandard)
        self.assertEqual(str(parsed), f"4096/{ROOT_MAC}")

    def test_the_priority_in_the_low_byte(self):
        """Every pair observed differed by exactly 256."""
        for prefix, expected in (
            ("0010", 4096), ("0040", 16384), ("0080", 32768),
        ):
            with self.subTest(prefix=prefix):
                parsed = parse_bridge_id(raw_id(prefix, ROOT_MAC))
                self.assertEqual(parsed.priority, expected)
                self.assertTrue(parsed.nonstandard)
                self.assertEqual(parsed.mac, ROOT_MAC)

    def test_both_encodings_of_one_root_read_the_same(self):
        self.assertEqual(
            format_bridge_id(raw_id("1000", ROOT_MAC)),
            format_bridge_id(raw_id("0010", ROOT_MAC)),
        )

    def test_the_system_id_extension_is_not_part_of_the_priority(self):
        """802.1t: 4 bits of priority, 12 of MSTI/VLAN number."""
        parsed = parse_bridge_id(raw_id("1005", ROOT_MAC))
        self.assertEqual(parsed.priority, 4096)
        self.assertEqual(parsed.sys_id_ext, 5)
        self.assertEqual(str(parsed), f"4096+5/{ROOT_MAC}")

    def test_an_all_zero_bridge_id_is_left_alone(self):
        """What the D-Links answer, and it means nothing, not 0x0000."""
        parsed = parse_bridge_id(raw_id("0000", "00:00:00:00:00:00"))
        self.assertEqual(parsed.priority, 0)
        self.assertFalse(parsed.nonstandard)

    def test_something_that_is_not_a_bridge_id(self):
        self.assertIsNone(parse_bridge_id(b"\x01\x02"))
        self.assertEqual(format_bridge_id(b"\x01\x02"), "0102")
        self.assertEqual(format_bridge_id(b""), "")


def member(ip_mac: str, root: str, cost: int, **kw) -> StpData:
    """A switch that has accepted somebody else's root."""
    return judge(StpData(
        supported=True, protocol_spec=3, priority=32768,
        designated_root=root, root_cost=cost, root_port=25,
        own_macs={ip_mac}, sys_uptime=400_000,
        ports={25: StpPort(bridge_port=25, if_index=25, state=STATE_FORWARDING,
                           enabled=True, link_up=True)},
        **kw,
    ))


def silent_root(mac: str) -> StpData:
    """A D-Link running RSTP and reporting nothing about it."""
    return judge(StpData(
        supported=True, protocol_spec=3, priority=0,
        designated_root="0/00:00:00:00:00:00", root_cost=0, root_port=0,
        own_macs={mac}, sys_uptime=400_000, time_since_change=0,
        top_changes=0,
        ports={i: StpPort(bridge_port=i, if_index=i, state=1, enabled=True)
               for i in range(1, 5)},
    ))


def tree_off(mac: str) -> StpData:
    """STP disabled: answers everything, names itself, at cost zero."""
    return judge(StpData(
        supported=True, protocol_spec=3, priority=0,
        designated_root=f"0/{mac}", root_cost=0, root_port=0,
        own_macs={mac}, sys_uptime=400_000, time_since_change=400_000,
        top_changes=0,
        ports={i: StpPort(bridge_port=i, if_index=i, state=1, enabled=False)
               for i in range(1, 5)},
    ))


def rootless_bridge(mac: str) -> StpData:
    """RouterOS: a real port table, and none of the scalars around it.

    A walk of 1.3.6.1.2.1.17.2 on an RB941 returns
    dot1dStpProtocolSpecification, dot1dStpPriority and the whole port
    table — with forwarding and blocking states, path costs and a
    designated root per port — and nothing in between. No
    dot1dStpDesignatedRoot, no root cost, no root port, no topology
    changes. So the switch names no root while its uptime is six weeks
    and its TimeSinceTopologyChange is zero, which the historical
    heuristic reads as "converged long ago".
    """
    return judge(StpData(
        supported=True, protocol_spec=3, priority=32768,
        designated_root="", root_cost=0, root_port=0,
        own_macs={mac}, sys_uptime=413_991_900, time_since_change=0,
        top_changes=0,
        ports={
            1: StpPort(bridge_port=1, if_index=1, state=STATE_FORWARDING,
                       enabled=True, link_up=True),
            3: StpPort(bridge_port=3, if_index=3, state=2, enabled=True,
                       link_up=True),
        },
    ))


class RootlessBridgeTest(unittest.TestCase):
    """A bridge that names no root is not taking part in a tree.

    Three RouterOS boxes reached the panel as a second spanning tree
    whose root was the string "unknown", and stp_fragmented raised,
    cleared and raised again over it. Two defects in a row: a verdict
    that believed a heuristic with nothing to work on, and a grouping
    that let the absence of a root become a root of its own.
    """

    def test_no_root_means_not_operating(self):
        data = rootless_bridge("18:fd:74:fd:b3:af")
        self.assertFalse(data.operating)
        self.assertIn("names no root", data.reason)

    def test_the_first_two_rules_still_come_first(self):
        """Accepting a foreign root, and being confirmed by neighbours,
        are evidence. They are not affected by this."""
        data = member("aa:00:00:00:00:01", f"4096/{ROOT_MAC}", 20000)
        self.assertTrue(data.operating)

    def test_they_do_not_make_a_second_tree(self):
        per_switch = {
            "10.0.0.10": silent_root(ROOT_MAC),
            "10.0.0.21": member("aa:00:00:00:00:01", f"4096/{ROOT_MAC}", 20000),
            "10.3.6.2": rootless_bridge("18:fd:74:fd:b3:af"),
            "10.3.6.4": rootless_bridge("18:fd:74:fd:e3:80"),
            "10.3.7.10": rootless_bridge("18:fd:74:fd:b5:c5"),
        }
        judge_network(per_switch)
        verdict = network_verdict(per_switch)
        self.assertEqual(verdict["verdict"], "single")
        self.assertEqual(len(verdict["roots"]), 1)
        self.assertNotIn("unknown", verdict["roots"])
        # …and they are listed rather than dropped out of sight
        self.assertEqual(
            verdict["rootless"], ["10.3.6.2", "10.3.6.4", "10.3.7.10"]
        )

    def test_two_real_roots_are_still_two(self):
        """The alarm this protects must not be disarmed along the way."""
        other = "02:11:22:33:44:55"
        per_switch = {
            "10.0.0.10": silent_root(ROOT_MAC),
            "10.0.0.21": member("aa:00:00:00:00:01", f"4096/{ROOT_MAC}", 20000),
            "10.9.0.1": silent_root(other),
            "10.9.0.2": member("bb:00:00:00:00:02", f"4096/{other}", 20000),
            "10.3.6.2": rootless_bridge("18:fd:74:fd:b3:af"),
        }
        judge_network(per_switch)
        verdict = network_verdict(per_switch)
        self.assertEqual(verdict["verdict"], "fragmented")
        self.assertEqual(len(verdict["roots"]), 2)
        self.assertEqual(verdict["rootless"], ["10.3.6.2"])

    def test_a_switch_reporting_nothing_at_all_is_not_in_that_list(self):
        """`reports_nothing` is a different finding with its own words.

        Those agents answer the whole subtree with zeros; these answer
        a real port table and skip the scalars. Lumping them together
        would lose the distinction that names the cause.
        """
        per_switch = {
            "10.0.0.10": silent_root(ROOT_MAC),
            "10.0.0.44": tree_off("02:99:99:99:99:99"),
        }
        self.assertEqual(rootless(per_switch), [])


class OperatingTest(unittest.TestCase):
    def test_a_bridge_that_accepted_a_foreign_root_is_operating(self):
        data = member("aa:00:00:00:00:01", f"4096/{ROOT_MAC}", 20000)
        self.assertTrue(data.operating)
        self.assertIn("accepted an external root", data.reason)

    def test_a_freshly_converged_tree_is_not_called_unconverged(self):
        """Zero topology changes, no history at all — and running."""
        data = member(
            "aa:00:00:00:00:01", f"4096/{ROOT_MAC}", 20000,
            top_changes=0, time_since_change=400_000,
        )
        self.assertTrue(data.operating)

    def test_a_switch_reporting_nothing_says_so(self):
        data = silent_root(ROOT_MAC)
        self.assertFalse(data.operating)
        self.assertIn("answers dot1dStp* with nothing", data.reason)
        self.assertIn("does not put it in", data.reason)

    def test_neighbours_confirm_the_root(self):
        network = {
            "10.0.0.10": silent_root(ROOT_MAC),
            "10.0.0.17": member("44:5b:ed:5b:39:40", f"4096/{ROOT_MAC}", 20000),
            "10.3.6.5": member("70:72:cf:63:22:2d", f"4096/{ROOT_MAC}", 420000),
        }
        judge_network(network)
        root = network["10.0.0.10"]
        self.assertTrue(root.operating)
        self.assertTrue(root.confirmed_root)
        self.assertTrue(root.is_root())
        self.assertIn("2 neighbour(s)", root.reason)

    def test_one_switch_alone_has_no_witness(self):
        network = {"10.0.0.10": silent_root(ROOT_MAC)}
        judge_network(network)
        self.assertFalse(network["10.0.0.10"].operating)

    def test_a_network_of_disabled_trees_stays_disabled(self):
        """The false positive the whole verdict exists to avoid.

        Five switches with STP off, each naming itself at cost zero.
        Nobody accepted anybody's root, so nobody can confirm anybody.
        """
        network = {
            f"10.0.0.{n}": tree_off(f"aa:00:00:00:00:{n:02x}")
            for n in range(1, 6)
        }
        judge_network(network)
        self.assertEqual([d.operating for d in network.values()], [False] * 5)
        verdict = network_verdict(network)
        self.assertEqual(verdict["verdict"], "not_operating")

    def test_a_switch_that_answers_nothing_at_all(self):
        data = judge(StpData(supported=False))
        self.assertFalse(data.operating)


class NetworkVerdictTest(unittest.TestCase):
    def test_one_root_in_two_spellings_is_one_tree(self):
        """The false stp_fragmented, stated as a test."""
        network = {
            "10.0.0.17": member(
                "44:5b:ed:5b:39:40", format_bridge_id(STANDARD), 20000
            ),
            "10.3.6.5": member(
                "70:72:cf:63:22:2d", format_bridge_id(SHIFTED), 420000,
                root_nonstandard=True,
            ),
        }
        verdict = network_verdict(network)
        self.assertEqual(verdict["verdict"], "single")
        self.assertEqual(list(verdict["roots"]), [f"4096/{ROOT_MAC}"])

    def test_the_confirmed_root_joins_its_own_tree(self):
        network = {
            "10.0.0.10": silent_root(ROOT_MAC),
            "10.0.0.17": member("44:5b:ed:5b:39:40", f"4096/{ROOT_MAC}", 20000),
        }
        judge_network(network)
        verdict = network_verdict(network)
        self.assertEqual(verdict["verdict"], "single")
        self.assertEqual(
            verdict["roots"], {f"4096/{ROOT_MAC}": ["10.0.0.10", "10.0.0.17"]}
        )

    def test_two_real_roots_are_still_two(self):
        network = {
            "10.0.0.17": member("44:5b:ed:5b:39:40", f"4096/{ROOT_MAC}", 20000),
            "10.3.6.5": member(
                "70:72:cf:63:22:2d", "32768/18:fd:74:fd:b5:ca", 200000
            ),
        }
        verdict = network_verdict(network)
        self.assertEqual(verdict["verdict"], "fragmented")
        self.assertEqual(len(verdict["roots"]), 2)


class BlockingPortTest(unittest.TestCase):
    """RouterOS reports blocking on sockets with nothing in them."""

    def workshop(self) -> StpData:
        return StpData(
            supported=True, own_macs={"18:fd:74:fd:b3:af"},
            ports={
                1: StpPort(bridge_port=1, if_index=1, state=STATE_FORWARDING,
                           enabled=True, link_up=True, name="ether1"),
                2: StpPort(bridge_port=2, if_index=2, state=STATE_BLOCKING,
                           enabled=True, link_up=True, name="ether2"),
                3: StpPort(bridge_port=3, if_index=3, state=STATE_BLOCKING,
                           enabled=True, link_up=False, name="ether3"),
                4: StpPort(bridge_port=4, if_index=4, state=STATE_BLOCKING,
                           enabled=True, link_up=False, name="ether4"),
            },
        )

    def test_only_a_port_with_a_cable_counts_as_blocking(self):
        blocking = [p.name for p in self.workshop().blocking_ports()]
        self.assertEqual(blocking, ["ether2"])

    def test_an_unknown_link_state_is_not_held_against_the_port(self):
        """Before the interface table is in, blocking still reads as blocking."""
        port = StpPort(bridge_port=9, state=STATE_BLOCKING, link_up=None)
        self.assertTrue(port.blocking)


if __name__ == "__main__":
    unittest.main()
