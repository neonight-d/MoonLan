"""The borders between server.py and the modules taken out of it (v0.8.12).

server.py is taken apart in stages (docs/ARCHITECTURE.md). A module
taken out of it never imports moonlan.server — not at the top, not
inside a function, not by name: that is a cycle, and the next stage,
which moves the process state out of server.py, would have to undo
it. What such a module needs from the process it is given.

Until that stage server.py keeps every name that moved — a wrapper
with the old signature, or the constant re-exported — so nothing that
calls or patches server.<name> notices the move. And the log keeps its
name: "moonlan: …" in every line, as before.

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
NEW_MODULES = ["startup", "housekeeping"]

# Every name each of them took, and what server.py keeps of it: the
# signature the function had there, "async " for a coroutine, or None
# for a constant, re-exported
MOVED = {
    "startup": {
        "configure_logging": "() -> None",
        "_config_missing": "() -> dict | None",
        "_log_config": "() -> None",
        "_setting_problem": "(error: dict) -> str",
        "_log_settings": "() -> None",
        "_log_public_url": "() -> None",
        "_log_transport": "() -> None",
        "_log_certificate": "() -> None",
        "start_redirect_listener": "async ()",
        "CERT_WARN_DAYS": None,
    },
    "housekeeping": {
        "purge_invalid_macs": "async () -> None",
        "purge_old_hosts": "async () -> None",
        "purge_journal": "async () -> None",
        "hide_confirmed_copies": "async (vouched: set[str] | None = None) -> None",
        "purge_copies": "async () -> None",
        "drop_unpinned_positions": "async () -> None",
        "check_certificate_end": "async () -> None",
        "resource_usage": "() -> tuple[int, int]",
        "CERT_ALARM_DAYS": None,
        "CERT_CHECK_SECONDS": None,
        "RESOURCE_LOG_SECONDS": None,
        "CLEANUP_SECONDS": None,
    },
}


def server_imports(source: str) -> list[str]:
    """Every way the source imports moonlan.server, at any depth; []
    when it does not."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found += [
                f"import {alias.name}" for alias in node.names
                if alias.name == "moonlan.server"
                or alias.name.startswith("moonlan.server.")
            ]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names = {alias.name for alias in node.names}
            if node.level == 0 and (
                module == "moonlan.server" or module.startswith("moonlan.server.")
                or (module == "moonlan" and "server" in names)
            ):
                found.append(f"from {module} import {', '.join(sorted(names))}")
            elif node.level == 1 and (
                module == "server" or module.startswith("server.")
                or (not module and "server" in names)
            ):
                found.append(f"from .{module} import {', '.join(sorted(names))}")
        elif isinstance(node, ast.Constant) and node.value in (
            "moonlan.server", ".server",
        ):
            found.append(f"{node.value!r} by name")
    return found


def source(name: str) -> str:
    return (PACKAGE / f"{name}.py").read_text(encoding="utf-8")


def kept_signature(function) -> str:
    """What MOVED says of a function"""
    text = str(inspect.signature(function)).replace("'", "")
    return ("async " if inspect.iscoroutinefunction(function) else "") + text


class NoServerImportTest(unittest.TestCase):
    def test_no_new_module_imports_the_server(self):
        for name in NEW_MODULES:
            with self.subTest(module=name):
                self.assertEqual(server_imports(source(name)), [])

    def test_the_check_sees_every_way_in(self):
        for text in (
            "import moonlan.server",
            "import moonlan.server as service",
            "from moonlan import server",
            "from moonlan import db, server",
            "from moonlan.server import db",
            "from .server import db",
            "from . import server",
            "def later():\n    from .server import config\n",
            "async def later():\n    import moonlan.server\n",
            "import importlib\nimportlib.import_module('moonlan.server')",
        ):
            with self.subTest(text=text):
                self.assertTrue(server_imports(text))
        for text in (
            "from . import https, settings",
            "from fido2.server import Fido2Server",
            "import moonlan.settings",
            "from .snmp_collector import is_valid_mac",
        ):
            with self.subTest(text=text):
                self.assertEqual(server_imports(text), [])

    def test_every_new_module_is_listed(self):
        for name in MOVED:
            self.assertIn(name, NEW_MODULES)
        for name in NEW_MODULES:
            self.assertTrue((PACKAGE / f"{name}.py").is_file(), name)


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
        for name in NEW_MODULES:
            with self.subTest(module=name):
                module = importlib.import_module(f"moonlan.{name}")
                self.assertEqual(module.log.name, "moonlan")
                loggers = [
                    [ast.unparse(arg) for arg in node.args]
                    for node in ast.walk(ast.parse(source(name)))
                    if isinstance(node, ast.Call)
                    and ast.unparse(node.func) == "logging.getLogger"
                ]
                self.assertEqual(loggers, [["'moonlan'"]])


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


if __name__ == "__main__":
    unittest.main()
