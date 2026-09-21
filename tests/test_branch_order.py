"""A branch is a chain, and LLDP is what says so.

Behind port 1/28 of mb1 hang three boxes: RouterOS-Garage,
RouterOS-Workshop and an Edge-Core. LanTopoLog draws them as a garland
— Garage → Workshop → Edge-Core. MoonLan drew all three straight onto
mb1 and then laid the one LLDP edge it had (Workshop ↔ Edge-Core) on
top, which closed a ring that does not exist.

Neither half was a bug on its own. `attach()` gives up on ordering a
branch when the MAC tables are too thin to say who sees whom — and on
RouterOS they are very thin — and says so in the log before connecting
everything to the parent. `merge_lldp_links` could refine ports and add
a missing pair, but never remove the link that pair contradicts.

The order of the two was the bug. A MAC table says a device is
*reachable through* a port; LLDP says a device is *on the cable*. The
second is the stronger statement, and it was arriving after the
weaker one had already drawn the picture.

Run with:  python -m unittest discover -s tests
"""

import unittest

from moonlan.lldp import LldpNeighbor
from moonlan.snmp_collector import PortInfo, SwitchData
from moonlan.topology import build_topology

MB1 = "10.0.0.21"
GARAGE = "10.3.6.4"
WORKSHOP = "10.3.6.2"
EDGE = "10.3.6.5"
CORE = "10.0.0.10"


def switch(ip: str, name: str, octet: int, ports) -> SwitchData:
    sw = SwitchData(
        ip=ip, reachable=True, sys_name=name,
        bridge_mac=f"02:00:00:00:00:{octet:02x}",
    )
    sw.own_macs = {sw.bridge_mac}
    for if_index, port_name in ports.items():
        sw.ports[if_index] = PortInfo(
            if_index=if_index, name=port_name, oper_up=True, speed_mbps=1000
        )
    return sw


def neighbor(local: int, chassis: str, matched_by: str = "fdb"):
    return LldpNeighbor(
        local_ifindex=local, local_port_num=local,
        port_matched_by=matched_by, chassis_id=chassis,
        chassis_subtype=4, cap_enabled={"bridge"}, cap_known=True,
    )


def segment(with_lldp: bool = True) -> list[SwitchData]:
    """The Workshop segment as the live network reports it.

    mb1 holds all three boxes in the FDB of one port, which is true —
    they are all reachable through it. The three of them barely fill
    their own tables, which is why the order cannot be read out of
    them. Only Workshop and Edge-Core see each other over LLDP.
    """
    core = switch(CORE, "comm-mb0", 1, {i: f"Gi0/{i}" for i in range(1, 27)})
    mb1 = switch(MB1, "comm.mb1", 2, {i: f"1/{i}" for i in range(1, 29)})
    garage = switch(GARAGE, "RouterOS-Garage", 3,
                    {1: "ether1", 2: "ether2", 3: "ether3"})
    workshop = switch(WORKSHOP, "RouterOS-Workshop", 4,
                      {1: "ether1", 2: "ether2", 3: "ether3"})
    edge = switch(EDGE, "ES3528M", 5,
                  {2: "Port2", 25: "Port25", 26: "Port26"})

    # The core is the root: it sees everybody, on port 1 toward mb1
    for sw in (mb1, garage, workshop, edge):
        core.fdb[sw.bridge_mac] = 1
    mb1.fdb[core.bridge_mac] = 25
    # …and mb1 holds all three of them on 1/28. True, and not an order.
    for sw in (garage, workshop, edge):
        mb1.fdb[sw.bridge_mac] = 28
    # The three boxes know almost nothing: each has only the way out.
    garage.fdb[core.bridge_mac] = 1
    workshop.fdb[core.bridge_mac] = 1
    edge.fdb[core.bridge_mac] = 25

    if with_lldp:
        # Workshop names Edge-Core on ether2. Its uplink is ether1 —
        # that is the port the rest of the network is behind — so
        # Edge-Core is further out than Workshop is.
        workshop.lldp_neighbors = [neighbor(2, edge.bridge_mac)]
        edge.lldp_neighbors = [neighbor(25, workshop.bridge_mac)]
    return [core, mb1, garage, workshop, edge]


