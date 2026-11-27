"""The steps of the map: each reads what an earlier one wrote (v0.8.14).

poller._build_map calls the steps of mapping/build.py one after the
other, and what one step hands another is a field of MapPass. Read off
the AST of the orchestrator and the steps:

- a step reads p.X only after an earlier step, or the step itself, has
  written it, or X is what the pass got;
- the fields written by more than one step are a list here, so that a
  new one shows in review;
- no field is written for nobody to read, and MapPass has exactly the
  fields the steps hand each other;
- the steps are defined in build.py in the order they are called.

What the steps do to the world, and in which order, is
tests/test_build_map_effects.py.

Run with:  python -m unittest discover -s tests
"""

import ast
import dataclasses
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POLLER = ROOT / "moonlan" / "poller.py"
BUILD = ROOT / "moonlan" / "mapping" / "build.py"

ORCHESTRATOR = "_build_map"
INPUTS = {"collected", "arp", "observe", "identities"}
# the fields more than one step writes; "(input)" is what the pass got
REWRITTEN = {
    "identities": ["(input)", "vouch_for_routers"],
    "db_rows": ["weigh_addresses", "settle_unconfirmed"],
    "hosts": ["infer_topology", "add_stub_switches", "settle_unconfirmed",
              "assemble_hosts"],
    "switches": ["infer_topology", "add_stub_switches"],
}


def steps_of(source: str) -> dict[str, ast.AST]:
    return {
        node.name: node for node in ast.parse(source).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def orchestration(source: str) -> list[tuple[str, object]]:
    """The orchestrator in order: ("step", name) for each build.<step>(p),
    ("own", statement) for its own lines; MapPass(...) itself left out"""
    function = steps_of(source)[ORCHESTRATOR]
    order = []
    for statement in function.body[1:]:  # past the docstring
        call = statement.value if isinstance(statement, ast.Expr) else None
        if isinstance(call, ast.Await):
            call = call.value
        if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                and ast.unparse(call.func.value) == "build"):
            order.append(("step", call.func.attr))
        elif not (isinstance(statement, ast.Assign)
                  and ast.unparse(statement.targets[0]) == "p"):
            order.append(("own", statement))
    return order


def accesses(node) -> list[tuple[str, str]]:
    """(field, "R" or "W") for every p.<field> in node, in the order
    they happen: the value of an assignment before its targets"""
    found = []

    def visit(n):
        if isinstance(n, ast.Assign):
            visit(n.value)
            for target in n.targets:
                visit(target)
            return
        if isinstance(n, ast.AugAssign):
            if isinstance(n.target, ast.Attribute):
                visit(n.target.value)
                found.append((n.target.attr, "R"))
            visit(n.value)
            visit(n.target)
            return
        if isinstance(n, (ast.For, ast.AsyncFor)):
            visit(n.iter)
            visit(n.target)
            for child in n.body + n.orelse:
                visit(child)
            return
        if (isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                and n.value.id == "p"):
            found.append((n.attr, "W" if isinstance(n.ctx, ast.Store) else "R"))
            return
        for child in ast.iter_child_nodes(n):
            visit(child)

    visit(node)
    return found


def flow(order, steps) -> tuple[list[str], dict[str, list[str]], set[str]]:
    """(problems, field -> who writes it, fields read)"""
    problems = []
    written = set(INPUTS)
    writers = {name: ["(input)"] for name in INPUTS}
    read = set()
    for kind, item in order:
        name = item if kind == "step" else ORCHESTRATOR
        node = steps[item] if kind == "step" else item
        here = set()
        for field, how in accesses(node):
            if how == "R":
                read.add(field)
                message = f"{name} reads p.{field} before anyone writes it"
                if field not in written | here and message not in problems:
                    problems.append(message)
            else:
                here.add(field)
                if name not in writers.setdefault(field, []):
                    writers[field].append(name)
        written |= here
    for field in sorted(set(writers) - read):
        problems.append(f"p.{field} is written by {writers[field]} and read by nobody")
    return problems, writers, read


class StepFlowTest(unittest.TestCase):
    def setUp(self):
        self.steps = steps_of(BUILD.read_text(encoding="utf-8"))
        self.order = orchestration(POLLER.read_text(encoding="utf-8"))

    def test_each_step_reads_what_an_earlier_one_wrote(self):
        problems, writers, read = flow(self.order, self.steps)
        self.assertEqual(problems, [])
        self.assertEqual(
            {f: w for f, w in writers.items() if len(w) > 1}, REWRITTEN
        )
        from moonlan.mapping.build import MapPass
        self.assertEqual(
            {f.name for f in dataclasses.fields(MapPass)}, set(writers) | read
        )

    def test_every_step_once_in_the_order_of_build_py(self):
        called = [item for kind, item in self.order if kind == "step"]
        self.assertEqual(called, list(self.steps))
        self.assertEqual(len(called), 21)

    def test_the_check_catches_a_step_out_of_place(self):
        moved = [entry for entry in self.order if entry != ("step", "publish")]
        moved.insert(moved.index(("step", "assemble_hosts")), ("step", "publish"))
        problems, _, _ = flow(moved, self.steps)
        self.assertIn("publish reads p.offline_groups before anyone writes it",
                      problems)
        early = [entry for entry in self.order if entry != ("step", "infer_topology")]
        early.append(("step", "infer_topology"))
        problems, _, _ = flow(early, self.steps)
        self.assertIn("add_stub_switches reads p.switches before anyone writes it",
                      problems)

    def test_the_check_catches_a_field_nobody_reads(self):
        extra = ast.parse("def stray(p):\n    p.nothing = 1\n").body[0]
        problems, writers, _ = flow(self.order + [("step", "stray")],
                                    {**self.steps, "stray": extra})
        self.assertEqual(
            problems, ["p.nothing is written by ['stray'] and read by nobody"]
        )


if __name__ == "__main__":
    unittest.main()
