"""`port_prefix`: what all physical ports of a switch have before their
number (v0.7.10). With "numbers only" the map shows the number alone
on a switch where it names one port; where it would name two, there is
no prefix and the names stay."""

import logging
import unittest

from moonlan import demo
from moonlan.snmp_collector import PortInfo, SwitchData
from moonlan.topology import build_topology, port_prefix


def switch(*names, others=()):
    """Physical ports by name, then non-physical ones: (if_index, name)"""
    sw = SwitchData(ip="10.0.0.1", reachable=True, sys_name="sw")
    for if_index, name in enumerate(names, start=1):
        sw.ports[if_index] = PortInfo(if_index=if_index, name=name)
    for if_index, name in others:
        sw.ports[if_index] = PortInfo(if_index=if_index, name=name,
                                      is_physical=False)
    return sw


class PortPrefixTest(unittest.TestCase):
    def test_one_beginning(self):
        for names, prefix in (
            ([f"GigabitEthernet1/0/{n}" for n in range(1, 25)], "GigabitEthernet1/0/"),
            ([f"ether{n}" for n in range(1, 6)], "ether"),
            ([f"1/{n}" for n in range(1, 29)], "1/"),
            ([f"Slot0/{n}" for n in range(1, 27)], "Slot0/"),
        ):
            with self.subTest(prefix):
                self.assertEqual(port_prefix(switch(*names)), prefix)

    def test_a_number_that_would_name_two_ports(self):
        # a stack: "1/5" and "2/5" would both be "5"
        stack = [f"1/{n}" for n in range(1, 25)] + [f"2/{n}" for n in range(1, 25)]
        self.assertIsNone(port_prefix(switch(*stack)))
        # a MikroTik: "ether1" and "sfp-sfpplus1" would both be "1"
        mikrotik = [f"ether{n}" for n in range(1, 6)] + ["sfp-sfpplus1"]
        self.assertIsNone(port_prefix(switch(*mikrotik)))

    def test_a_name_without_a_number(self):
        self.assertIsNone(port_prefix(switch("ether1", "ether2", "mgmt")))

    def test_no_physical_ports(self):
        self.assertIsNone(port_prefix(switch(others=((100, "Po1"),))))

    def test_what_is_not_physical_does_not_count(self):
        sw = switch(*[f"ether{n}" for n in range(1, 6)],
                    others=((100, "Po1"), (200, "Vlan1"), (-3, "LAG ether3+ether4"),
                            (300, "bridge")))
        self.assertEqual(port_prefix(sw), "ether")

    def test_on_the_map(self):
        logging.disable(logging.WARNING)
        try:
            switches, *_ = build_topology(demo.demo_network())
        finally:
            logging.disable(logging.NOTSET)
        prefixes = {sw["name"]: sw["port_prefix"] for sw in switches}
        self.assertEqual(prefixes["core-sw"], "Gi0/")
        # access-sw-2 has an SFP port with a name of its own (v0.7.9)
        self.assertIsNone(prefixes["access-sw-2"])


if __name__ == "__main__":
    unittest.main()
