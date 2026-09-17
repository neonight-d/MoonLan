"""Four neighbours out of four, placed on no port at all.

    10.3.6.2 LLDP: 4 of 4 neighbours sit on a local port that could not
    be matched to an interface — not used for links

Both RouterOS boxes said this every scan. `match_local_port` tries the
switch's own port table, then the forwarding table, then the port
number as an ifIndex, and on an RB941 none of the three fired. The
fixture is that device's answer (tests/fixtures/lldp-routeros-rb941.txt),
and it shows why:

- `lldpRemLocalPortNum` is 0 on every row. Not a bridge-port number,
  not an ifIndex, not anything: the remote table does not say which
  local port a neighbour arrived on;
- `lldpRemChassisId` says subtype macAddress and then hands over
  seventeen bytes of ASCII rather than six octets. Taken at its word
  that is a string, and a string is not in anyone's MAC table — which
  is exactly why the forwarding table, which does know all four of
  these addresses, never got the chance to place them.

A MAC written out as text is still a MAC. That one line puts all four
back on their ports.

Run with:  python -m unittest discover -s tests
"""

import asyncio
import unittest
from pathlib import Path

from moonlan import lldp
from moonlan.lldp import (
    build_port_names,
    collect_lldp,
    mac_from_text,
    match_local_port,
    normalize_id,
)
from moonlan.snmp_collector import PortInfo, WalkStatus

FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures" / "lldp-routeros-rb941.txt"
)
HOST = "10.3.6.2"
OID_FDB = "1.3.6.1.2.1.17.4.3.1.2"
OID_BRIDGE_PORTS = "1.3.6.1.2.1.17.1.4.1.2"


class Octets(bytes):
    """An OctetString the way pysnmp hands one over."""

    def __str__(self):
        return self.decode("utf-8", "replace")


def load_rows() -> list[tuple[str, object]]:
    rows: list[tuple[str, object]] = []
    for line in FIXTURE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        oid, kind, value = line.split(" ", 2)
        if kind == "int":
            rows.append((oid, int(value)))
        elif kind == "hex":
            rows.append((oid, Octets(bytes.fromhex(value))))
        else:
            rows.append((oid, Octets(value.encode("utf-8"))))
    return rows


class FixtureCollector:
    """Serves the captured rows and nothing else."""

    def __init__(self, rows):
        self.rows = rows

    async def _walk(self, host, oid):
        base = oid + "."
        for full, value in self.rows:
            if full.startswith(base):
                suffix = tuple(
                    int(part) for part in full[len(base):].split(".")
                )
                yield suffix, value

    def last_walk_status(self, host, oid):
        return WalkStatus(oid=oid)


def fixture_maps(rows) -> tuple[dict[str, int], dict[int, int]]:
    """(MAC -> ifIndex, bridge-port -> ifIndex) out of the capture."""
    bridge_ports = {
        int(oid.rsplit(".", 1)[1]): int(value)
        for oid, value in rows
        if oid.startswith(OID_BRIDGE_PORTS + ".")
    }
    fdb: dict[str, int] = {}
    for oid, value in rows:
        if not oid.startswith(OID_FDB + "."):
            continue
        octets = [int(part) for part in oid[len(OID_FDB) + 1:].split(".")]
        mac = ":".join(f"{octet:02x}" for octet in octets)
        fdb[mac] = bridge_ports[int(value)]
    return fdb, bridge_ports


PORT_NAMES = {
    1: "ether1", 2: "ether2", 3: "ether3", 4: "ether4",
    5: "pwr-line1", 6: "wlan1", 8: "bridge",
}


