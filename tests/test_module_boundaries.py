"""The borders between server.py and the modules taken out of it.

server.py is taken apart in stages (docs/ARCHITECTURE.md): startup.py
and housekeeping.py in v0.8.12, the state of the process in
runtime.py, derived.py and the routes in routes/ in v0.8.13. A module
taken out of it never imports moonlan.server — not at the top, not
inside a function, not by name: that is a cycle. What such a module
needs from the process it reads from rt (runtime.py) when it runs, or
— stage 1 — it is given; never at its import, before server.py has
filled rt.

server.py keeps every name that moved and is called or read through
it — a stage 1 wrapper with the old signature, or the name re-exported
as it is — so nothing that calls server.<name> notices the move. And
the log keeps its name: "moonlan: …" in every line, as before.

Run with:  python -m unittest discover -s tests
"""

import ast
import importlib
import inspect
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from service_fixture import SWITCHES  # noqa: E402,F401

from moonlan import server  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "moonlan"

# The modules taken out of server.py; each next stage adds its own
NEW_MODULES = [
    "startup", "housekeeping",
    "runtime", "derived", "routes", "routes.common", "routes.system",
    "routes.settings", "routes.notes", "routes.layout", "routes.actions",
    "routes.alarms", "routes.topology",
]

# Every name they took that server.py keeps, and how: the signature the
# function had there ("async " for a coroutine) for a stage 1 wrapper,
# or None for a name re-exported as it is — a constant, or since stage 2
# a function, which reads rt itself and needs no wrapper
MOVED = {
    "derived": {
        "_uplink_ports": None,
        "_copies_window": None,
        "_subject_inbound_errors": None,
        "_effective_monitored": None,
        "_lag_groups": None,
        "_sum_known": None,
        "_rates_hide_age": None,
        "_round": None,
        "_loop_report": None,
    },
    "routes.system": {
        "WEB_DIR": None,
        "VERSIONED_ASSETS": None,
        "RevalidatedStaticFiles": None,
        "api_about": None,
    },
    "routes.notes": {
        "_stand_in": None,
        "_noted_node_ids": None,
        "_mac_directory": None,
    },
    "routes.layout": {
        "NodePosition": None,
        "Offset": None,
        "LayoutBody": None,
        "LAYOUT_EVENT_IDS": None,
        "_layout_anchors": None,
        "_layout_node_ids": None,
        "api_layout": None,
        "api_put_layout": None,
        "api_patch_positions": None,
        "api_delete_node_position": None,
        "api_clear_layout": None,
    },
    "routes.actions": {
        "ActionBody": None,
        "_node_directory": None,
        "api_node_menu": None,
        "api_start_action": None,
        "api_action_state": None,
    },
    "routes.alarms": {
        "api_journal": None,
    },
    "routes.topology": {
        "api_switch_ports": None,
        "_lldp_dict": None,
        "api_skipped_oids": None,
        "api_status": None,
    },
}


def _is_server(name: str) -> bool:
    return name == "moonlan.server" or name.startswith("moonlan.server.")


def server_imports(source: str, package: str = "moonlan") -> list[str]:
    """Every way the source — a module of `package` — imports
    moonlan.server, at any depth; [] when it does not. A relative import
    is resolved against the package: `from .. import server` is the
    server in moonlan/routes/, `from . import server` is not."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found += [f"import {alias.name}" for alias in node.names
                      if _is_server(alias.name)]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = package.split(".")
                if node.level > len(parts):
                    continue
                anchor = ".".join(parts[:len(parts) - node.level + 1])
                base = f"{anchor}.{base}" if base else anchor
            names = {alias.name for alias in node.names}
            if _is_server(base) or any(_is_server(f"{base}.{n}") for n in names):
                found.append(f"from {'.' * node.level}{node.module or ''} "
                             f"import {', '.join(sorted(names))}")
        elif isinstance(node, ast.Constant) and node.value in (
            "moonlan.server", ".server", "..server",
        ):
            found.append(f"{node.value!r} by name")
    return found


def path_of(name: str) -> Path:
    """moonlan/<name>.py, or the package's __init__.py"""
    path = PACKAGE.joinpath(*name.split("."))
    return path / "__init__.py" if path.is_dir() else path.with_suffix(".py")


