"""LLDP behind an aggregate is on the aggregate (v0.8.11).

LLDP arrives on a physical member; the devices behind an aggregate are
on the aggregate. A router moved from a single port onto a LACP pair
came in over LLDP on Gi0/16 while its MACs were under
"LAG Gi0/15+Gi0/16", and nothing keyed by (switch, port) put the two
together: a bridge there would not have taken over its port's
pseudo-switch, its twin MAC would have stayed a second node, and the
name LLDP gave a non-bridge never reached its host.

Both kinds of aggregate: a D-Link trunk inferred from the
dot1dBasePortIfIndex gaps (FDB on the synthetic port -15) and an
IEEE8023-LAG-MIB one (a positive aggregate ifIndex).

Run with:  python -m unittest discover -s tests
"""

import unittest

from moonlan.lldp import LldpNeighbor
from moonlan.snmp_collector import PortInfo, SwitchData
from moonlan.topology import build_topology, detect_bridges, normalized_fdb

SWITCH = "10.1.0.21"
SYNTHETIC = -15
SYNTHETIC_NAME = "LAG Gi0/15+Gi0/16"
AGGREGATE = 1000
AGGREGATE_NAME = "Po1"
BOX = "02:ee:00:00:10:01"
DEVICES = [f"00:ee:00:00:20:{n:02x}" for n in range(1, 5)]


def access_switch(kind: str) -> tuple[SwitchData, int, str]:
    """access-sw with ports Gi0/1..26 and an aggregate of 15 and 16.

    kind "synthetic": the D-Link way, the members missing from
    dot1dBasePortIfIndex and the FDB on bridge-port 15's synthetic
    ifIndex -15. kind "ieee": IEEE8023-LAG-MIB, members 15 and 16 of
    the aggregate ifIndex 1000.
    """
    sw = SwitchData(
        ip=SWITCH, reachable=True, sys_name="access-sw",
        bridge_mac="02:ee:00:00:00:01",
    )
    sw.own_macs = {sw.bridge_mac}
    for i in range(1, 27):
        sw.ports[i] = PortInfo(
            if_index=i, name=f"Gi0/{i}", oper_up=True, speed_mbps=1000
        )
    if kind == "synthetic":
        sw.lag_groups = {15: [15, 16]}
        sw.ports[SYNTHETIC] = PortInfo(
            if_index=SYNTHETIC, name=SYNTHETIC_NAME, is_physical=False,
        )
        return sw, SYNTHETIC, SYNTHETIC_NAME
    sw.lag_members = {15: AGGREGATE, 16: AGGREGATE}
    sw.ports[AGGREGATE] = PortInfo(
        if_index=AGGREGATE, name=AGGREGATE_NAME, oper_up=True,
        speed_mbps=2000, is_physical=False,
    )
    return sw, AGGREGATE, AGGREGATE_NAME


def neighbour(chassis: str, caps: set[str], **extra) -> LldpNeighbor:
    """What LLDP reports on the physical member Gi0/16."""
    return LldpNeighbor(
        local_ifindex=16, local_port_num=16, port_matched_by="loc_id",
        chassis_id=chassis, chassis_subtype=4, cap_enabled=caps,
        cap_known=True, **extra,
    )


def build(sw: SwitchData, **kwargs):
    result = build_topology([sw], **kwargs)
    return {
        "hosts": {h["mac"]: h for h in result[2]},
        "pseudo": result[3],
        "bridges": result[5],
        "info": result[6],
    }


