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
class Config:
    listen_host: str = "0.0.0.0"
    listen_port: int = 8080
    snmp: SnmpConfig = field(default_factory=SnmpConfig)
    switches: list[str] = field(default_factory=list)
    routers: list[str] = field(default_factory=list)
    scan_interval_minutes: int = 10
    ping_interval_seconds: int = 60
    db_path: str = "moonlan.db"
    unmanaged_threshold: int = 3  # hosts per port; 0 disables pseudo-switches
    demo: bool = False


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
        cfg.db_path = str(raw.get("db_path", cfg.db_path))
        cfg.unmanaged_threshold = int(
            raw.get("unmanaged_threshold", cfg.unmanaged_threshold)
        )

    if os.environ.get("MOONLAN_DEMO") == "1":
        cfg.demo = True

    return cfg
