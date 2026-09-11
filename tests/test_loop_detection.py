"""Loop detection parsed off the values the real switches returned.

The fixtures in tests/fixtures/ are slices of
docs/diag/walk-*-full-2026-09-11.txt: the loop-detection branch of a
DGS-1210-26 Rev.F1, a DES-1210-28/ME and a DES-3526, exactly as those
agents answered. Their "LBD on this port" column was checked against
the operator's own exclusion list before any of it was written into a
profile, and the same lists are asserted here.

The rule under test is the one that cannot be verified against live
hardware: only "no loop" has ever been observed on these models, so
anything else has to read as a loop. A test with a value nobody has
seen is the only place that rule can be proved.

Run with:  python -m unittest discover -s tests
"""

import asyncio
import unittest
from pathlib import Path

from moonlan import loopdetect
from moonlan.loopdetect import (
    BUILTIN_PROFILES,
    LoopPortState,
    collect_loop_detection,
    judge_port,
    map_ports,
    parse_profiles,
)
from moonlan.snmp_collector import PortInfo, WalkStatus

FIXTURES = Path(__file__).resolve().parent / "fixtures"

DGS1210 = "1.3.6.1.4.1.171.11.153.1000"
DES1210 = "1.3.6.1.4.1.171.10.75.15.2"
DES3526 = "1.3.6.1.4.1.171.11.64.1"


class Octets(str):
    """An OctetString the way pysnmp hands one over."""

    def asOctets(self) -> bytes:
        return self.encode("utf-8")


def load_fixture(name: str) -> dict[str, object]:
    """A diag walk dump -> {full OID: value}, types included."""
    rows: dict[str, object] = {}
    for line in (FIXTURES / name).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        oid, _, rest = line.partition("  (")
        kind, _, value = rest.partition(")")
        # an empty OctetString prints as "... (OctetString) =" with
        # nothing after it, so the value is taken by stripping rather
        # than by splitting on " = "
        value = value.lstrip(" =")
        rows[oid] = Octets(value) if kind == "OctetString" else int(value)
    return rows


class ReplayCollector:
    """Answers GETs and walks out of a captured subtree.

    Nothing here touches pysnmp: the point is the parser, and the rows
    it is given are the ones a real agent produced.
    """

    def __init__(self, rows: dict[str, object], missing: set[str] | None = None):
        self.rows = rows
        self.missing = missing or set()
        self.gets: list[str] = []
        self.walks: list[str] = []

    async def _get(self, host: str, oid: str):
        self.gets.append(oid)
        if oid in self.missing:
            return None
        return self.rows.get(oid)

    async def _walk(self, host: str, oid: str):
        self.walks.append(oid)
        prefix = oid + "."
        for full in sorted(
            (o for o in self.rows if o.startswith(prefix)),
            key=lambda o: [int(p) for p in o.split(".")],
        ):
            suffix = tuple(
                int(p) for p in full[len(prefix):].split(".")
            )
            yield suffix, self.rows[full]

    def last_walk_status(self, host: str, oid: str) -> WalkStatus:
        return WalkStatus(oid=oid, rows=1)


def ports(count: int) -> dict[int, PortInfo]:
    return {
        i: PortInfo(if_index=i, name=f"1/{i}", is_physical=True)
        for i in range(1, count + 1)
    }