class AggregateKinds:
    """The same cases for both kinds; `kind` is set by the subclasses."""

    kind = ""

    def switch(self):
        return access_switch(self.kind)

    def test_the_entry_is_on_the_aggregate_and_names_the_member(self):
        sw, _index, name = self.switch()
        sw.lldp_neighbors = [
            neighbour(BOX, {"bridge"}, sys_name="RouterOS-C",
                      mgmt_ip="10.1.5.6", mgmt_ips=["10.1.5.6"]),
        ]
        bridges, _others, _unidentified = detect_bridges([sw], set(), {})
        self.assertEqual(len(bridges), 1)
        self.assertEqual(bridges[0]["port"], name)
        self.assertEqual(bridges[0]["member_port"], "Gi0/16")

    def test_a_port_that_is_no_aggregate_has_no_member(self):
        sw, _index, _name = self.switch()
        sw.lldp_neighbors = [LldpNeighbor(
            local_ifindex=7, local_port_num=7, chassis_id=BOX,
            chassis_subtype=4, cap_enabled={"bridge"}, cap_known=True,
            sys_name="RouterOS-C",
        )]
        bridges, _others, _unidentified = detect_bridges([sw], set(), {})
        self.assertEqual(bridges[0]["port"], "Gi0/7")
        self.assertEqual(bridges[0]["member_port"], "")

    def test_a_bridge_takes_over_the_pseudo_switch_and_its_devices(self):
        sw, index, name = self.switch()
        for mac in DEVICES:
            sw.fdb[mac] = index
        sw.lldp_neighbors = [
            neighbour(BOX, {"bridge"}, sys_name="RouterOS-C",
                      mgmt_ip="10.1.5.6", mgmt_ips=["10.1.5.6"]),
        ]
        topo = build(sw)
        self.assertEqual(topo["pseudo"], [])
        [bridge] = topo["bridges"]
        self.assertEqual((bridge["switch"], bridge["port"]), (SWITCH, name))
        self.assertFalse(bridge["shares_port"])
        self.assertEqual(bridge["host_count"], len(DEVICES))
        for mac in DEVICES:
            self.assertEqual(topo["hosts"][mac]["port"], name)
            self.assertEqual(topo["hosts"][mac]["via"], bridge["id"])

    def test_a_bridge_in_the_table_of_its_aggregate_is_one_node(self):
        sw, index, _name = self.switch()
        sw.fdb[BOX] = index
        sw.lldp_neighbors = [
            neighbour(BOX, {"bridge"}, sys_name="RouterOS-C"),
        ]
        topo = build(sw)
        self.assertEqual(topo["hosts"][BOX]["merged_into"], "bridge:" + BOX)
        self.assertEqual(topo["bridges"][0]["mac"], BOX)

    def test_a_non_bridge_gives_its_name_to_its_host_on_the_aggregate(self):
        sw, index, name = self.switch()
        sw.fdb[BOX] = index
        sw.lldp_neighbors = [
            neighbour(BOX, {"router"}, sys_name="MikroTik",
                      port_id="VLAN_MGT", mgmt_ip="10.1.1.1",
                      mgmt_ips=["10.1.1.1", "10.1.0.1"]),
        ]
        topo = build(sw)
        self.assertEqual(topo["bridges"], [])
        host = topo["hosts"][BOX]
        self.assertEqual(host["port"], name)
        self.assertEqual(host["lldp"]["sys_name"], "MikroTik")
        self.assertEqual(host["lldp"]["port_id"], "VLAN_MGT")
        self.assertEqual(host["lldp"]["kind"], "router")
        self.assertEqual(host["lldp"]["mgmt_ips"], ["10.1.1.1", "10.1.0.1"])

    def test_an_aggregate_that_leaves_the_network_takes_its_bridge(self):
        sw, index, name = self.switch()
        sw.fdb[BOX] = index
        sw.fdb[DEVICES[0]] = index
        sw.lldp_neighbors = [
            neighbour(BOX, {"bridge"}, sys_name="CE6851"),
        ]
        topo = build(sw, uplink_ports={(SWITCH, name)})
        [bridge] = topo["bridges"]
        self.assertTrue(bridge["external"])
        [external] = topo["info"]["external_networks"]
        self.assertEqual(external["via"], bridge["id"])
        # the bridge is the boundary, not one of the devices behind it
        self.assertEqual(external["count"], 1)


class SyntheticAggregateTest(AggregateKinds, unittest.TestCase):
    kind = "synthetic"

    def test_a_member_in_the_fdb_is_counted_on_the_synthetic_port(self):
        # the agent hands the trunk's table on the synthetic port; a
        # row on a member resolves to it all the same
        sw, index, _name = self.switch()
        sw.fdb[DEVICES[0]] = 16
        sw.fdb[DEVICES[1]] = index
        self.assertEqual(
            normalized_fdb([sw])[SWITCH],
            {DEVICES[0]: SYNTHETIC, DEVICES[1]: SYNTHETIC},
        )


class IeeeAggregateTest(AggregateKinds, unittest.TestCase):
    kind = "ieee"

    def test_a_member_in_the_fdb_is_counted_on_the_aggregate(self):
        sw, _index, _name = self.switch()
        sw.fdb[DEVICES[0]] = 15
        sw.fdb[DEVICES[1]] = 16
        self.assertEqual(
            normalized_fdb([sw])[SWITCH],
            {DEVICES[0]: AGGREGATE, DEVICES[1]: AGGREGATE},
        )


class BeforeTheFixTest(unittest.TestCase):
    """What the member name did — so the tests above test something."""

    def test_the_member_and_the_aggregate_are_different_ports(self):
        sw, index, name = access_switch("synthetic")
        self.assertNotEqual(name, sw.ports[16].name)
        sw.fdb[BOX] = index
        topo = build(sw)
        self.assertEqual(topo["hosts"][BOX]["port"], name)


if __name__ == "__main__":
    unittest.main()