def package_of(name: str) -> str:
    """The package a module's relative imports start from"""
    if path_of(name).name == "__init__.py":
        return f"moonlan.{name}"
    return ".".join(f"moonlan.{name}".split(".")[:-1])


def source(name: str) -> str:
    return path_of(name).read_text(encoding="utf-8")


def kept_signature(function) -> str:
    """What MOVED says of a function"""
    text = str(inspect.signature(function)).replace("'", "")
    return ("async " if inspect.iscoroutinefunction(function) else "") + text


def at_import(node):
    """The nodes evaluated when a module is imported: all of it but the
    bodies of functions and lambdas (their decorators and defaults are
    evaluated) and the imports themselves"""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        args = node.args
        heads = [d for d in args.defaults + args.kw_defaults if d is not None]
        for head in getattr(node, "decorator_list", []) + heads:
            yield from at_import(head)
        return
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return
    yield node
    for child in ast.iter_child_nodes(node):
        yield from at_import(child)


def rt_read_at_import(source: str) -> list[int]:
    return [
        node.lineno for node in at_import(ast.parse(source))
        if isinstance(node, ast.Name) and node.id == "rt"
        and isinstance(node.ctx, ast.Load)
    ]


class NoServerImportTest(unittest.TestCase):
    def test_no_new_module_imports_the_server(self):
        for name in NEW_MODULES:
            with self.subTest(module=name):
                self.assertEqual(server_imports(source(name), package_of(name)), [])

    def test_the_check_sees_every_way_in(self):
        for text, package in (
            ("import moonlan.server", "moonlan"),
            ("import moonlan.server as service", "moonlan"),
            ("from moonlan import server", "moonlan"),
            ("from moonlan import db, server", "moonlan"),
            ("from moonlan.server import db", "moonlan"),
            ("from .server import db", "moonlan"),
            ("from . import server", "moonlan"),
            ("def later():\n    from .server import config\n", "moonlan"),
            ("async def later():\n    import moonlan.server\n", "moonlan"),
            ("import importlib\nimportlib.import_module('moonlan.server')", "moonlan"),
            ("from ..server import rt", "moonlan.routes"),
            ("from .. import server", "moonlan.routes"),
            ("from .. import db, server", "moonlan.routes"),
            ("def later():\n    from ..server import app\n", "moonlan.routes"),
            ("from moonlan import server", "moonlan.routes"),
        ):
            with self.subTest(text=text, package=package):
                self.assertTrue(server_imports(text, package))
        for text, package in (
            ("from . import https, settings", "moonlan"),
            ("from fido2.server import Fido2Server", "moonlan"),
            ("import moonlan.settings", "moonlan"),
            ("from .snmp_collector import is_valid_mac", "moonlan"),
            ("from . import common", "moonlan.routes"),
            ("from .common import _refuse", "moonlan.routes"),
            ("from ..runtime import rt", "moonlan.routes"),
            ("from .. import notes, settings", "moonlan.routes"),
        ):
            with self.subTest(text=text, package=package):
                self.assertEqual(server_imports(text, package), [])

    def test_every_new_module_is_listed(self):
        for name in MOVED:
            self.assertIn(name, NEW_MODULES)
        for name in NEW_MODULES:
            self.assertTrue(path_of(name).is_file(), name)
        found = {
            str(path.relative_to(PACKAGE).with_suffix("")).replace("/", ".")
            .removesuffix(".__init__")
            for path in (PACKAGE / "routes").rglob("*.py")
        }
        self.assertLessEqual(found, set(NEW_MODULES))