class RealStructureTest(unittest.TestCase):
    """Both vendor layouts, read off the bytes they really sent."""

    def test_dgs_1210_26(self):
        rows = load_fixture("loop-dgs1210-26.txt")
        collector = ReplayCollector(rows)
        data = asyncio.run(collect_loop_detection(
            collector, "10.0.0.10", ports(26), list(BUILTIN_PROFILES),
            sys_object_id=DGS1210 + ".1",
        ))
        self.assertTrue(data.supported)
        self.assertEqual(data.profile, "dlink-1210")
        self.assertEqual(data.matched_by, "sysObjectID")
        self.assertTrue(data.enabled)
        self.assertEqual(data.mode, 1)          # port-based
        self.assertEqual(data.interval, 5)
        self.assertEqual(data.recover_time, 300)
        self.assertEqual(len(data.ports), 26)
        self.assertFalse(data.index_mismatch)
        # the operator's own exclusion list for this switch
        off = {i for i, p in data.ports.items() if not p.lbd_enabled}
        self.assertEqual(off, {1, 2, 3, 4, 5, 6, 7, 8, 21, 25, 26})
        self.assertEqual(data.looped_ports(), [])
        self.assertEqual(data.status, "ok")

    def test_des_1210_28_same_structure_other_root(self):
        rows = load_fixture("loop-des1210-28.txt")
        collector = ReplayCollector(rows)
        data = asyncio.run(collect_loop_detection(
            collector, "10.0.0.21", ports(28), list(BUILTIN_PROFILES),
            sys_object_id=DES1210,
        ))
        self.assertTrue(data.supported)
        self.assertEqual(data.profile, "dlink-1210")
        self.assertEqual(data.root, DES1210 + ".17")
        self.assertEqual(data.interval, 5)
        self.assertEqual(data.recover_time, 300)
        off = {i for i, p in data.ports.items() if not p.lbd_enabled}
        self.assertEqual(off, {17, 19, 25, 26, 27, 28})
        self.assertEqual(data.looped_ports(), [])

    def test_des_3526_string_status(self):
        rows = load_fixture("loop-des3526.txt")
        collector = ReplayCollector(rows)
        data = asyncio.run(collect_loop_detection(
            collector, "10.0.0.44", ports(26), list(BUILTIN_PROFILES),
            sys_object_id=DES3526,
        ))
        self.assertTrue(data.supported)
        self.assertEqual(data.profile, "dlink-des3526")
        self.assertTrue(data.enabled)
        self.assertEqual(data.interval, 5)
        self.assertEqual(data.recover_time, 300)
        off = {i for i, p in data.ports.items() if not p.lbd_enabled}
        self.assertEqual(off, {25, 26})
        # the status is a string here, and "None" is what fine looks like
        self.assertEqual(data.ports[1].status_raw, "None")
        self.assertEqual(data.looped_ports(), [])

    def test_a_model_with_no_profile_claims_nothing(self):
        # the HPE 1820 of the real network: it answers no branch at all
        collector = ReplayCollector({})
        data = asyncio.run(collect_loop_detection(
            collector, "10.0.0.17", ports(24), list(BUILTIN_PROFILES),
            sys_object_id="1.3.6.1.4.1.11.2.3.7.11.180",
        ))
        self.assertFalse(data.supported)
        self.assertEqual(data.status, "unsupported")
        self.assertEqual(data.ports, {})
        self.assertEqual(data.looped_ports(), [])


class UnknownValueIsALoopTest(unittest.TestCase):
    """The rule that cannot be checked against live hardware.

    A loop was never produced on this network on purpose, so the value
    these agents report during one is unknown. Reading an unknown value
    as "probably fine" would let the first real loop pass in silence.
    """

    def test_an_unseen_value_reads_as_a_loop(self):
        rows = load_fixture("loop-dgs1210-26.txt")
        # port 9 starts reporting something nobody has documented
        rows[DGS1210 + ".17.5.1.3.9"] = 7
        collector = ReplayCollector(rows)
        data = asyncio.run(collect_loop_detection(
            collector, "10.0.0.10", ports(26), list(BUILTIN_PROFILES),
            sys_object_id=DGS1210,
        ))
        looped = data.looped_ports()
        self.assertEqual([p.if_index for p in looped], [9])
        # and the raw value travels with it, so the first real loop
        # documents itself in the alarm text
        self.assertEqual(looped[0].status_raw, "7")
        self.assertEqual(data.status, "loop")

    def test_a_loop_on_an_unwatched_port_is_not_reported(self):
        rows = load_fixture("loop-dgs1210-26.txt")
        rows[DGS1210 + ".17.5.1.3.21"] = 3   # port 21 has LBD switched off
        collector = ReplayCollector(rows)
        data = asyncio.run(collect_loop_detection(
            collector, "10.0.0.10", ports(26), list(BUILTIN_PROFILES),
            sys_object_id=DGS1210,
        ))
        self.assertEqual(data.looped_ports(), [])

    def test_a_missing_status_is_unknown_not_normal(self):
        state = judge_port(LoopPortState(port=3), ("1",))
        self.assertIsNone(state.looped)
        self.assertFalse(state.known)
        state = judge_port(LoopPortState(port=3, status_raw="1"), ("1",))
        self.assertFalse(state.looped)
        self.assertTrue(state.known)


