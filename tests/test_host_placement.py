"""Where a device seen only through trunks is drawn.

This tuple has been reordered three times and there was never a test
on it. Each reordering fixed the complaint in front of it and broke
the neighbouring case, because nothing said what the other cases were:

- v0.6.4 ordered it (downlink, depth, …). The root has no uplink, so
  every one of its trunks counted as a downlink and the root won every
  claim; offline devices of one subnet ended up split across the
  core's trunks;
- v0.6.6 fixed how `downlink` was computed and, in passing, moved
  depth in front of it. The pendulum swung the other way: the deepest
  switch won every claim it could make, and an Edge-Core whose only
  live port was its own uplink collected twenty devices belonging to
  the rest of the network;
- v0.6.9 puts direction first and depth second, and refuses to place a
  device whose every sighting was on an uplink.

All three cases are pinned here, on trees assembled by hand. A MAC on
an uplink says where the device is NOT.

Run with:  python -m unittest discover -s tests
"""

import unittest

from moonlan.snmp_collector import PortInfo, SwitchData
from moonlan.topology import build_topology

WANDERER = "aa:bb:cc:00:00:01"


def switch(ip: str, name: str, octet: int, ports: int = 26) -> SwitchData:
    sw = SwitchData(
        ip=ip, reachable=True, sys_name=name,
        bridge_mac=f"02:00:00:00:00:{octet:02x}",
    )
    sw.own_macs = {sw.bridge_mac}
    for i in range(1, ports + 1):
        sw.ports[i] = PortInfo(
            if_index=i, name=f"Gi0/{i}", oper_up=True, speed_mbps=1000
        )
    return sw


def chain() -> tuple[SwitchData, SwitchData, SwitchData]:
    """core -- mid -- leaf, the shape of the network this came from.

    core Gi0/1 goes down to mid; mid Gi0/24 is its uplink and Gi0/2 its
    downlink to leaf; leaf Gi0/25 is its uplink. The ordinary devices
    on the core are there to make it the root — the root is the switch
    that sees the most others, ties broken by table size.
    """
    core = switch("10.0.0.1", "core-sw", 1)
    mid = switch("10.0.0.2", "mid-sw", 2)
    leaf = switch("10.0.0.3", "leaf-sw", 3)
    core.fdb[mid.bridge_mac] = 1
    core.fdb[leaf.bridge_mac] = 1
    mid.fdb[core.bridge_mac] = 24
    mid.fdb[leaf.bridge_mac] = 2
    leaf.fdb[core.bridge_mac] = 25
    leaf.fdb[mid.bridge_mac] = 25
    for n in range(1, 6):
        core.fdb[f"aa:00:00:00:00:{n:02x}"] = 10 + n
    mid.fdb["aa:00:00:00:01:01"] = 11
    return core, mid, leaf


def place(switches, **kwargs):
    """(hosts by mac, uplink_only) out of build_topology."""
    result = build_topology(switches, unmanaged_threshold=0, **kwargs)
    hosts, info = result[2], result[6]
    return {h["mac"]: h for h in hosts}, info.get("uplink_only", {})


class TreeShapeTest(unittest.TestCase):
    """The fixture has to be the tree the tests think it is."""

    def test_the_chain_is_inferred_as_a_chain(self):
        core, mid, leaf = chain()
        result = build_topology([core, mid, leaf], unmanaged_threshold=0)
        links, info = result[1], result[6]
        self.assertEqual(info["root"], "10.0.0.1")
        self.assertEqual(
            sorted((l["a"], l["a_port"], l["b"], l["b_port"]) for l in links),
            [
                ("10.0.0.1", "Gi0/1", "10.0.0.2", "Gi0/24"),
                ("10.0.0.2", "Gi0/2", "10.0.0.3", "Gi0/25"),
            ],
        )


class UplinkIsNotAPlaceTest(unittest.TestCase):
    """v0.6.9: the defect this version exists for."""

    def test_a_downlink_beats_a_deeper_uplink(self):
        core, mid, leaf = chain()
        # the leaf sees it on its uplink — the one cable the device
        # cannot be behind — and the shallower switch sees it on a
        # port pointing away from the root
        mid.fdb[WANDERER] = 2
        leaf.fdb[WANDERER] = 25
        hosts, uplink_only = place([core, mid, leaf])
        self.assertIn(WANDERER, hosts)
        self.assertEqual(hosts[WANDERER]["switch"], "10.0.0.2")
        self.assertEqual(hosts[WANDERER]["port"], "Gi0/2")
        self.assertTrue(hosts[WANDERER]["approximate"])
        self.assertEqual(uplink_only, {})
        # and nothing at all is drawn behind the leaf
        self.assertEqual(
            [h for h in hosts.values() if h["switch"] == "10.0.0.3"], []
        )

    def test_seen_on_uplinks_only_is_not_placed_at_all(self):
        core, mid, leaf = chain()
        mid.fdb[WANDERER] = 24   # mid's uplink
        leaf.fdb[WANDERER] = 25  # leaf's uplink
        hosts, uplink_only = place([core, mid, leaf])
        self.assertNotIn(WANDERER, hosts)
        self.assertIn(WANDERER, uplink_only)
        # the card can say where it was seen, all of it useless
        self.assertEqual(
            sorted(uplink_only[WANDERER]),
            [("10.0.0.2", "Gi0/24"), ("10.0.0.3", "Gi0/25")],
        )