class MovedNamesTest(unittest.TestCase):
    def test_server_keeps_every_moved_name(self):
        for name, moved in MOVED.items():
            module = importlib.import_module(f"moonlan.{name}")
            for attr, signature in moved.items():
                with self.subTest(module=name, name=attr):
                    self.assertTrue(hasattr(module, attr), "not moved")
                    self.assertTrue(hasattr(server, attr), "not kept")
                    kept = getattr(server, attr)
                    if signature is None:
                        self.assertIs(kept, getattr(module, attr))
                        continue
                    self.assertEqual(kept_signature(kept), signature)
                    self.assertEqual(
                        inspect.iscoroutinefunction(kept),
                        inspect.iscoroutinefunction(getattr(module, attr)),
                    )

    def test_a_wrapper_calls_the_moved_function(self):
        tree = ast.parse((PACKAGE / "server.py").read_text(encoding="utf-8"))
        defined = {
            node.name: node for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for name, moved in MOVED.items():
            for attr, signature in moved.items():
                if signature is None:
                    continue
                with self.subTest(module=name, name=attr):
                    calls = {
                        ast.unparse(node.func) for node in ast.walk(defined[attr])
                        if isinstance(node, ast.Call)
                    }
                    self.assertIn(f"{name}.{attr}", calls)


class LoggerTest(unittest.TestCase):
    def test_the_log_is_moonlan(self):
        logging_ones = []
        for name in NEW_MODULES:
            with self.subTest(module=name):
                module = importlib.import_module(f"moonlan.{name}")
                tree = ast.parse(source(name))
                loggers = [
                    [ast.unparse(arg) for arg in node.args]
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Call)
                    and ast.unparse(node.func) == "logging.getLogger"
                ]
                logs = any(
                    isinstance(node, ast.Name) and node.id == "log"
                    and isinstance(node.ctx, ast.Load) for node in ast.walk(tree)
                )
                if logs:
                    logging_ones.append(name)
                    self.assertEqual(loggers, [["'moonlan'"]])
                    self.assertEqual(module.log.name, "moonlan")
                else:
                    self.assertEqual(loggers, [])
        self.assertIn("startup", logging_ones)
        self.assertIn("routes.settings", logging_ones)


class RuntimeReadTest(unittest.TestCase):
    def test_rt_is_read_when_the_code_runs(self):
        """No module but server.py, which fills it, reads rt at its
        import: db = rt.db at the top would keep what was there before
        server.py filled it, and miss every change after"""
        found = []
        for path in sorted(PACKAGE.rglob("*.py")):
            if path == PACKAGE / "server.py":
                continue
            lines = rt_read_at_import(path.read_text(encoding="utf-8"))
            found += [f"{path.relative_to(PACKAGE)}:{n}" for n in lines]
        self.assertEqual(found, [])

    def test_the_check_sees_a_read_at_import(self):
        for text in ("db = rt.db", "x = [rt.db]",
                     "def f(db=rt.db):\n    pass",
                     "@rt.config.decorate\ndef f():\n    pass",
                     "class K:\n    db = rt.db"):
            with self.subTest(text=text):
                self.assertTrue(rt_read_at_import(text))
        for text in ("from .runtime import rt",
                     "def f():\n    return rt.db",
                     "async def f():\n    rt.first_scan_done = True",
                     "class K:\n    def m(self):\n        return rt.db",
                     "f = lambda: rt.db"):
            with self.subTest(text=text):
                self.assertEqual(rt_read_at_import(text), [])


class NoCycleTest(unittest.TestCase):
    def test_a_new_module_imports_without_the_server(self):
        for name in NEW_MODULES:
            with self.subTest(module=name):
                done = subprocess.run(
                    [sys.executable, "-c",
                     f"import json, sys, moonlan.{name}; "
                     "print(json.dumps(sorted(sys.modules)))"],
                    cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT)},
                    capture_output=True, text=True, timeout=120,
                )
                self.assertEqual(done.returncode, 0, done.stderr[-2000:])
                loaded = json.loads(done.stdout.strip().splitlines()[-1])
                self.assertIn(f"moonlan.{name}", loaded)
                self.assertNotIn("moonlan.server", loaded)

    def test_a_module_of_routes_loads_no_other(self):
        """routes/__init__.py imports none of its modules (v0.8.14):
        server.py imports the ones it includes"""
        done = subprocess.run(
            [sys.executable, "-c",
             "import json, sys, moonlan.routes.common; "
             "print(json.dumps(sorted(m for m in sys.modules "
             "if m.startswith('moonlan.routes'))))"],
            cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT)},
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        self.assertEqual(json.loads(done.stdout.strip().splitlines()[-1]),
                         ["moonlan.routes", "moonlan.routes.common"])


if __name__ == "__main__":
    unittest.main()
