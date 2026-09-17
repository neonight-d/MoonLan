"""SNMP settings a switch carries of its own.

`snmp:` is one compromise for the whole network. Five seconds with two
retries suits a D-Link; on an RB941 it means every request the agent
does not answer costs fifteen seconds, and one poll holds thirteen of
them. The only way to account for that used to be to make the setting
worse for every switch at once.

The old `switches:` — a plain list of addresses — is the form every
config.yaml in existence uses, and it has to keep working exactly as it
did. That is most of what is tested here.

Run with:  python -m unittest discover -s tests
"""

import unittest

from moonlan.config import Config, HostSnmp, SnmpConfig, parse_switches


class PlainListTest(unittest.TestCase):
    """The format every config.yaml already has."""

    def test_addresses_only(self):
        addresses, settings, problems = parse_switches(
            ["10.0.0.10", "10.0.0.21"], SnmpConfig()
        )
        self.assertEqual(addresses, ["10.0.0.10", "10.0.0.21"])
        self.assertEqual(problems, [])
        for ip in addresses:
            self.assertEqual(settings[ip].explicit, frozenset())
            self.assertEqual(settings[ip].timeout, SnmpConfig.timeout)
            self.assertEqual(settings[ip].community, SnmpConfig.community)

    def test_empty_and_missing(self):
        self.assertEqual(parse_switches(None, SnmpConfig()), ([], {}, []))
        self.assertEqual(parse_switches([], SnmpConfig()), ([], {}, []))

    def test_a_single_address_is_not_a_list_of_characters(self):
        addresses, _, _ = parse_switches("10.0.0.10", SnmpConfig())
        self.assertEqual(addresses, ["10.0.0.10"])


class OverridesTest(unittest.TestCase):
    def setUp(self):
        self.defaults = SnmpConfig(
            community="global", timeout=8, retries=2,
            retries_on_break=2, host_budget_seconds=120,
        )

    def test_unnamed_keys_are_inherited(self):
        _, settings, problems = parse_switches(
            [
                "10.0.0.10",
                {"ip": "10.3.6.2", "timeout": 2, "retries": 1,
                 "host_budget_seconds": 90},
                {"ip": "10.3.7.15", "community": "OtherString"},
            ],
            self.defaults,
        )
        self.assertEqual(problems, [])
        slow = settings["10.3.6.2"]
        self.assertEqual((slow.timeout, slow.retries), (2, 1))
        self.assertEqual(slow.host_budget_seconds, 90)
        # not named for this switch, so it comes from snmp:
        self.assertEqual(slow.community, "global")
        self.assertEqual(slow.retries_on_break, 2)
        self.assertEqual(
            slow.explicit,
            {"timeout", "retries", "host_budget_seconds"},
        )
        self.assertEqual(settings["10.3.7.15"].community, "OtherString")
        self.assertEqual(settings["10.3.7.15"].timeout, 8)
        # and the plain address next to them is untouched
        self.assertEqual(settings["10.0.0.10"].explicit, frozenset())

    def test_a_bad_entry_is_reported_rather_than_dropped(self):
        """Silence would leave the operator sure a setting is in force.

        Three ways to get this wrong, all of them worth a line: a
        mapping with no address at all, a key nobody implements, and a
        timeout that is not a number.
        """
        addresses, settings, problems = parse_switches(
            [
                {"timeout": 3},
                {"ip": "10.0.0.9", "timout": 3},
                {"ip": "10.0.0.8", "timeout": "soon"},
            ],
            self.defaults,
        )
        self.assertEqual(addresses, ["10.0.0.9", "10.0.0.8"])
        self.assertEqual(len(problems), 3)
        self.assertTrue(any("no ip" in p for p in problems))
        self.assertTrue(any("timout" in p for p in problems))
        self.assertTrue(any("not a number" in p for p in problems))
        # the unusable value falls back rather than crashing the load
        self.assertEqual(settings["10.0.0.8"].timeout, 8)
        self.assertEqual(settings["10.0.0.8"].explicit, frozenset())


class ConfigLookupTest(unittest.TestCase):
    def test_an_address_nobody_configured_gets_the_global_section(self):
        cfg = Config()
        cfg.snmp = SnmpConfig(community="global", timeout=8)
        cfg.switches = ["10.0.0.10"]
        cfg.switch_snmp = {
            "10.0.0.10": HostSnmp(
                community="global", timeout=2, retries=1,
                retries_on_break=2, host_budget_seconds=90,
                explicit=frozenset({"timeout", "retries",
                                    "host_budget_seconds"}),
            )
        }
        self.assertEqual(cfg.host_budget("10.0.0.10"), 90)
        # a router, or an address typed into diag
        self.assertEqual(cfg.host_snmp("10.0.0.1").timeout, 8)
        self.assertEqual(cfg.host_budget("10.0.0.1"), 120)
        self.assertEqual(cfg.custom_switches(), ["10.0.0.10"])


if __name__ == "__main__":
    unittest.main()
