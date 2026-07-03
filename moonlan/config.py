"""Загрузка конфигурации MoonLan из YAML-файла."""

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
    scan_interval_minutes: int = 10
    db_path: str = "moonlan.db"
    demo: bool = False


def load_config(path: Path | None = None) -> Config:
    """Читает config.yaml; отсутствующие поля получают значения по умолчанию.

    Переменная окружения MOONLAN_DEMO=1 включает демо-режим
    независимо от конфигурации.
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
        cfg.scan_interval_minutes = int(raw.get("scan_interval_minutes", 10))
        cfg.db_path = str(raw.get("db_path", cfg.db_path))

    if os.environ.get("MOONLAN_DEMO") == "1":
        cfg.demo = True

    return cfg
