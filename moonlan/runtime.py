"""The state of the running process, in one object (v0.8.13).

Until v0.8.13 these were globals of server.py. Now there is one
`rt = Runtime()`, made when this module is imported and never replaced:
server.py fills its fields at its own import, in the order it used to
assign the globals (settings.load changes the config in place, so the
order matters), and everything reads rt.<name> when it runs. Import
`rt` itself — `from .runtime import rt` — never one of its fields at
module level: `db = rt.db` there would keep what was in it at import,
before server.py filled it, and miss every later change.

The fields are declared here with their types; what each holds is said
where server.py fills it. Until then a field is None, or the empty
container or the False the global used to start as. The config is
changed only in place: the alarm engine and the notifier hold parts of
it, and settings.py edits them.

Nothing from MoonLan is imported here but for the annotations, and
nothing here imports moonlan.server (docs/ARCHITECTURE.md).
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncio

    from . import counters, demo, oui, probes, signin, subscriptions
    from .alarms import AlarmEngine
    from .config import Config
    from .db import Database
    from .https import CertInfo, PublicUrl
    from .notify import Notifier
    from .settings import LoadReport
    from .snmp_collector import SnmpCollector, SwitchData
    from .topology import FdbStability, TopologyState


@dataclass(eq=False, repr=False)
class Runtime:
    """Every field is filled by server.py when it is imported"""

    state: TopologyState = None
    config: Config = None
    public_url: PublicUrl | None = None
    public_url_problems: list[str] = field(default_factory=list)
    trusted_proxies: list[str] = field(default_factory=list)
    trusted_proxy_problems: list[str] = field(default_factory=list)
    certificate: CertInfo | None = None
    vendors: oui.Registry = None
    db: Database = None
    settings_loaded: LoadReport = None
    accounts: Database = None
    transport: str = None
    sign_in: signin.SignIn = None
    switch_ping: dict[str, dict] = field(default_factory=dict)
    tools: dict = field(default_factory=dict)
    jobs: probes.Jobs = None
    fdb_stability: FdbStability = None
    switch_data: dict[str, SwitchData] = field(default_factory=dict)
    poll_report: dict[str, dict] = field(default_factory=dict)
    readings_from_database: set[str] = field(default_factory=set)
    counter_store: counters.CounterStore = None
    flap_tracker: counters.FlapTracker = None
    flap_by_port: dict[tuple[str, str], counters.PortFlaps] = field(
        default_factory=dict
    )
    counter_columns: dict[str, dict[str, counters.ColumnStatus]] = field(
        default_factory=dict
    )
    notifier: Notifier = None
    personal: subscriptions.Subscriptions = None
    alarm_engine: AlarmEngine = None
    demo_counters: demo.DemoCounters | None = None
    first_scan_done: bool = False
    fdb_macs: set[str] = field(default_factory=set)
    prev_pseudo_ports: set[tuple[str, str]] = field(default_factory=set)
    suspect_by_port: dict[tuple[str, str], list[dict]] = field(
        default_factory=dict
    )
    uplink_suspects: dict[tuple[str, str], dict] = field(default_factory=dict)
    layout_purged: bool = False
    _loop_profiles: list | None = None
    _collector: SnmpCollector | None = None
    _host_locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    _identity_reported: dict[str, frozenset[str]] = field(default_factory=dict)
    _dns_attempts: dict[str, float] = field(default_factory=dict)
    interval_changed: dict[str, asyncio.Event] = field(default_factory=dict)
    _loop_logged: dict[str, str] = field(default_factory=dict)


# The names of the fields, the one list of what moved here: server.py
# refuses them loudly (its _StrictModule), the tests walk them
FIELDS: tuple[str, ...] = tuple(f.name for f in fields(Runtime))

rt = Runtime()