class RegressionTest(unittest.TestCase):
    """The two versions this rule already got wrong."""

    def test_v064_the_root_does_not_take_a_deeper_switchs_device(self):
        """The root's trunk is a downlink too — but a shallow one."""
        core, mid, leaf = chain()
        core.fdb[WANDERER] = 1   # the root's trunk toward mid
        mid.fdb[WANDERER] = 2    # mid's own downlink toward leaf
        hosts, uplink_only = place([core, mid, leaf])
        self.assertEqual(hosts[WANDERER]["switch"], "10.0.0.2")
        self.assertEqual(hosts[WANDERER]["port"], "Gi0/2")
        self.assertEqual(uplink_only, {})

    def test_v066_a_device_only_the_root_sees_is_still_placed(self):
        """All of the root's ports point down: nothing is above it.

        Treating "no uplink" as "direction unknown" would send this
        device to the off-map list, which is how the fix for the case
        above used to overshoot.
        """
        core, mid, leaf = chain()
        core.fdb[WANDERER] = 1
        hosts, uplink_only = place([core, mid, leaf])
        self.assertIn(WANDERER, hosts)
        self.assertEqual(hosts[WANDERER]["switch"], "10.0.0.1")
        self.assertEqual(hosts[WANDERER]["port"], "Gi0/1")
        self.assertTrue(hosts[WANDERER]["approximate"])
        self.assertEqual(uplink_only, {})


class RememberedLocationTest(unittest.TestCase):
    """v0.6.4's other half, which must survive all of this."""

    def test_a_remembered_real_port_wins_over_any_trunk(self):
        core, mid, leaf = chain()
        mid.fdb[WANDERER] = 2
        leaf.fdb[WANDERER] = 25
        hosts, uplink_only = place(
            [core, mid, leaf],
            remembered_locations={WANDERER: ("10.0.0.3", "Gi0/7")},
        )
        self.assertEqual(hosts[WANDERER]["switch"], "10.0.0.3")
        self.assertEqual(hosts[WANDERER]["port"], "Gi0/7")
        self.assertTrue(hosts[WANDERER]["remembered"])
        self.assertNotIn("approximate", hosts[WANDERER])
        self.assertEqual(uplink_only, {})

    def test_a_remembered_trunk_port_is_not_a_memory_worth_keeping(self):
        """It is the previous guess, not a sighting."""
        core, mid, leaf = chain()
        mid.fdb[WANDERER] = 2
        leaf.fdb[WANDERER] = 25
        hosts, _ = place(
            [core, mid, leaf],
            remembered_locations={WANDERER: ("10.0.0.3", "Gi0/25")},
        )
        self.assertEqual(hosts[WANDERER]["switch"], "10.0.0.2")
        self.assertTrue(hosts[WANDERER]["approximate"])

    def test_an_uplink_only_device_still_honours_its_remembered_port(self):
        core, mid, leaf = chain()
        mid.fdb[WANDERER] = 24
        leaf.fdb[WANDERER] = 25
        hosts, uplink_only = place(
            [core, mid, leaf],
            remembered_locations={WANDERER: ("10.0.0.2", "Gi0/9")},
        )
        self.assertEqual(hosts[WANDERER]["switch"], "10.0.0.2")
        self.assertEqual(hosts[WANDERER]["port"], "Gi0/9")
        self.assertEqual(uplink_only, {})


class OrdinaryPlacementTest(unittest.TestCase):
    """A device on a real port is not affected by any of this."""

    def test_a_host_on_an_access_port_stays_there(self):
        core, mid, leaf = chain()
        leaf.fdb[WANDERER] = 7
        core.fdb[WANDERER] = 1   # the core sees it through its trunk
        hosts, uplink_only = place([core, mid, leaf])
        self.assertEqual(hosts[WANDERER]["switch"], "10.0.0.3")
        self.assertEqual(hosts[WANDERER]["port"], "Gi0/7")
        self.assertNotIn("approximate", hosts[WANDERER])
        self.assertEqual(uplink_only, {})

    def test_place_trunk_only_false_draws_nothing_and_claims_nothing(self):
        core, mid, leaf = chain()
        mid.fdb[WANDERER] = 2
        hosts, uplink_only = place([core, mid, leaf], place_trunk_only=False)
        self.assertNotIn(WANDERER, hosts)
        self.assertEqual(uplink_only, {})


if __name__ == "__main__":
    unittest.main()
