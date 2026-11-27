"""What _build_map does to the world, and in which order, frozen (v0.8.14).

Stage 5 of taking server.py apart cuts _build_map into steps
(docs/ARCHITECTURE.md). Whatever the steps, the map has to be built
with the same calls in the same order. Each scenario of
tests/build_map_effects.py runs in a process of its own, and its record
— every call of a method of rt.db, rt.alarm_engine and rt.state, every
line of the log, every field of rt assigned, and the state at the end —
is held to tests/fixtures/build-map-effects/<scenario>.jsonl, written
before the cut.

The fixtures are written only on request:

    MOONLAN_RECORD_EFFECTS=1 python -m unittest tests.test_build_map_effects

A plain run, and CI, compare. On a difference the test shows the first
event that differs, as the fixture has it and as this run made it.

Run with:  python -m unittest discover -s tests
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import build_map_effects as effects  # noqa: E402

FIXTURES = HERE / "fixtures" / "build-map-effects"
RECORD = os.environ.get("MOONLAN_RECORD_EFFECTS") == "1"


def run(scenario: str) -> list[dict]:
    """The scenario's events, whole, from a process of its own"""
    with tempfile.TemporaryDirectory() as folder:
        out = Path(folder) / "events.jsonl"
        env = {**os.environ, "PYTHONHASHSEED": "0", "TZ": "UTC",
               "PYTHONPATH": str(HERE.parent)}
        env.pop("MOONLAN_CONFIG", None)
        env.pop("MOONLAN_DEMO", None)
        done = subprocess.run(
            [sys.executable, str(HERE / "build_map_effects.py"), scenario,
             folder, str(out)],
            cwd=folder, env=env, capture_output=True, text=True, timeout=300,
        )
        if done.returncode:
            raise AssertionError(f"{scenario} failed:\n{done.stderr[-4000:]}")
        return [json.loads(line)
                for line in out.read_text(encoding="utf-8").splitlines()]


def where(events: list[dict], index: int) -> str:
    """The step an event belongs to: the last marker before it"""
    for event in reversed(events[:index]):
        if event["kind"] == "step" and event["name"] != "_build_map":
            return event["name"]
    return "the start"


class BuildMapEffectsTest(unittest.TestCase):
    def test_every_scenario_does_what_it_did(self):
        for scenario in effects.SCENARIOS:
            with self.subTest(scenario=scenario):
                events = run(scenario)
                lines = [effects.text(effects.compact(e)) for e in events]
                fixture = FIXTURES / f"{scenario}.jsonl"
                if RECORD:
                    FIXTURES.mkdir(parents=True, exist_ok=True)
                    fixture.write_text("\n".join(lines) + "\n", encoding="utf-8")
                    continue
                self.assertTrue(fixture.is_file(), f"no fixture for {scenario}")
                kept = fixture.read_text(encoding="utf-8").splitlines()
                for index, (old, new) in enumerate(zip(kept, lines)):
                    if old != new:
                        self.fail(
                            f"{scenario}: event {index + 1} differs "
                            f"(in {where(events, index)})\n"
                            f"  fixture: {old[:1500]}\n"
                            f"  now:     {new[:1500]}\n"
                            f"  whole:   {effects.text(events[index])[:4000]}"
                        )
                self.assertEqual(
                    len(lines), len(kept),
                    f"{scenario}: {len(lines)} events now, {len(kept)} in the "
                    f"fixture; the first one past the shorter: "
                    f"{(lines + kept)[min(len(lines), len(kept))][:1500]}",
                )


if __name__ == "__main__":
    unittest.main()
