"""Демо-режим: виртуальная сеть, чтобы посмотреть MoonLan без коммутаторов."""

from __future__ import annotations

import random
import time

from .db import Database
from .snmp_collector import PortInfo, SwitchData

random.seed(7)  # чтобы демо-сеть была одинаковой при каждом запуске


def _rand_mac(prefix: str = "02:4d:4c") -> str:
    return prefix + ":" + ":".join(f"{random.randint(0, 255):02x}" for _ in range(3))


def demo_network() -> list[SwitchData]:
    """Три коммутатора звездой от ядра и полтора десятка хостов."""
    core = SwitchData(
        ip="10.0.0.1", reachable=True, sys_name="core-sw",
        sys_descr="MoonLan demo core switch, 24 ports",
        bridge_mac="02:4d:4c:00:00:01",
    )
    access1 = SwitchData(
        ip="10.0.0.2", reachable=True, sys_name="access-sw-1",
        sys_descr="MoonLan demo access switch, 24 ports",
        bridge_mac="02:4d:4c:00:00:02",
    )
    access2 = SwitchData(
        ip="10.0.0.3", reachable=True, sys_name="access-sw-2",
        sys_descr="MoonLan demo access switch, 24 ports",
        bridge_mac="02:4d:4c:00:00:03",
    )
    switches = [core, access1, access2]

    for sw in switches:
        for i in range(1, 25):
            sw.ports[i] = PortInfo(
                if_index=i, name=f"Gi0/{i}",
                oper_up=False, speed_mbps=1000,
            )

    def connect_host(sw: SwitchData, port: int) -> None:
        mac = _rand_mac()
        sw.ports[port].oper_up = True
        sw.fdb[mac] = port
        # ядро «видит» этот MAC через свой uplink к соответствующему коммутатору
        if sw is access1:
            core.fdb[mac] = 1
        elif sw is access2:
            core.fdb[mac] = 2

    # Магистрали: core Gi0/1 <-> access1 Gi0/24, core Gi0/2 <-> access2 Gi0/24
    for port in (1, 2):
        core.ports[port].oper_up = True
    for sw in (access1, access2):
        sw.ports[24].oper_up = True
        sw.fdb[core.bridge_mac] = 24
    core.fdb[access1.bridge_mac] = 1
    core.fdb[access2.bridge_mac] = 2

    for port in range(1, 9):
        connect_host(access1, port)
    for port in range(1, 7):
        connect_host(access2, port)
    for port in (10, 11, 12):  # серверы в ядре
        connect_host(core, port + 2)

    return switches


_journal_seeded = False


def enrich_db(db: Database, hosts: list[dict]) -> None:
    """Данные v0.3 для демо: IP, имена, состояние ping, события журнала.

    Показывает все состояния UI: зелёный (отвечает), серый (не отвечает),
    синий (без IP — ping невозможен), хосты с именем и без.
    """
    global _journal_seeded
    now = time.time()

    for i, host in enumerate(hosts):
        mac = host["mac"]
        if i % 5 == 4:
            continue  # у части хостов IP так и не определился
        db.set_ips({mac: f"10.0.99.{10 + i}"})
        if i % 3 != 2:  # у части хостов имени нет — только IP
            db.set_name(mac, f"pc-{i + 1:02d}.demo.lan")
        if i in (1, 6):  # пара выключенных: отвечали 15 минут назад
            db.set_ping_state(mac, up=False, last_ok=now - 15 * 60)
        else:
            db.set_ping_state(mac, up=True, last_ok=now)

    if not _journal_seeded and len(hosts) > 6:
        _journal_seeded = True
        db.add_event(now - 40 * 60, "host_down", hosts[6]["mac"], "10.0.99.16")
        db.add_event(now - 15 * 60, "host_down", hosts[1]["mac"], "pc-02.demo.lan")
        db.add_event(now - 5 * 60, "host_up", hosts[3]["mac"], "pc-04.demo.lan")
