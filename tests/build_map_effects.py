"""What _build_map does to the world, one scenario at a time (v0.8.14).

    python tests/build_map_effects.py <scenario> <folder> <out.jsonl>

Run by tests/test_build_map_effects.py, each scenario in a process of
its own with PYTHONHASHSEED=0 and TZ=UTC: the scenario writes its
config.yaml into <folder>, its database goes there too, and the demo's
module state (random.Random(7), the count of scans) starts afresh.
Named not test_*, so that discovery leaves it alone.

While _build_map runs, one ordered list of events:

- every call of a method of rt.db, rt.alarm_engine and rt.state —
  proxies put in with patch.object(server.rt, …): the method's name and
  its arguments in canonical form;
- every line of the "moonlan" log and its children: level and text;
- every assignment of a field of rt — rt's class swapped for a subclass
  of Runtime whose __setattr__ writes the name down.

After the scenario, a snapshot: rt.state.as_dict(), rt.fdb_macs,
rt.prev_pseudo_ports, rt.uplink_suspects, rt.first_scan_done, the
map_state of every switch in rt.switch_data, and every table of the
database with its rows sorted.

The scenarios:

- demo-restore: the demo as lifespan prepares it, and restore_readings()
  — the map from the saved reading, observe=False;
- demo-scans: the same, then three scans — the initial inventory, then
  confirmations, damaged copies and new_mac;
- snmp-scans: not the demo, a stub collector, two scans — ARP and a
  router's identity, a switch past its budget, one that stops
  answering, one never read (a stub node), a damaged copy, a port that
  looks like a way out, an LLDP bridge, a new MAC in the second scan;
- snmp-silent: every switch silent at once.

The clock is frozen: time.time() is START, moved on by the scan
interval between scans; the poller's monotonic clock, which only times
polls, stands still with it. Reverse DNS answers from a table. The
SNMP scenarios are synthetic — 10.1.x.x, 02:ee:… MACs, Gi0/x ports,
documentation addresses — and the demo ones are the demo's own network.
"""

import asyncio
import dataclasses
import enum
import hashlib
import inspect
import json
import logging
import os
import sys
import time
import types
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

START = 1_800_000_000.0     # 2027-01-15 08:00 UTC
SCENARIOS = ("demo-restore", "demo-scans", "snmp-scans", "snmp-silent")
# an argument longer than this, in canonical JSON, is kept as its hash
LONG = 3000


class Clock:
    def __init__(self, now: float):
        self.now = now

    def __call__(self) -> float:
        return self.now


CLOCK = Clock(START)


# ---------- canonical form ----------

def canon(value, depth: int = 0):
    """JSON-able, the same from run to run and from 3.11 to 3.12; the
    version of MoonLan (a reading is saved with it) is "<version>", so
    that a new version does not change the record"""
    from moonlan import __version__
    from moonlan.snmp_collector import SwitchData
    if depth > 40:
        return {"type": type(value).__name__}
    if value == __version__ and isinstance(value, str):
        return "<version>"
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, enum.Enum):
        return str(value.value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"type": "bytes", "len": len(value)}
    if isinstance(value, SwitchData):
        return f"SwitchData({value.ip})"
    if isinstance(value, dict):
        items = [(k if isinstance(k, str) else json.dumps(canon(k, depth + 1),
                                                          sort_keys=True),
                  canon(v, depth + 1)) for k, v in value.items()]
        return dict(sorted(items))
    if isinstance(value, (list, tuple)):
        return [canon(v, depth + 1) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted((canon(v, depth + 1) for v in value),
                      key=lambda v: json.dumps(v, sort_keys=True))
    if isinstance(value, (types.FunctionType, types.MethodType,
                          types.BuiltinFunctionType)):
        return f"<function {value.__name__}>"
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        out = {"type": type(value).__name__}
        for field in dataclasses.fields(value):
            out[field.name] = canon(getattr(value, field.name), depth + 1)
        return out
    try:
        size = len(value)
    except TypeError:
        size = None
    return {"type": type(value).__name__, "len": size}


def text(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))


def short(value):
    """An argument as the fixture keeps it: whole, or its hash when long"""
    whole = text(value)
    if len(whole) <= LONG:
        return value
    return {"sha256": hashlib.sha256(whole.encode()).hexdigest()[:24],
            "chars": len(whole)}


# ---------- the recording ----------

