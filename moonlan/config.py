"""Loading the MoonLan configuration from a YAML file."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_CONFIG_PATH = Path("config.yaml")


@dataclass
class SnmpConfig:
    community: str = "public"
    timeout: int = 2
    retries: int = 1


@dataclass
class Thresholds:
    errors_per_minute: float = 10.0
    port_utilization_percent: float = 90.0


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
    email: EmailConfig = field(default_factory=EmailConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    syslog: SyslogConfig = field(default_factory=SyslogConfig)


# Which alarm types go to which channels (a channel still has to be
# enabled in the notifications section to actually send anything)
DEFAULT_ALARM_NOTIFY: dict[str, list[str]] = {
    "host_down": ["email", "telegram", "syslog"],
    "switch_down": ["email", "telegram", "syslog"],
    "port_errors": ["syslog"],
    "port_util": ["telegram", "syslog"],
    "new_mac": ["syslog"],
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
    thresholds: Thresholds = field(default_factory=Thresholds)
    notifications: NotificationsConfig = field(default_factory=NotificationsConfig)
    alarm_notify: dict[str, list[str]] = field(
        default_factory=lambda: dict(DEFAULT_ALARM_NOTIFY)
    )
    demo: bool = False


def _as_str_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [str(value)]


def load_config(path: Path | None = None) -> Config:
    """Reads config.yaml; missing fields get default values.

    The MOONLAN_DEMO=1 environment variable enables demo mode
    regardless of the configuration.
    """
    cfg = Config()
    path = path or DEFAULT_CONFIG_PATH

    if path.exists():
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        listen = raw.get("listen", {})
        cfg.listen_host = str(listen.get("host", cfg.listen_host))
        cfg.listen_port = int(listen.get("port", cfg.listen_port))

        snmp = raw.get("snmp", {})
        cfg.snmp = SnmpConfig(
            community=str(snmp.get("community", "public")),
            timeout=int(snmp.get("timeout", 2)),
            retries=int(snmp.get("retries", 1)),
        )

        cfg.switches = [str(s) for s in raw.get("switches", [])]
        cfg.routers = [str(r) for r in raw.get("routers", [])]
        cfg.scan_interval_minutes = int(raw.get("scan_interval_minutes", 10))
        cfg.ping_interval_seconds = int(
            raw.get("ping_interval_seconds", cfg.ping_interval_seconds)
        )
        cfg.counters_interval_seconds = int(
            raw.get("counters_interval_seconds", cfg.counters_interval_seconds)
        )
        cfg.db_path = str(raw.get("db_path", cfg.db_path))
        cfg.unmanaged_threshold = int(
            raw.get("unmanaged_threshold", cfg.unmanaged_threshold)
        )

        thr = raw.get("thresholds") or {}
        cfg.thresholds = Thresholds(
            errors_per_minute=float(thr.get("errors_per_minute", 10)),
            port_utilization_percent=float(
                thr.get("port_utilization_percent", 90)
            ),
        )

        notif = raw.get("notifications") or {}
        email = notif.get("email") or {}
        telegram = notif.get("telegram") or {}
        syslog = notif.get("syslog") or {}
        cfg.notifications = NotificationsConfig(
            cooldown_seconds=int(notif.get("cooldown_seconds", 300)),
            email=EmailConfig(
                enabled=bool(email.get("enabled", False)),
                smtp_host=str(email.get("smtp_host", "")),
                smtp_port=int(email.get("smtp_port", 587)),
                starttls=bool(email.get("starttls", True)),
                username=str(email.get("username", "")),
                password=str(email.get("password", "")),
                mail_from=str(email.get("mail_from", "")),
                mail_to=_as_str_list(email.get("mail_to")),
            ),
            telegram=TelegramConfig(
                enabled=bool(telegram.get("enabled", False)),
                bot_token=str(telegram.get("bot_token", "")),
                chat_ids=_as_str_list(telegram.get("chat_ids")),
            ),
            syslog=SyslogConfig(
                enabled=bool(syslog.get("enabled", False)),
                host=str(syslog.get("host", "127.0.0.1")),
                port=int(syslog.get("port", 514)),
            ),
        )

        routing = raw.get("alarm_notify") or {}
        for alarm_type, channels in routing.items():
            cfg.alarm_notify[str(alarm_type)] = _as_str_list(channels)

    if os.environ.get("MOONLAN_DEMO") == "1":
        cfg.demo = True

    return cfg
