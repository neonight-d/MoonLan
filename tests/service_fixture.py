"""One configuration for every test that imports the service.

`moonlan.server` reads its configuration at import time and caches it
in module globals, so the first test module to import it decides what
every later one sees. Three of them used to write their own temporary
config and set MOONLAN_CONFIG, and which one won depended on the
alphabet: adding `test_saved_reading` moved the winner from
`test_scan_budget` and broke four of its tests, none of which had
changed.

The cure is one config they all use. This module is not named `test*`,
so unittest discovery imports it only through them.
"""

import os
import tempfile
from pathlib import Path

# Eight of them because the budget tests need a crowd; the rest of the
# service tests use the first two and ignore the others.
SWITCHES = [f"10.0.0.{n}" for n in range(1, 9)]

_TMP = tempfile.TemporaryDirectory()
_CONFIG = Path(_TMP.name) / "config.yaml"
_CONFIG.write_text(
    "db_path: " + str(Path(_TMP.name) / "test.db") + "\n"
    "scan_interval_minutes: 0\n"
    "snmp:\n"
    "  host_budget_seconds: 1\n"
    "switches:\n" + "".join(f"  - {ip}\n" for ip in SWITCHES),
    encoding="utf-8",
)
os.environ.setdefault("MOONLAN_CONFIG", str(_CONFIG))
