"""The state of the process in one object, rt (v0.8.13).

server.py's mutable globals moved to moonlan/runtime.py: one Runtime,
made when that module is imported and never replaced, filled by
server.py at its own import, read as rt.X when the code runs. What is
left of the old names in server is a loud refusal: server.db = fake
would otherwise set an attribute nothing reads any more, and the test
that did it would pass checking nothing.

Run with:  python -m unittest discover -s tests
"""

import ast
import dataclasses
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from service_fixture import SWITCHES  # noqa: E402,F401

from moonlan import runtime, server  # noqa: E402

PACKAGE = Path(__file__).resolve().parent.parent / "moonlan"
HINT = "server.db moved to the runtime: use server.rt.db"

# Fields that stay None in the tests' configuration: no public address,
# no certificate, no demo; the last two are made when first needed
MAY_STAY_NONE = {"public_url", "certificate", "demo_counters",
                 "_loop_profiles", "_collector"}


class StrictModuleTest(unittest.TestCase):
    def test_reading_an_old_name_says_where_it_went(self):
        with self.assertRaises(AttributeError) as caught:
            server.db
        self.assertEqual(str(caught.exception), HINT)

    def test_writing_one_is_refused_and_changes_nothing(self):
        db = server.rt.db
        with self.assertRaises(AttributeError) as caught:
            server.db = object()
        self.assertEqual(str(caught.exception), HINT)
        with self.assertRaises(AttributeError) as caught:
            del server.db
        self.assertEqual(str(caught.exception), HINT)
        self.assertNotIn("db", vars(server))
        self.assertIs(server.rt.db, db)

    def test_patching_one_is_refused(self):
        with self.assertRaises(AttributeError):
            with mock.patch.object(server, "db", object()):
                pass
        # create=True gets past the read and stops at the write
        with self.assertRaises(AttributeError) as caught:
            with mock.patch.object(server, "db", object(), create=True):
                pass
        self.assertEqual(str(caught.exception), HINT)
        self.assertNotIn("db", vars(server))

    def test_every_field_is_refused(self):
        for name in runtime.FIELDS:
            with self.subTest(name):
                self.assertFalse(hasattr(server, name))
                with self.assertRaises(AttributeError) as caught:
                    setattr(server, name, None)
                self.assertIn(f"use server.rt.{name}", str(caught.exception))
                self.assertNotIn(name, vars(server))

    def test_other_names_are_as_before(self):
        original = server.get_collector
        with mock.patch.object(server, "get_collector", lambda: "patched"):
            self.assertEqual(server.get_collector(), "patched")
        self.assertIs(server.get_collector, original)
        with self.assertRaises(AttributeError) as caught:
            server.no_such_name
        self.assertEqual(
            str(caught.exception),
            "module 'moonlan.server' has no attribute 'no_such_name'",
        )


class RuntimeTest(unittest.TestCase):
    def test_one_object(self):
        self.assertIs(server.rt, runtime.rt)
        self.assertIsInstance(runtime.rt, runtime.Runtime)
        self.assertEqual(
            runtime.FIELDS,
            tuple(f.name for f in dataclasses.fields(runtime.Runtime)),
        )

    def test_every_field_is_filled_at_the_import_of_server(self):
        for name in runtime.FIELDS:
            with self.subTest(name):
                self.assertTrue(hasattr(server.rt, name))
                if name not in MAY_STAY_NONE:
                    self.assertIsNotNone(getattr(server.rt, name))

    def test_server_fills_them_in_their_order(self):
        """server.py assigns every field at module level, the first time
        in the order Runtime declares them — the order the globals had"""
        tree = ast.parse((PACKAGE / "server.py").read_text(encoding="utf-8"))
        filled = []
        for node in tree.body:
            targets = (node.targets if isinstance(node, ast.Assign) else
                       [node.target] if isinstance(node, ast.AnnAssign) else [])
            for target in targets:
                for sub in ast.walk(target):
                    if (isinstance(sub, ast.Attribute)
                            and isinstance(sub.value, ast.Name)
                            and sub.value.id == "rt" and sub.attr not in filled):
                        filled.append(sub.attr)
        self.assertEqual(tuple(filled), runtime.FIELDS)

    def test_rt_is_never_replaced(self):
        made = []
        for path in sorted(PACKAGE.rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, (ast.Global, ast.Nonlocal)) and "rt" in node.names:
                    made.append(f"{path.name}:{node.lineno} global rt")
                targets = (node.targets if isinstance(node, ast.Assign) else
                           [node.target] if isinstance(node, (ast.AnnAssign, ast.AugAssign)) else [])
                for target in targets:
                    if (isinstance(target, ast.Name) and target.id == "rt") or (
                            isinstance(target, ast.Attribute) and target.attr == "rt"):
                        made.append(f"{path.name}:{node.lineno}")
        self.assertEqual(made, [f"runtime.py:{self._line_of_rt()}"])

    def _line_of_rt(self) -> int:
        lines = (PACKAGE / "runtime.py").read_text(encoding="utf-8").splitlines()
        return lines.index("rt = Runtime()") + 1


if __name__ == "__main__":
    unittest.main()