def build(switches):
    return build_topology(switches, unmanaged_threshold=0)


def pairs(links) -> set[frozenset]:
    return {frozenset({link["a"], link["b"]}) for link in links}


class WorkshopSegmentTest(unittest.TestCase):
    def test_edge_core_is_behind_workshop_not_beside_it(self):
        with self.assertLogs("moonlan.topology", "INFO") as logged:
            _sw, links, *_rest = build(segment())
        drawn = pairs(links)
        self.assertIn(frozenset({WORKSHOP, EDGE}), drawn)
        # the ring's third side: mb1 never gets a cable to the Edge-Core
        self.assertNotIn(frozenset({MB1, EDGE}), drawn)
        self.assertTrue(
            any("is behind" in line and EDGE in line for line in logged.output),
            logged.output,
        )

    def test_there_is_no_ring(self):
        _sw, links, *_rest = build(segment())
        switches = {CORE, MB1, GARAGE, WORKSHOP, EDGE}
        among = [
            link for link in links
            if link["a"] in switches and link["b"] in switches
        ]
        # a tree over five nodes has four edges, and any fifth is a ring
        self.assertEqual(len(among), 4, [(l["a"], l["b"]) for l in among])

    def test_without_lldp_it_is_the_old_star_and_says_so(self):
        """No LLDP, no order — but nothing worse than before either.

        The three still hang off mb1, the log still says the order is
        undetermined, and the links are marked so the map can draw
        them as the guess they are.
        """
        with self.assertLogs("moonlan.topology", "WARNING") as logged:
            _sw, links, *_rest = build(segment(with_lldp=False))
        drawn = pairs(links)
        self.assertIn(frozenset({MB1, EDGE}), drawn)
        self.assertIn(frozenset({MB1, WORKSHOP}), drawn)
        self.assertTrue(
            any("undetermined" in line for line in logged.output),
            logged.output,
        )
        guessed = [link for link in links if link.get("order_unknown")]
        self.assertEqual(
            {frozenset({link["a"], link["b"]}) for link in guessed},
            {frozenset({MB1, ip}) for ip in (GARAGE, WORKSHOP, EDGE)},
        )

    def test_the_whole_garland_when_lldp_covers_both_hops(self):
        """What LanTopoLog draws: mb1 → Garage → Workshop → Edge-Core.

        One more LLDP pair is all it takes. No MAC table on any of the
        three boxes has to know anything about the others.
        """
        switches = segment()
        by_ip = {sw.ip: sw for sw in switches}
        garage, workshop = by_ip[GARAGE], by_ip[WORKSHOP]
        garage.lldp_neighbors = [neighbor(2, workshop.bridge_mac)]
        workshop.lldp_neighbors.append(neighbor(1, garage.bridge_mac))
        _sw, links, *_rest = build(switches)
        drawn = pairs(links)
        self.assertEqual(
            drawn & {
                frozenset({MB1, GARAGE}), frozenset({GARAGE, WORKSHOP}),
                frozenset({WORKSHOP, EDGE}),
            },
            {
                frozenset({MB1, GARAGE}), frozenset({GARAGE, WORKSHOP}),
                frozenset({WORKSHOP, EDGE}),
            },
        )
        self.assertNotIn(frozenset({MB1, WORKSHOP}), drawn)
        self.assertNotIn(frozenset({MB1, EDGE}), drawn)
        self.assertFalse([link for link in links if link.get("order_unknown")])

    def test_the_rest_of_the_network_is_untouched(self):
        _sw, links, *_rest = build(segment())
        self.assertIn(frozenset({CORE, MB1}), pairs(links))


if __name__ == "__main__":
    unittest.main()