class Recording:
    def __init__(self):
        self.events: list[dict] = []
        self.on = False

    def add(self, kind: str, name: str, **data) -> None:
        if self.on:
            self.events.append({"kind": kind, "name": name, **data})

    def mark(self, what: str) -> None:
        self.events.append({"kind": "step", "name": what})


REC = Recording()


class Proxy:
    """Hands every attribute through; a call of a method is an event"""

    def __init__(self, target, kind: str):
        object.__setattr__(self, "_target", target)
        object.__setattr__(self, "_kind", kind)

    def __getattr__(self, name):
        value = getattr(self._target, name)
        if name.startswith("__") or not inspect.ismethod(value):
            return value  # _conn is callable too, and no method
        kind = self._kind

        if asyncio.iscoroutinefunction(value):
            async def call(*args, **kwargs):
                REC.add(kind, name, args=canon(list(args)), kwargs=canon(kwargs))
                return await value(*args, **kwargs)
        else:
            def call(*args, **kwargs):
                REC.add(kind, name, args=canon(list(args)), kwargs=canon(kwargs))
                return value(*args, **kwargs)
        return call

    def __setattr__(self, name, value):
        setattr(self._target, name, value)


class LogLines(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        REC.add("log", record.name, level=record.levelname,
                text=record.getMessage())


def record_build_map(poller):
    """poller._build_map, recorded while it runs (run_scan and
    restore_readings call it by that name)"""
    original = poller._build_map

    async def recorded(*args, **kwargs):
        REC.mark("_build_map")
        REC.on = True
        try:
            return await original(*args, **kwargs)
        finally:
            REC.on = False
    return recorded


def snapshot(server) -> list[dict]:
    """The end of the scenario, one line per element: a list (a table,
    the hosts of the map) gives a line for each of its items"""
    rt = server.rt

    def lines(name, value):
        if isinstance(value, list):
            return [{"kind": "snapshot", "name": name, "item": item}
                    for item in value]
        return [{"kind": "snapshot", "name": name, "value": value}]

    out = []
    for key, value in canon(rt.state.as_dict()).items():
        out += lines(f"state {key}", value)
    out += lines("fdb_macs", canon(rt.fdb_macs))
    out += lines("prev_pseudo_ports", canon(rt.prev_pseudo_ports))
    out += lines("uplink_suspects", canon(rt.uplink_suspects))
    out += lines("first_scan_done", rt.first_scan_done)
    out += lines("map_state", {
        ip: sw.map_state for ip, sw in sorted(rt.switch_data.items())})
    conn = rt.db._conn
    tables = [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
    for table in tables:
        cursor = conn.execute(f"SELECT * FROM {table}")
        columns = [c[0] for c in cursor.description]
        rows = sorted(
            text(canon(dict(zip(columns, tuple(row))))) for row in cursor
        )
        out += lines(f"table {table}", [json.loads(r) for r in rows])
    return out


def compact(event: dict) -> dict:
    """An event as the fixture keeps it: each argument whole or, when
    long, its hash; in a snapshot, a long string by its hash"""
    out = dict(event)
    if "args" in out:
        out["args"] = [short(a) for a in out["args"]]
        out["kwargs"] = {k: short(v) for k, v in out["kwargs"].items()}
    for key in ("value", "item"):
        if key in out:
            out[key] = _short_strings(out[key])
    return out


def _short_strings(value):
    if isinstance(value, str):
        return short(value)
    if isinstance(value, dict):
        return {k: _short_strings(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_short_strings(v) for v in value]
    return value


# ---------- the networks ----------

def write_config(folder: Path, body: str) -> Path:
    path = folder / "config.yaml"
    path.write_text(f"db_path: {folder / 'moonlan.db'}\n" + body,
                    encoding="utf-8")
    return path


CORE, ACCESS, OVER, QUIET, NEVER = (
    "10.1.0.10", "10.1.0.21", "10.1.0.30", "10.1.0.40", "10.1.0.50",
)
ROUTER = "10.1.0.1"
BOND = "02:ee:00:00:10:05"       # the router's bond1, held back as a copy
MEMBER = "02:ee:00:00:10:06"     # its ether5, two bits from BOND
OLD = "02:ee:00:00:20:02"        # the port the router left, grey
SPARE = "02:ee:00:00:44:01"      # never in a MAC table
SAMPLE = "02:ee:00:00:30:8d"     # a real device on core-sw Gi0/9…
COPY = "02:ee:00:00:30:87"       # …and a damaged copy of it, two bits away
NEWBIE = "02:ee:00:00:77:01"     # first seen in the second scan, with an IP
PUBLIC = "02:ee:00:00:66:01"     # answers at a public address: a way out?
NEVER_MAC = "02:ee:00:00:00:50"  # the management MAC of the switch never read
AP = "02:ee:00:00:55:01"         # an LLDP bridge nobody polls
BEHIND = "02:ee:00:00:88:01"     # in ARP only: behind the router
GONE = "02:ee:00:00:99:02"       # placed a year ago, long gone
LAG = "LAG Gi0/15+Gi0/16"
RENAMED = "02:ee:00:00:21:09"    # recorded under "bridge-port 15"
# devices more than corruption_hamming_bits from every other address
# here, so that none of them is taken for a damaged copy
DESKS = ["02:ee:3c:5a:a7:01", "02:ee:c3:a5:5e:02", "02:ee:96:0f:f0:03"]
ROOM = ["02:ee:69:f0:0f:04", "02:ee:5a:3c:c3:05", "02:ee:a5:c3:3c:06",
        "02:ee:0f:96:69:07"]                       # one port: a switch


def switch(ip: str, name: str, octet: int):
    from moonlan.snmp_collector import PortInfo, SwitchData
    sw = SwitchData(
        ip=ip, reachable=True, sys_name=name,
        bridge_mac=f"02:ee:00:00:00:{octet:02x}", polled_at=time.time(),
    )
    sw.own_macs = {sw.bridge_mac}
    for i in range(1, 27):
        sw.ports[i] = PortInfo(
            if_index=i, name=f"Gi0/{i}", oper_up=True, speed_mbps=1000
        )
    return sw


def router_identity():
    from moonlan.routers import identity_from_tables
    return identity_from_tables(
        ROUTER,
        phys={1: SPARE, 2: OLD, 4: BOND, 5: MEMBER,
              10: BOND, 20: BOND, 30: BOND},
        names={1: "ether1", 2: "ether2", 4: "ether4", 5: "ether5",
               10: "bond1", 20: "vlan-mgt", 30: "vlan-users"},
        types={1: 6, 2: 6, 4: 6, 5: 6, 10: 161, 20: 135, 30: 135},
        ip_index={ROUTER: 20, "10.1.1.1": 20, "10.1.2.1": 30},
    )


def neighbour(local: int, chassis: str, name: str, desc: str, caps: set,
              mgmt: str, port_id: str):
    from moonlan.lldp import LldpNeighbor
    return LldpNeighbor(
        local_ifindex=local, local_port_num=local, port_matched_by="loc_id",
        chassis_id=chassis, chassis_subtype=4, port_id=port_id,
        sys_name=name, sys_desc=desc, cap_enabled=caps, cap_known=True,
        mgmt_ip=mgmt, mgmt_ips=[mgmt],
    )


class Network:
    """core-sw with access-sw behind its Gi0/1; the router on access-sw's
    aggregate Gi0/15+Gi0/16; a switch that runs out of budget, one that
    answers the first scan only, one never read. `scan` is the number of
    the scan being collected."""

    def __init__(self, silent: bool = False):
        self.scan = 0
        self.silent = silent

    def begin_scan_cycle(self) -> None:
        self.scan += 1

    async def collect(self, ip: str):
        from moonlan.snmp_collector import PortInfo, SwitchData
        if self.silent or ip == NEVER or (ip == QUIET and self.scan > 1):
            return SwitchData(ip=ip)
        if ip == OVER:
            await asyncio.sleep(60)  # past its budget, every time
        core = switch(CORE, "core-sw", 0x10)
        access = switch(ACCESS, "access-sw", 0x21)
        quiet = switch(QUIET, "quiet-sw", 0x40)
        access.lag_groups = {15: [15, 16]}
        access.ports[-15] = PortInfo(if_index=-15, name=LAG, oper_up=False,
                                     is_physical=False)
        core.fdb[access.bridge_mac] = 1
        access.fdb[core.bridge_mac] = 25
        core.fdb[quiet.bridge_mac] = 2
        quiet.fdb[core.bridge_mac] = 24
        seen_access = {BOND: -15, MEMBER: -15}
        for n, mac in enumerate(DESKS):
            seen_access[mac] = 3 + n
        for mac in ROOM:
            seen_access[mac] = 8
        for mac, port in seen_access.items():
            access.fdb[mac] = port
            core.fdb[mac] = 1
        seen_core = {SAMPLE: 9, PUBLIC: 24, NEVER_MAC: 7}
        if self.scan > 1:
            seen_core[COPY] = 9
            seen_core[NEWBIE] = 11
        for mac, port in seen_core.items():
            core.fdb[mac] = port
            access.fdb[mac] = 25
        access.lldp_neighbors = [neighbour(
            16, OLD, "RouterOS-R", "RouterOS 6.x", {"router"}, "10.1.1.1",
            "vlan-mgt")]
        core.lldp_neighbors = [
            neighbour(5, AP, "ap-1", "Access point", {"bridge", "wlan"},
                      "10.1.0.60", "eth0"),
            neighbour(1, access.bridge_mac, "access-sw", "switch", {"bridge"},
                      ACCESS, "Gi0/25"),
        ]
        return {CORE: core, ACCESS: access, QUIET: quiet}[ip]

    async def collect_arp(self, ip: str) -> dict[str, str]:
        arp = {BOND: ROUTER, PUBLIC: "203.0.113.7", NEVER_MAC: NEVER,
               SAMPLE: "10.1.0.71", BEHIND: "10.1.0.88"}
        if self.scan > 1:
            arp[NEWBIE] = "10.1.0.77"
        return arp

    async def collect_router_identity(self, ip: str):
        return router_identity()


async def reverse_dns(ip: str) -> str | None:
    """Names for some addresses, from a table: no DNS in a test"""
    return {"10.1.0.71": "printer.example.test",
            "10.1.0.77": "laptop.example.test"}.get(ip)


def seed_snmp(db) -> None:
    """What the database held: the router's member confirmed, its bond
    held back, the old port's MAC grey with an address ARP stopped
    confirming, a record under a name from before v0.8.6, the sample;
    a note and a pin on the member, notes on nodes that are back and
    on one that is gone; a position a year old of a node long gone, and
    an offset measured from a node its device no longer hangs off"""
    now = time.time()
    db.seed_host(MEMBER, ACCESS, LAG, 1, first_seen=now - 6 * 86400,
                 last_seen=now - 60, seen_count=500)
    db.seed_host(OLD, ACCESS, "Gi0/21", 1, first_seen=now - 300 * 86400,
                 last_seen=now - 3600, seen_count=9000)
    db.seed_host(SAMPLE, CORE, "Gi0/9", 1, first_seen=now - 30 * 86400,
                 last_seen=now - 600, seen_count=400)
    db.seed_host(RENAMED, ACCESS, "bridge-port 15", 1,
                 first_seen=now - 90 * 86400, last_seen=now - 7200,
                 seen_count=50)
    db._conn.execute(
        "INSERT INTO hosts (mac, switch_ip, port, vlan, first_seen, "
        "last_seen, seen_count, confirmed) VALUES (?, ?, ?, 10, ?, ?, 40, 0)",
        (BOND, ACCESS, LAG, now - 6 * 86400, now - 60),
    )
    db._conn.execute(
        "UPDATE hosts SET ip = ?, ip_confirmed = ? WHERE mac = ?",
        ("10.1.0.99", now - 100 * 3600, OLD),
    )
    db._conn.commit()
    db.set_note("host:" + MEMBER, "Gateway", "", "manual", "admin", now - 3600)
    db.set_note("host:02:ee:00:00:99:01", "Gone", "", "manual", "admin",
                now - 86400)
    db.set_note("sw:" + CORE, "Core", "", "manual", "admin", now - 86400)
    db.set_note("host:" + DESKS[0], "Desk 1", "", "manual", "admin",
                now - 9 * 86400)
    db._conn.execute(
        "UPDATE notes SET missing_scans = 3, missing_since = ?, seen_at = ? "
        "WHERE node_id = ?",
        (now - 2 * 86400, now - 3 * 86400, "host:" + DESKS[0]),
    )
    db.set_node_position("host:" + MEMBER, 120.0, -40.0, True)
    db.set_node_position("sw:" + CORE, 0.0, 0.0, True)
    db.set_node_position("host:" + GONE, 300.0, 300.0, True)
    db._conn.execute(
        "UPDATE layout SET updated_at = ? WHERE node_id = ?",
        (now - 400 * 86400, "host:" + GONE),
    )
    db._conn.execute(
        "INSERT INTO layout (node_id, x, y, pinned, updated_at, anchor) "
        "VALUES (?, 10.0, 10.0, 0, ?, ?)",
        ("host:" + RENAMED, now - 86400, "sw:" + CORE),
    )
    db._conn.commit()


# ---------- the scenarios ----------

def setup(scenario: str, folder: Path) -> None:
    if scenario.startswith("demo"):
        os.environ["MOONLAN_DEMO"] = "1"
        config = write_config(folder, "")
    elif scenario == "snmp-scans":
        os.environ.pop("MOONLAN_DEMO", None)
        config = write_config(
            folder,
            "switches:\n" + "".join(f"  - {ip}\n" for ip in
                                    (CORE, ACCESS, OVER, QUIET, NEVER))
            + f"routers:\n  - {ROUTER}\n",
        )
    else:
        os.environ.pop("MOONLAN_DEMO", None)
        config = write_config(
            folder,
            "switches:\n" + "".join(f"  - {ip}\n" for ip in (CORE, ACCESS, OVER)),
        )
    os.environ["MOONLAN_CONFIG"] = str(config)


async def run(scenario: str, server, poller) -> None:
    from moonlan import demo
    rt = server.rt
    interval = max(rt.config.scan_interval_minutes, 1) * 60

    async def scan(n: int) -> None:
        CLOCK.now += interval
        REC.mark(f"run_scan {n}")
        await poller.run_scan()

    if scenario.startswith("demo"):
        # what lifespan does in the demo before the first scan
        demo.seed_notes(rt.db)
        demo.seed_reading(rt.db)
        demo.seed_stale_alarm(rt.db)
        await rt.alarm_engine.load()
        REC.mark("restore_readings")
        await poller.restore_readings()
        if scenario == "demo-scans":
            for n in (1, 2, 3):
                await scan(n)
        return
    network = Network(silent=scenario == "snmp-silent")
    poller.get_collector = lambda: network
    rt.config.new_host_confirm_scans = 2
    rt.config.filter_suspect_macs = True
    rt.config.unmanaged_threshold = 3
    rt.config.thresholds.corruption_hamming_bits = 8
    rt.config.host_grace_hours = 24
    rt.config.stale_switch_scans = 2
    budgets = {OVER: 0.3}
    rt.config.host_budget = lambda ip: budgets.get(ip, 30)
    if scenario == "snmp-silent":
        await scan(1)
        return
    seed_snmp(rt.db)
    await rt.alarm_engine.load()
    for n in (1, 2):
        await scan(n)


def main() -> None:
    scenario, folder, out = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
    if scenario not in SCENARIOS:
        raise SystemExit(f"no scenario {scenario!r}: {', '.join(SCENARIOS)}")
    if os.environ.get("PYTHONHASHSEED") != "0" or os.environ.get("TZ") != "UTC":
        raise SystemExit("run with PYTHONHASHSEED=0 and TZ=UTC")
    time.tzset()
    setup(scenario, folder)
    real_time = time.time
    time.time = CLOCK
    from moonlan import poller, runtime, server
    from moonlan.mapping import hosts
    # the poller times polls by the monotonic clock: still, like the rest
    poller.time = types.SimpleNamespace(
        **{name: getattr(time, name) for name in dir(time)
           if not name.startswith("_")},
    )
    poller.time.monotonic = lambda: 0.0
    hosts._reverse_dns = reverse_dns

    moonlan_log = logging.getLogger("moonlan")
    moonlan_log.setLevel(logging.DEBUG)
    moonlan_log.addHandler(LogLines())
    moonlan_log.propagate = False

    class RecordedRuntime(runtime.Runtime):
        def __setattr__(self, name, value):
            REC.add("rt", name)
            object.__setattr__(self, name, value)

    poller._build_map = record_build_map(poller)
    rt = server.rt
    with mock.patch.object(rt, "db", Proxy(rt.db, "db")), \
            mock.patch.object(rt, "alarm_engine", Proxy(rt.alarm_engine, "alarm_engine")), \
            mock.patch.object(rt, "state", Proxy(rt.state, "state")):
        rt.__class__ = RecordedRuntime
        try:
            asyncio.run(run(scenario, server, poller))
        finally:
            rt.__class__ = runtime.Runtime
        events = REC.events + snapshot(server)
    time.time = real_time
    with out.open("w", encoding="utf-8") as f:
        for event in events:
            f.write(text(event) + "\n")


if __name__ == "__main__":
    main()