class TextMacTest(unittest.TestCase):
    def test_a_mac_spelled_out_is_still_a_mac(self):
        self.assertEqual(
            mac_from_text("00:00:5E:00:53:20"), "00:00:5e:00:53:20"
        )
        self.assertEqual(
            mac_from_text("00-00-5E-00-53-20"), "00:00:5e:00:53:20"
        )
        self.assertEqual(
            normalize_id(4, b"00:00:5E:00:53:20", 4), "00:00:5e:00:53:20"
        )

    def test_text_that_is_not_an_address_is_left_alone(self):
        for text in ("bridge/ether2", "600", "", "Ethernet Port, port 25",
                     "00:00:5E:00:53", "zz:00:5e:00:53:20"):
            self.assertEqual(mac_from_text(text), "")
        self.assertEqual(normalize_id(5, b"bridge/ether2", 3),
                         "bridge/ether2")

    def test_six_real_octets_still_win(self):
        raw = bytes([0x00, 0x00, 0x5E, 0x00, 0x53, 0x20])
        self.assertEqual(normalize_id(4, raw, 4), "00:00:5e:00:53:20")


class BridgePortStepTest(unittest.TestCase):
    """lldpRemLocalPortNum read as a bridge-port number.

    It does not help this device — its port number is 0, which is not a
    bridge port either — but the number means one of the two on every
    agent, and asking the switch to resolve it beats assuming.
    """

    def test_bridge_port_is_tried_before_the_bare_ifindex(self):
        if_index, how = match_local_port(
            5, "", "", {}, {1, 2, 3, 4, 5, 6, 8},
            fdb_port=None, bridge_ports={1: 1, 2: 2, 3: 3, 4: 4, 5: 6},
        )
        self.assertEqual((if_index, how), (6, "bridge_port"))

    def test_it_is_skipped_when_the_number_is_not_a_bridge_port(self):
        if_index, how = match_local_port(
            8, "", "", {}, {1, 8}, fdb_port=None,
            bridge_ports={1: 1, 2: 2},
        )
        self.assertEqual((if_index, how), (8, "num"))

    def test_the_forwarding_table_still_outranks_it(self):
        if_index, how = match_local_port(
            5, "", "", {}, {1, 6}, fdb_port=1,
            bridge_ports={5: 6},
        )
        self.assertEqual((if_index, how), (1, "fdb"))


class RouterOsCaptureTest(unittest.TestCase):
    def setUp(self):
        self.rows = load_rows()
        self.collector = FixtureCollector(self.rows)
        self.fdb, self.bridge_ports = fixture_maps(self.rows)
        self.ports = {
            if_index: PortInfo(if_index=if_index, name=name)
            for if_index, name in PORT_NAMES.items()
        }

    def _collect(self):
        return asyncio.run(
            collect_lldp(
                self.collector, HOST,
                build_port_names(self.ports, {}),
                set(self.ports),
                self.fdb,
                self.bridge_ports,
            )
        )

    def test_the_capture_really_does_report_port_zero(self):
        """The premise, asserted rather than assumed."""
        numbers = {
            oid.split(".")[-2]
            for oid, _ in self.rows
            if oid.startswith("1.0.8802.1.1.2.1.4.1.1.5.")
        }
        self.assertEqual(numbers, {"0"})

    def test_every_neighbour_is_placed_by_the_forwarding_table(self):
        neighbors, _labels = self._collect()
        self.assertEqual(len(neighbors), 4)
        self.assertEqual(
            {n.port_matched_by for n in neighbors}, {"fdb"}
        )
        self.assertEqual(
            sorted(n.local_ifindex for n in neighbors), [1, 1, 1, 2]
        )
        self.assertFalse([n for n in neighbors if n.port_unmatched])

    def test_the_chassis_ids_come_out_as_addresses(self):
        neighbors, _labels = self._collect()
        self.assertEqual(
            sorted(n.chassis_id for n in neighbors),
            [
                "00:00:5e:00:53:20", "00:00:5e:00:53:21",
                "00:00:5e:00:53:22", "00:00:5e:00:53:23",
            ],
        )

    def test_without_the_fix_all_four_are_lost(self):
        """What the log said every scan, reproduced.

        The forwarding table is keyed by addresses; the chassis ids
        arrived as text. Nothing else in the row could place them.
        """
        original = lldp.mac_from_text
        lldp.mac_from_text = lambda text: ""
        try:
            neighbors, _labels = self._collect()
        finally:
            lldp.mac_from_text = original
        self.assertEqual(len(neighbors), 4)
        self.assertEqual(len([n for n in neighbors if n.port_unmatched]), 4)


if __name__ == "__main__":
    unittest.main()