class PortMappingTest(unittest.TestCase):
    def test_row_count_mismatch_is_flagged(self):
        rows = {
            i: LoopPortState(port=i, lbd_enabled=True, status_raw="1")
            for i in range(1, 29)
        }
        mapped, unmapped, mismatch = map_ports(rows, set(range(1, 27)))
        self.assertTrue(mismatch)
        # the two rows with no interface behind them are not used
        self.assertEqual(unmapped, [27, 28])
        self.assertEqual(len(mapped), 26)

    def test_matching_tables_are_not_flagged(self):
        rows = {
            i: LoopPortState(port=i, lbd_enabled=True, status_raw="1")
            for i in range(1, 27)
        }
        mapped, unmapped, mismatch = map_ports(rows, set(range(1, 27)))
        self.assertFalse(mismatch)
        self.assertEqual(unmapped, [])
        self.assertEqual(len(mapped), 26)

    def test_a_mismatched_switch_reports_partial(self):
        rows = load_fixture("loop-dgs1210-26.txt")
        collector = ReplayCollector(rows)
        # the interface table says 24 ports, the vendor table says 26
        data = asyncio.run(collect_loop_detection(
            collector, "10.0.0.10", ports(24), list(BUILTIN_PROFILES),
            sys_object_id=DGS1210,
        ))
        self.assertTrue(data.index_mismatch)
        self.assertEqual(data.unmapped, [25, 26])
        self.assertEqual(data.status, "partial")


class ProfileSelectionTest(unittest.TestCase):
    def test_an_unknown_sys_object_id_still_finds_the_branch(self):
        """A model nobody has keyed yet is identified by its answer.

        sysObjectID is the declared way in, but a vendor branch is not
        something another vendor also implements: if it answers, the
        switch is that model. Without this, a firmware revision with a
        new sysObjectID would silently stop being watched.
        """
        rows = load_fixture("loop-des1210-28.txt")
        collector = ReplayCollector(rows)
        data = asyncio.run(collect_loop_detection(
            collector, "10.0.0.21", ports(28), list(BUILTIN_PROFILES),
            sys_object_id="1.3.6.1.4.1.171.10.999.1",
        ))
        self.assertTrue(data.supported)
        self.assertEqual(data.matched_by, "probe")
        self.assertEqual(data.root, DES1210 + ".17")

    def test_config_profile_replaces_a_builtin_by_name(self):
        extra, problems = parse_profiles([{
            "name": "dlink-1210",
            "roots": {"1.3.6.1.4.1.171.10.75.15.2":
                      "1.3.6.1.4.1.171.10.75.15.2.17"},
            "scalars": {"enabled": "1.0", "mode": "2.0",
                        "interval": "3.0", "recover_time": "4.0"},
            "columns": {"index": "5.1.1", "lbd_enabled": "5.1.2",
                        "status": "5.1.3"},
            "status_type": "integer",
            "normal": [1],
        }])
        self.assertEqual(problems, [])
        merged = loopdetect.merge_profiles(BUILTIN_PROFILES, extra)
        self.assertEqual(len(merged), len(BUILTIN_PROFILES))
        self.assertEqual(merged[0].name, "dlink-1210")
        self.assertEqual(len(merged[0].roots), 1)  # the override's, not both

    def test_a_broken_profile_is_reported_not_silently_dropped(self):
        extra, problems = parse_profiles([
            {"name": "no-roots"},
            {"roots": {"1.2": "1.2.3"}},
            "not a mapping",
        ])
        self.assertEqual(extra, [])
        self.assertEqual(len(problems), 3)


if __name__ == "__main__":
    unittest.main()
