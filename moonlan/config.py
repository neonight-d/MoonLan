"""Loading the MoonLan configuration from a YAML file."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_CONFIG_PATH = Path("config.yaml")
# Point a second instance at its own file: the service runs out of the
# project directory, so anything started there for a quick check would
# otherwise load the production config — and with it the production
# db_path and port, which is how a demo run writes demo hosts into the
# real inventory.
CONFIG_PATH_ENV = "MOONLAN_CONFIG"


@dataclass
class SnmpConfig:
    community: str = "public"
    # A mean timeout does not look like a timeout further down the
    # line: it looks like a switch that does not implement the OID.
    # Two seconds and one retry produced empty and truncated tables on
    # the network this service was written for, and a wrong diagnosis
    # ("the DGS-1210 does not answer ifHCOutOctets") came straight out
    # of them — the switch answers it fine, given five seconds. A
    # generous timeout costs a healthy agent nothing at all.
    timeout: int = 5
    retries: int = 2
    # How many times a walk that stops answering mid-table is picked
    # back up from the last OID that did arrive. Not the same as
    # `retries`, which re-sends a single request.
    retries_on_break: int = 2
    # The whole poll of one switch, start to finish. `timeout` bounds a
    # single request; nothing bounded the sum of them, so one slow
    # agent held up the entire scan — and, because a scan already
    # running makes the next one return at once, every scan after it
    # as well. Two minutes is several times what the slowest healthy
    # switch on the network this was written for needs.
    host_budget_seconds: int = 120
    # An agent that does not implement a table is supposed to say so
    # (noSuchObject), and that answer is cheap. Some simply go quiet,
    # and the walk pays the full timeout budget for nothing — every
    # cycle, forever. After this many walks in a row that returned no
    # rows AND ended in a timeout, the OID is left alone on that host
    # for `dead_oid_cooldown_scans` scans, then tried again: firmware
    # gets updated. Only that one outcome counts. A partial answer is
    # picked back up (retries_on_break), and an honest noSuchObject
    # costs nothing to keep asking for.
    dead_oid_strikes: int = 3
    dead_oid_cooldown_scans: int = 30


@dataclass
class Thresholds:
    # Errors are damaged frames — rare and worth a warning. Discards
    # are usually normal filtering (VLAN rules, storm control, a full
    # buffer during a burst), so they have their own, much higher bar.
    errors_per_minute: float = 5.0
    error_ratio_percent: float = 0.01  # of all frames on the port
    discards_per_minute: float = 500.0
    port_utilization_percent: float = 90.0
    port_alarm_cycles: int = 3  # counter cycles over/under a threshold
    mass_down_hosts: int = 3  # port_hosts_down: newly silent hosts per port
    # Frame corruption: how far a MAC may sit from a confirmed one on
    # the same port and still be read as a damaged copy of it, and how
    # many such copies raise port_frame_corruption within the flap window
    corruption_hamming_bits: int = 8
    corruption_macs_threshold: int = 5
    # Spanning tree: how many topology changes inside one scan cycle
    # are still routine. A converging tree produces a handful; a
    # flapping link produces a stream.
    stp_changes_per_cycle: int = 3
    # A port that goes up and down this many times inside the window
    # is flapping, whatever the counters say about it
    flaps_per_window: int = 4
    flap_window_minutes: float = 10.0


@dataclass
class LoopDetectionConfig:
    """Loop Detection read from the vendors' private MIBs.

    `profiles` adds model families or replaces a built-in one by name;
    the shape of an entry is documented in config.example.yaml and
    parsed by loopdetect.parse_profiles.
    """

    enabled: bool = True
    profiles: list = field(default_factory=list)


@dataclass
class EmailConfig:
    enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    starttls: bool = True
    username: str = ""
    password: str = ""
    mail_from: str = ""
    mail_to: list[str] = field(default_factory=list)


@dataclass
class TelegramConfig:
    enabled: bool = False
    bot_token: str = ""
    chat_ids: list[str] = field(default_factory=list)


@dataclass
class SyslogConfig:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 514


@dataclass
class NotificationsConfig:
    cooldown_seconds: int = 300  # anti-spam per (type, subject)
    # Flap damping: >= flap_count raises of one (type, subject) within
    # flap_window_seconds mute its notifications until it stays quiet
    # for flap_quiet_seconds (alarms keep flowing to the DB/journal)
    flap_count: int = 3
    flap_window_seconds: int = 7200
    flap_quiet_seconds: int = 3600
    email: EmailConfig = field(default_factory=EmailConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    syslog: SyslogConfig = field(default_factory=SyslogConfig)


# Which alarm types go to which channels (a channel still has to be
# enabled in the notifications section to actually send anything)
DEFAULT_ALARM_NOTIFY: dict[str, list[str]] = {
    "host_down": ["email", "telegram", "syslog"],
    "switch_down": ["email", "telegram", "syslog"],
    "port_errors": ["syslog"],
    # discards are noisy and usually harmless: syslog only, never
    # Telegram or email
    "port_discards": ["syslog"],
    "port_util": ["telegram", "syslog"],
    "new_mac": ["syslog"],
    "port_hosts_down": ["email", "telegram", "syslog"],
    "lag_degraded": ["telegram", "syslog"],
    # a physical fault: worth waking someone, but not by email
    "port_frame_corruption": ["telegram", "syslog"],
    # a switch appeared behind an access port and nobody put it there
    "unmanaged_bridge_detected": ["telegram", "syslog"],
    "stp_root_changed": ["telegram", "syslog"],
    "stp_topology_change": ["telegram", "syslog"],
    "stp_fragmented": ["telegram", "syslog"],
    "port_flapping": ["telegram", "syslog"],
    # A loop takes a segment down; with STP off it is the only thing
    # standing between the network and a broadcast storm
    "loop_detected": ["email", "telegram", "syslog"],
    # …and its silent disappearance is worth a line in the log
    "loop_detection_disabled": ["syslog"],
}


@dataclass
class Config:
    listen_host: str = "0.0.0.0"
    listen_port: int = 8080
    snmp: SnmpConfig = field(default_factory=SnmpConfig)
    switches: list[str] = field(default_factory=list)
    # Per-switch SNMP settings, one entry per address in `switches`.
    # Everything not written for that switch is inherited from `snmp:`,
    # so this is always complete and nothing has to fall back to the
    # global section at the point of use.
    switch_snmp: dict = field(default_factory=dict)
    routers: list[str] = field(default_factory=list)
    scan_interval_minutes: int = 10
    ping_interval_seconds: int = 60
    counters_interval_seconds: int = 60
    db_path: str = "moonlan.db"
    unmanaged_threshold: int = 3  # hosts per port; 0 disables pseudo-switches
    monitored_by_default: bool = False  # True = every host raises host_down
    # A host stays on the map this long after its MAC left the FDB
    # (FDB entries age out in minutes; quiet devices must not blink)
    host_grace_hours: float = 24.0
    host_retention_days: float = 30.0  # then it is deleted from the DB
    # Offline devices on one port are drawn as a single group node
    # instead of a cloud of grey dots around the switch
    offline_group_threshold: int = 2   # 0 disables the grouping
    # Devices visible only through a trunk are drawn as one "Beyond the
    # trunk" node rather than as dots on the trunk itself: the port is
    # known, the place behind it is not
    trunk_group_threshold: int = 3     # 0 disables the grouping
    # A stale host whose IP ARP has not confirmed for this long
    # gives the address up: it may belong to another device now
    ip_confirm_hours: float = 6.0
    # Polls a brand-new MAC must appear in before it becomes a device
    # (a MAC ARP already knows by IP is taken at once). Damaged frames
    # invent addresses that live for one poll — this is what keeps them
    # off the map, out of the journal and out of the alarms.
    new_host_confirm_scans: int = 2
    # Keep MACs that look like damaged copies of a real address off the
    # map; false draws them, which is a way to see the damage itself
    filter_suspect_macs: bool = True
    # A device no switch sees on a host port is placed on the trunk
    # with the best claim to it, marked approximate, rather than
    # dropped off the map
    place_trunk_only_hosts: bool = True
    # Bridges that are known and expected behind an access port, by
    # chassis id (MAC) or management IP: no unmanaged_bridge_detected
    known_bridges: list[str] = field(default_factory=list)
    # Ports that leave the network — a provider handover, an uplink to
    # someone else's equipment — as "<switch ip>:<port name>". What is
    # behind them is not ours to map or to alarm about.
    uplink_ports: list[str] = field(default_factory=list)
    # Loop detection: which models to read it from, and whether to
    # read it at all
    loop_detection: LoopDetectionConfig = field(
        default_factory=LoopDetectionConfig
    )
    thresholds: Thresholds = field(default_factory=Thresholds)
    notifications: NotificationsConfig = field(default_factory=NotificationsConfig)
    alarm_notify: dict[str, list[str]] = field(
        default_factory=lambda: dict(DEFAULT_ALARM_NOTIFY)
    )
    demo: bool = False
    # filled in by load_config: which settings came from the file
    report: "ConfigReport | None" = None

    def host_snmp(self, ip: str) -> "HostSnmp":
        """The settings this switch is polled with, inheritance applied.

        A switch that is not in `switches:` — a router from `routers:`,
        an address typed into diag — gets the global section, which is
        what every caller assumed before per-switch settings existed.
        """
        found = self.switch_snmp.get(ip)
        if found is not None:
            return found
        return HostSnmp(
            community=self.snmp.community,
            timeout=self.snmp.timeout,
            retries=self.snmp.retries,
            retries_on_break=self.snmp.retries_on_break,
            host_budget_seconds=self.snmp.host_budget_seconds,
        )

    def host_budget(self, ip: str) -> int:
        return self.host_snmp(ip).host_budget_seconds

    def custom_switches(self) -> list[str]:
        """Addresses whose settings differ from the global section."""
        return [
            ip for ip in self.switches
            if self.switch_snmp.get(ip)
            and self.switch_snmp[ip].explicit
        ]


def parse_uplink_ports(entries: list[str]) -> set[tuple[str, str]]:
    """"10.0.0.10:Slot0/25" -> {("10.0.0.10", "Slot0/25")}.

    Split on the first colon: switch addresses have none and port names
    ("Slot0/25", "1/25", "24") have none either.
    """
    parsed: set[tuple[str, str]] = set()
    for entry in entries:
        ip, sep, port = entry.partition(":")
        if sep and ip.strip() and port.strip():
            parsed.add((ip.strip(), port.strip()))
    return parsed


@dataclass(frozen=True)
class HostSnmp:
    """The SNMP settings one switch is actually polled with.

    A network is never made of one kind of hardware. Five seconds with
    two retries is a sensible compromise for a D-Link; on an RB941 it
    means every request it does not answer costs fifteen seconds, and a
    poll holds thirteen of those. Until now the only way to account for
    that was to make the setting worse for every switch at once.

    `explicit` names the keys this switch was given of its own; the
    rest are inherited from the global `snmp:` section, and
    `diag --config` prints which is which.
    """

    community: str
    timeout: int
    retries: int
    retries_on_break: int
    host_budget_seconds: int
    explicit: frozenset = frozenset()


# Per-switch keys, and where each one is read from a switches: entry
HOST_SNMP_KEYS = (
    "community", "timeout", "retries", "retries_on_break",
    "host_budget_seconds",
)
_HOST_SNMP_CAST = {
    "community": str,
    "timeout": int,
    "retries": int,
    "retries_on_break": int,
    "host_budget_seconds": int,
}


def parse_switches(
    value, defaults: SnmpConfig
) -> tuple[list[str], dict[str, HostSnmp], list[str]]:
    """`switches:` -> (addresses, per-switch settings, complaints).

    An entry is either an address, exactly as every config.yaml written
    so far has it, or a mapping with `ip:` and any of the SNMP keys.
    The old form must keep working untouched: this is the one file an
    operator edits by hand.
    """
    problems: list[str] = []
    addresses: list[str] = []
    settings: dict[str, HostSnmp] = {}
    if value is None:
        return [], {}, []
    if not isinstance(value, (list, tuple)):
        value = [value]
    for entry in value:
        if isinstance(entry, dict):
            ip = str(entry.get("ip") or entry.get("address") or "").strip()
            if not ip:
                problems.append(
                    f"a switches: entry has no ip: {entry!r} — skipped"
                )
                continue
            overrides: dict[str, object] = {}
            for key, raw in entry.items():
                if key in ("ip", "address"):
                    continue
                if key not in _HOST_SNMP_CAST:
                    problems.append(
                        f"{ip}: unknown per-switch key {key!r} — ignored"
                    )
                    continue
                try:
                    overrides[key] = _HOST_SNMP_CAST[key](raw)
                except (TypeError, ValueError):
                    problems.append(
                        f"{ip}: {key} is not a number: {raw!r} — "
                        f"the global value is used"
                    )
        else:
            ip = str(entry).strip()
            overrides = {}
        if not ip:
            continue
        addresses.append(ip)
        settings[ip] = HostSnmp(
            community=str(overrides.get("community", defaults.community)),
            timeout=int(overrides.get("timeout", defaults.timeout)),
            retries=int(overrides.get("retries", defaults.retries)),
            retries_on_break=int(
                overrides.get("retries_on_break", defaults.retries_on_break)
            ),
            host_budget_seconds=int(
                overrides.get(
                    "host_budget_seconds", defaults.host_budget_seconds
                )
            ),
            explicit=frozenset(overrides),
        )
    return addresses, settings, problems


def _as_str_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [str(value)]


_MISSING = object()
# Keys `diag --config` prints as *** rather than as themselves. Not
# only the credentials: chat_ids is a Telegram account and mail_to is
# a person, and both went into a published report in the clear. The
# test here is "would this identify someone", not "is this a
# password". Addresses are a separate matter — the operator needs to
# see their own switch list in an audit of their own config, so
# `--anonymize` rewrites those instead of hiding them.
SECRET_KEYS = (
    "password", "bot_token", "community", "chat_ids", "mail_to",
    "username", "mail_from",
)


@dataclass
class ConfigReport:
    """Where every setting came from, for `diag --config` and the log.

    Working config.yaml files fall behind the example: new keys are
    missing (so silent defaults apply) and stale ones keep overriding
    them. The loader records what it read, so both cases are visible.
    """

    path: str = ""
    exists: bool = False
    # (dotted key, value, "config.yaml" | "default")
    values: list[tuple[str, object, str]] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    # Entries the loader could read but not use: a switches: mapping
    # with no ip:, a per-switch key nobody implements, a timeout that
    # is not a number. Silently dropping any of those leaves an
    # operator convinced a setting is in force when it is not.
    problems: list[str] = field(default_factory=list)

    @property
    def overrides(self) -> list[tuple[str, object, str]]:
        return [v for v in self.values if v[2] == "config.yaml"]

    @property
    def defaults(self) -> list[tuple[str, object, str]]:
        return [v for v in self.values if v[2] == "default"]


def _leaf_paths(node, prefix: str = "") -> list[str]:
    """Dotted paths of every scalar or list in a parsed YAML tree."""
    if not isinstance(node, dict):
        return [prefix] if prefix else []
    paths: list[str] = []
    for key, value in node.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict) and value:
            paths.extend(_leaf_paths(value, path))
        else:
            paths.append(path)
    return paths


class _Reader:
    """Reads settings out of the parsed YAML and remembers the source."""

    def __init__(self, raw: dict):
        self._raw = raw
        self._asked: list[str] = []
        self.values: list[tuple[str, object, str]] = []

    def get(self, path: str, default, cast=None):
        self._asked.append(path)
        node = self._raw
        for part in path.split("."):
            if isinstance(node, dict) and node.get(part) is not None:
                node = node[part]
            else:
                node = _MISSING
                break
        if node is _MISSING:
            self.values.append((path, default, "default"))
            return default
        value = cast(node) if cast else node
        self.values.append((path, value, "config.yaml"))
        return value

    def unknown_keys(self) -> list[str]:
        """Keys the file has but the loader never asked for: typos and
        settings dropped in an earlier version."""
        return [
            path
            for path in _leaf_paths(self._raw)
            if not any(
                path == asked or path.startswith(asked + ".")
                for asked in self._asked
            )
        ]


def config_path(path: Path | None = None) -> Path:
    """Which config.yaml to read: the argument, then MOONLAN_CONFIG,
    then config.yaml in the working directory."""
    if path is not None:
        return path
    from_env = os.environ.get(CONFIG_PATH_ENV)
    return Path(from_env) if from_env else DEFAULT_CONFIG_PATH


def load_config(path: Path | None = None) -> Config:
    """Reads config.yaml; missing fields get default values.

    The MOONLAN_DEMO=1 environment variable enables demo mode
    regardless of the configuration, and MOONLAN_CONFIG points at an
    alternative file.
    """
    cfg = Config()
    path = config_path(path)
    raw: dict = {}
    exists = path.exists()
    if exists:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    r = _Reader(raw)
    d = Config()  # the defaults every "not in the file" answer comes from

    cfg.listen_host = r.get("listen.host", d.listen_host, str)
    cfg.listen_port = r.get("listen.port", d.listen_port, int)

    cfg.snmp = SnmpConfig(
        community=r.get("snmp.community", d.snmp.community, str),
        timeout=r.get("snmp.timeout", d.snmp.timeout, int),
        retries=r.get("snmp.retries", d.snmp.retries, int),
        retries_on_break=r.get(
            "snmp.retries_on_break", d.snmp.retries_on_break, int
        ),
        host_budget_seconds=r.get(
            "snmp.host_budget_seconds", d.snmp.host_budget_seconds, int
        ),
        dead_oid_strikes=r.get(
            "snmp.dead_oid_strikes", d.snmp.dead_oid_strikes, int
        ),
        dead_oid_cooldown_scans=r.get(
            "snmp.dead_oid_cooldown_scans", d.snmp.dead_oid_cooldown_scans,
            int,
        ),
    )

    cfg.switches, cfg.switch_snmp, switch_problems = parse_switches(
        r.get("switches", d.switches), cfg.snmp
    )
    cfg.routers = r.get("routers", d.routers, _as_str_list)
    cfg.scan_interval_minutes = r.get(
        "scan_interval_minutes", d.scan_interval_minutes, int
    )
    cfg.ping_interval_seconds = r.get(
        "ping_interval_seconds", d.ping_interval_seconds, int
    )
    cfg.counters_interval_seconds = r.get(
        "counters_interval_seconds", d.counters_interval_seconds, int
    )
    cfg.db_path = r.get("db_path", d.db_path, str)
    cfg.unmanaged_threshold = r.get(
        "unmanaged_threshold", d.unmanaged_threshold, int
    )
    cfg.monitored_by_default = r.get(
        "monitored_by_default", d.monitored_by_default, bool
    )
    cfg.host_grace_hours = r.get("host_grace_hours", d.host_grace_hours, float)
    cfg.host_retention_days = r.get(
        "host_retention_days", d.host_retention_days, float
    )
    cfg.offline_group_threshold = r.get(
        "offline_group_threshold", d.offline_group_threshold, int
    )
    cfg.trunk_group_threshold = r.get(
        "trunk_group_threshold", d.trunk_group_threshold, int
    )
    cfg.ip_confirm_hours = r.get(
        "ip_confirm_hours", d.ip_confirm_hours, float
    )
    cfg.new_host_confirm_scans = r.get(
        "new_host_confirm_scans", d.new_host_confirm_scans, int
    )
    cfg.filter_suspect_macs = r.get(
        "filter_suspect_macs", d.filter_suspect_macs, bool
    )
    cfg.place_trunk_only_hosts = r.get(
        "place_trunk_only_hosts", d.place_trunk_only_hosts, bool
    )
    cfg.known_bridges = [
        entry.strip().lower()
        for entry in r.get("known_bridges", d.known_bridges, _as_str_list)
        if entry.strip()
    ]
    cfg.uplink_ports = [
        entry.strip()
        for entry in r.get("uplink_ports", d.uplink_ports, _as_str_list)
        if entry.strip()
    ]

    cfg.loop_detection = LoopDetectionConfig(
        enabled=r.get(
            "loop_detection.enabled", d.loop_detection.enabled, bool
        ),
        profiles=r.get("loop_detection.profiles", [], list),
    )

    t = d.thresholds
    cfg.thresholds = Thresholds(
        errors_per_minute=r.get(
            "thresholds.errors_per_minute", t.errors_per_minute, float
        ),
        error_ratio_percent=r.get(
            "thresholds.error_ratio_percent", t.error_ratio_percent, float
        ),
        discards_per_minute=r.get(
            "thresholds.discards_per_minute", t.discards_per_minute, float
        ),
        port_utilization_percent=r.get(
            "thresholds.port_utilization_percent",
            t.port_utilization_percent, float,
        ),
        port_alarm_cycles=r.get(
            "thresholds.port_alarm_cycles", t.port_alarm_cycles, int
        ),
        mass_down_hosts=r.get(
            "thresholds.mass_down_hosts", t.mass_down_hosts, int
        ),
        corruption_hamming_bits=r.get(
            "thresholds.corruption_hamming_bits", t.corruption_hamming_bits, int
        ),
        corruption_macs_threshold=r.get(
            "thresholds.corruption_macs_threshold",
            t.corruption_macs_threshold, int,
        ),
        stp_changes_per_cycle=r.get(
            "thresholds.stp_changes_per_cycle", t.stp_changes_per_cycle, int
        ),
        flaps_per_window=r.get(
            "thresholds.flaps_per_window", t.flaps_per_window, int
        ),
        flap_window_minutes=r.get(
            "thresholds.flap_window_minutes", t.flap_window_minutes, float
        ),
    )

    n = d.notifications
    cfg.notifications = NotificationsConfig(
        cooldown_seconds=r.get(
            "notifications.cooldown_seconds", n.cooldown_seconds, int
        ),
        flap_count=r.get("notifications.flap_count", n.flap_count, int),
        flap_window_seconds=r.get(
            "notifications.flap_window_seconds", n.flap_window_seconds, int
        ),
        flap_quiet_seconds=r.get(
            "notifications.flap_quiet_seconds", n.flap_quiet_seconds, int
        ),
        email=EmailConfig(
            enabled=r.get("notifications.email.enabled", n.email.enabled, bool),
            smtp_host=r.get(
                "notifications.email.smtp_host", n.email.smtp_host, str
            ),
            smtp_port=r.get(
                "notifications.email.smtp_port", n.email.smtp_port, int
            ),
            starttls=r.get(
                "notifications.email.starttls", n.email.starttls, bool
            ),
            username=r.get(
                "notifications.email.username", n.email.username, str
            ),
            password=r.get(
                "notifications.email.password", n.email.password, str
            ),
            mail_from=r.get(
                "notifications.email.mail_from", n.email.mail_from, str
            ),
            mail_to=r.get(
                "notifications.email.mail_to", n.email.mail_to, _as_str_list
            ),
        ),
        telegram=TelegramConfig(
            enabled=r.get(
                "notifications.telegram.enabled", n.telegram.enabled, bool
            ),
            bot_token=r.get(
                "notifications.telegram.bot_token", n.telegram.bot_token, str
            ),
            chat_ids=r.get(
                "notifications.telegram.chat_ids", n.telegram.chat_ids,
                _as_str_list,
            ),
        ),
        syslog=SyslogConfig(
            enabled=r.get(
                "notifications.syslog.enabled", n.syslog.enabled, bool
            ),
            host=r.get("notifications.syslog.host", n.syslog.host, str),
            port=r.get("notifications.syslog.port", n.syslog.port, int),
        ),
    )

    # Routing is read per alarm type, so a misspelled type shows up as
    # an unknown key instead of quietly never matching anything
    for alarm_type, channels in DEFAULT_ALARM_NOTIFY.items():
        cfg.alarm_notify[alarm_type] = r.get(
            f"alarm_notify.{alarm_type}", list(channels), _as_str_list
        )

    cfg.report = ConfigReport(
        path=str(path.resolve() if exists else path), exists=exists,
        values=r.values,
        unknown=r.unknown_keys(),
        problems=switch_problems,
    )

    if os.environ.get("MOONLAN_DEMO") == "1":
        cfg.demo = True

    return cfg
