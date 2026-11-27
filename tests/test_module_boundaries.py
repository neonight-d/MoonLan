"""The borders between server.py and the modules taken out of it.

server.py was taken apart in stages (docs/ARCHITECTURE.md): startup.py
and housekeeping.py in v0.8.12, the state of the process in
runtime.py, derived.py and the routes in routes/ in v0.8.13, the
poller, the map's helpers in mapping/ and the life of the service in
lifecycle.py in v0.8.14. A module taken out of it never imports
moonlan.server — not at the top, not inside a function, not by name:
that is a cycle. What such a module needs from the process it reads
from rt (runtime.py) when it runs; never at its import, before
server.py has filled rt.

server.py keeps nothing that moved: it fills rt and puts the app
together, and a function or class of the modules taken out is not
found through it — the refusal says where it lives, and the tests use
it there. The modules stand in layers, each importing only from its own
layer or below. And the log keeps its name: "moonlan: …" in every
line, as before.

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

# The modules taken out of server.py, stage by stage
NEW_MODULES = [
    "startup", "housekeeping",
    "runtime", "derived", "routes", "routes.common", "routes.system",
    "routes.settings", "routes.notes", "routes.layout", "routes.actions",
    "routes.alarms", "routes.topology", "routes.polling",
    "journal", "poller", "lifecycle", "mapping", "mapping.hosts",
    "mapping.switches", "mapping.bridges", "mapping.router_nodes",
    "mapping.upkeep", "mapping.build",
]

# What server.py itself defines (v0.8.14): the strict module with the
# helper that finds a name's address, and the probe the demo's jobs run
SERVER_DEFINES = {"_moved_to_rt", "_moved", "__getattr__", "_StrictModule",
                  "_demo_probe"}
# The functions and classes of NEW_MODULES that server.py imports for
# itself: the app's lifespan and the static page's files
SERVER_TAKES = {"lifespan", "RevalidatedStaticFiles"}

# The layers (docs/ARCHITECTURE.md): a module imports only from its own
# layer or below. Every module of moonlan not named here is layer 0:
# runtime, journal, about, config, db, topology, alarms, …
LAYERS = {
    "server": 5,
    "lifecycle": 4, "routes": 4,
    "poller": 3,
    "mapping": 2,
    "derived": 1, "startup": 1, "housekeeping": 1,
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


def layer(module: str) -> int:
    """The layer of a module named under moonlan ("" is the package)"""
    return LAYERS.get(module.split(".")[0], 0) if module else 0


def _is_module(name: str) -> bool:
    path = PACKAGE.joinpath(*name.split(".")) if name else PACKAGE
    return path.with_suffix(".py").is_file() or (path / "__init__.py").is_file()


def moonlan_imports(source: str, package: str) -> set[str]:
    """Every module of moonlan the source — a module of `package` —
    imports, at any depth: import statements and importlib by name.
    Names are under moonlan: "poller", "routes.notes"; "" is the
    package itself (`from . import __version__`)."""
    found = set()
    inside = package.split(".")[1:]  # the package under moonlan

    def relative(level: int, module: str | None) -> str | None:
        if level - 1 > len(inside):
            return None
        base = inside[:len(inside) - (level - 1)]
        return ".".join(base + ([module] if module else []))

    def add(base: str, names=()):
        subs = [f"{base}.{n}" if base else n for n in names]
        found.update(s for s in subs if _is_module(s))
        if not names or not all(_is_module(s) for s in subs):
            found.add(base)

    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "moonlan" or alias.name.startswith("moonlan."):
                    add(alias.name.removeprefix("moonlan").removeprefix("."))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = relative(node.level, node.module)
            elif node.module == "moonlan" or (node.module or "").startswith("moonlan."):
                base = node.module.removeprefix("moonlan").removeprefix(".")
            else:
                continue
            if base is not None:
                add(base, [alias.name for alias in node.names])
        elif (isinstance(node, ast.Call) and node.args
              and ast.unparse(node.func).endswith("import_module")
              and isinstance(node.args[0], ast.Constant)
              and isinstance(node.args[0].value, str)):
            name = node.args[0].value
            if name.startswith("."):
                level = len(name) - len(name.lstrip("."))
                base = relative(level, name.lstrip(".") or None)
                if base is not None:
                    add(base)
            elif name == "moonlan" or name.startswith("moonlan."):
                add(name.removeprefix("moonlan").removeprefix("."))
    return found


def import_graph() -> dict[str, set[str]]:
    """Every module of moonlan -> the modules of moonlan it imports"""
    graph = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        parts = list(path.relative_to(PACKAGE).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        name = ".".join(parts)
        package = ".".join(["moonlan"] + (parts if path.name == "__init__.py" else parts[:-1]))
        graph[name] = moonlan_imports(path.read_text(encoding="utf-8"), package) - {name}
    return graph


def layer_violations(graph: dict[str, set[str]]) -> list[str]:
    """What breaks the layers: an import up, lifecycle and routes
    importing each other, the poller imported by a module of routes
    other than routes.polling, a cycle inside a layer"""
    found = []
    for module, imported in sorted(graph.items()):
        for other in sorted(imported):
            if layer(other) > layer(module):
                found.append(f"up: {module} -> {other}")
            pair = {module.split(".")[0], other.split(".")[0]}
            if pair == {"lifecycle", "routes"}:
                found.append(f"lifecycle and routes: {module} -> {other}")
            if (module.startswith("routes") and layer(other) == 3
                    and module != "routes.polling"):
                found.append(f"the poller from routes: {module} -> {other}")
    # a cycle inside a layer: a module that reaches itself by imports
    # of its own layer
    for start in sorted(graph):
        todo, seen = [start], set()
        while todo:
            module = todo.pop()
            for other in graph.get(module, ()):
                if layer(other) != layer(start):
                    continue
                if other == start:
                    found.append(f"cycle in layer {layer(start)}: {start}")
                    todo = []
                    break
                if other not in seen:
                    seen.add(other)
                    todo.append(other)
    return found


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
        for name in NEW_MODULES:
            self.assertTrue(path_of(name).is_file(), name)
        for package in ("routes", "mapping"):
            with self.subTest(package=package):
                found = {
                    str(path.relative_to(PACKAGE).with_suffix(""))
                    .replace("/", ".").removesuffix(".__init__")
                    for path in (PACKAGE / package).rglob("*.py")
                }
                self.assertLessEqual(found, set(NEW_MODULES))


class AssemblyTest(unittest.TestCase):
    def test_server_defines_only_the_assembly(self):
        tree = ast.parse(source("server"))
        defined = {
            node.name for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        }
        self.assertEqual(defined, SERVER_DEFINES)
        routes_declared = [
            ast.unparse(decorator) for node in ast.walk(tree)
            for decorator in getattr(node, "decorator_list", [])
            if ast.unparse(decorator).startswith(("app.", "router."))
        ]
        self.assertEqual(routes_declared, [])

    def test_no_function_or_class_moved_is_found_through_server(self):
        """Read through server, each says where it lives — the module
        that defines it, or one of them when two define that name"""
        defined_in = {}
        for module in list(sys.modules.values()):
            name = getattr(module, "__name__", "")
            if not (name == "moonlan" or name.startswith("moonlan.")):
                continue
            for attr, value in list(vars(module).items()):
                if ((inspect.isfunction(value) or inspect.isclass(value))
                        and value.__module__ == name):
                    defined_in.setdefault(attr, set()).add(name)
        checked = 0
        for name in NEW_MODULES:
            module = importlib.import_module(f"moonlan.{name}")
            for attr, value in vars(module).items():
                if not ((inspect.isfunction(value) or inspect.isclass(value))
                        and value.__module__ == module.__name__):
                    continue
                with self.subTest(module=name, name=attr):
                    if attr in SERVER_TAKES:
                        self.assertIs(getattr(server, attr), value)
                        continue
                    checked += 1
                    self.assertNotIn(attr, vars(server))
                    with self.assertRaises(AttributeError) as caught:
                        getattr(server, attr)
                    places = defined_in[attr]
                    hints = {f"server.{attr} moved to {where}: use {where}.{attr}"
                             for where in places}
                    self.assertIn(str(caught.exception), hints)
                    if len(places) == 1:
                        self.assertIn(f"use moonlan.{name}.{attr}",
                                      str(caught.exception))
        self.assertGreater(checked, 150)
        for attr in SERVER_TAKES:
            self.assertIn(attr, vars(server))


class LayerTest(unittest.TestCase):
    def test_the_modules_keep_their_layers(self):
        graph = import_graph()
        self.assertIn("poller", graph["routes.polling"])
        self.assertIn("lifecycle", graph["server"])
        self.assertEqual(layer_violations(graph), [])
        self.assertEqual([m for m, imported in graph.items() if "server" in imported], [])

    def test_the_check_reads_every_import(self):
        for text, package, expected in (
            ("from ..poller import run_scan", "moonlan.mapping", {"poller"}),
            ("def later():\n    from .. import poller\n", "moonlan.mapping", {"poller"}),
            ("from . import topology", "moonlan.routes", {"routes.topology"}),
            ("from .. import topology", "moonlan.routes", {"topology"}),
            ("from .. import __version__, signin", "moonlan.routes", {"", "signin"}),
            ("from .mapping.hosts import _mark_recent", "moonlan", {"mapping.hosts"}),
            ("from .mapping import hosts", "moonlan", {"mapping.hosts"}),
            ("import moonlan.poller", "moonlan", {"poller"}),
            ("from moonlan.routes import polling", "moonlan.mapping", {"routes.polling"}),
            ("import importlib\nimportlib.import_module('moonlan.lifecycle')",
             "moonlan.routes", {"lifecycle"}),
            ("from importlib import import_module\nimport_module('..poller', __package__)",
             "moonlan.mapping", {"poller"}),
            ("import asyncio\nfrom fastapi import APIRouter", "moonlan.routes", set()),
        ):
            with self.subTest(text=text, package=package):
                self.assertEqual(moonlan_imports(text, package), expected)

    def test_the_check_sees_every_break(self):
        for graph, expected in (
            ({"mapping.hosts": {"poller"}}, "up: mapping.hosts -> poller"),
            ({"derived": {"mapping.upkeep"}}, "up: derived -> mapping.upkeep"),
            ({"journal": {"housekeeping"}}, "up: journal -> housekeeping"),
            ({"lifecycle": {"routes.common"}},
             "lifecycle and routes: lifecycle -> routes.common"),
            ({"routes.notes": {"lifecycle"}},
             "lifecycle and routes: routes.notes -> lifecycle"),
            ({"routes.topology": {"poller"}},
             "the poller from routes: routes.topology -> poller"),
            ({"mapping.hosts": {"mapping.bridges"}, "mapping.bridges": {"mapping.hosts"}},
             "cycle in layer 2: mapping.bridges"),
            ({"config": {"db"}, "db": {"settings"}, "settings": {"config"}},
             "cycle in layer 0: config"),
        ):
            with self.subTest(graph=graph):
                self.assertIn(expected, layer_violations(graph))
        self.assertEqual(layer_violations({
            "server": {"lifecycle", "routes.polling", "runtime"},
            "routes.polling": {"poller", "routes.common"},
            "poller": {"mapping.hosts", "derived"},
            "mapping.upkeep": {"notes", "runtime"},
        }), [])


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
