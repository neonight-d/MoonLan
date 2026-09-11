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


def _as_str_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [str(value)]


_MISSING = object()
SECRET_KEYS = ("password", "bot_token", "community")


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
    )

    cfg.switches = r.get("switches", d.switches, _as_str_list)
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
    )

    if os.environ.get("MOONLAN_DEMO") == "1":
        cfg.demo = True

    return cfg
