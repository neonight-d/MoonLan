"""Демо-режим: виртуальная сеть, чтобы посмотреть MoonLan без коммутаторов.

Звезда из пяти коммутаторов: ядро и четыре луча. Демонстрирует все
возможности v0.4.x: корректные прямые связи (лучи видят друг друга через
ядро, но ложных связей между ними нет), LACP-агрегат 2×1G между ядром
и первым лучом, неуправляемый коммутатор с пятью хостами за одним
портом второго луча, PVID и имена VLAN на портах. Лучи видят ядро под
интерфейсным MAC, а не под базовым (проверка own_macs); четвёртый луч
не видит ядро вовсе — связь строится по односторонней видимости.
"""

from __future__ import annotations

import random
import time

from .db import Database
from .snmp_collector import PortInfo, SwitchData

random.seed(7)  # чтобы демо-сеть была одинаковой при каждом запуске

VLAN_NAMES = {1: "default", 8: "office", 11: "ipmi"}
LAG_IFINDEX = 1000  # ifIndex логического порта Po1


def _rand_mac(prefix: str = "02:4d:4c") -> str:
    return prefix + ":" + ":".join(f"{random.randint(0, 255):02x}" for _ in range(3))


def _switch(ip: str, name: str, mac_octet: int) -> SwitchData:
    sw = SwitchData(
        ip=ip, reachable=True, sys_name=name,
        sys_descr="MoonLan demo switch, 26 ports",
        bridge_mac=f"02:4d:4c:00:00:{mac_octet:02x}",
    )
    # Как у реальных коммутаторов: кроме базового MAC есть интерфейсный,
    # и в чужих FDB устройство видно именно под ним
    sw.own_macs = {sw.bridge_mac, f"02:4d:4c:00:01:{mac_octet:02x}"}
    for i in range(1, 27):
        sw.ports[i] = PortInfo(if_index=i, name=f"Gi0/{i}", oper_up=False, speed_mbps=1000)
    sw.vlan_names = dict(VLAN_NAMES)
    return sw


def _iface_mac(sw: SwitchData) -> str:
    return next(m for m in sw.own_macs if m != sw.bridge_mac)


def demo_network() -> list[SwitchData]:
    """Звезда: ядро + четыре луча, LACP, pseudo-коммутатор, VLAN."""
    core = _switch("10.0.0.10", "core-sw", 1)
    ray1 = _switch("10.0.0.21", "access-sw-1", 2)
    ray2 = _switch("10.0.0.22", "access-sw-2", 3)
    ray3 = _switch("10.0.0.23", "access-sw-3", 4)
    ray4 = _switch("10.0.0.24", "access-sw-4", 5)
    switches = [core, ray1, ray2, ray3, ray4]
    rays = [ray1, ray2, ray3, ray4]

    # LACP 2×1G между ядром (порты 1 и 25) и первым лучом (порты 25 и 26)
    for sw, members in ((core, (1, 25)), (ray1, (25, 26))):
        sw.ports[LAG_IFINDEX] = PortInfo(
            if_index=LAG_IFINDEX, name="Po1", oper_up=True, speed_mbps=2000
        )
        sw.lag_members = {m: LAG_IFINDEX for m in members}
        for m in members:
            sw.ports[m].oper_up = True

    # Магистрали ядро—лучи: у ядра порт на каждый луч, у луча — аплинк 24
    # (у ray1 аплинк — агрегат). FDB заполняем так, как выглядит реальная
    # звезда: ядро видит базовые MAC лучей, лучи видят ядро под
    # интерфейсным MAC (проверка own_macs), друг друга — через аплинк.
    # ray4 не видит ядро вовсе — проверка односторонней видимости.
    core_port_to_ray = {ray1.ip: 1, ray2.ip: 2, ray3.ip: 3, ray4.ip: 4}
    for ray in (ray2, ray3, ray4):
        core.ports[core_port_to_ray[ray.ip]].oper_up = True
        ray.ports[24].oper_up = True
    for ray in rays:
        uplink = 25 if ray is ray1 else 24
        core.fdb[ray.bridge_mac] = core_port_to_ray[ray.ip]
        if ray is not ray4:
            ray.fdb[_iface_mac(core)] = uplink
        for other in rays:
            if other is not ray:
                ray.fdb[other.bridge_mac] = uplink

    def connect_host(sw: SwitchData, port: int, vlan: int) -> str:
        mac = _rand_mac()
        sw.ports[port].oper_up = True
        sw.fdb[mac] = port
        sw.port_pvid[port] = vlan
        if sw is not core:  # ядро видит хосты лучей через свои магистрали
            core.fdb[mac] = core_port_to_ray[sw.ip]
        return mac

    # Хосты: первый луч — офис, второй — офис + pseudo-коммутатор,
    # третий — смешанный, в ядре — серверы в VLAN 11 (ipmi)
    for port in range(1, 9):
        connect_host(ray1, port, 1 if port <= 4 else 8)
    for port in range(1, 5):
        connect_host(ray2, port, 8)
    for _ in range(5):  # 5 хостов на одном порту — неуправляемый коммутатор
        connect_host(ray2, 5, 8)
    for port in range(1, 7):
        connect_host(ray3, port, 1 if port % 2 else 8)
    for port in range(1, 4):
        connect_host(ray4, port, 1)
    for port in (12, 13, 14):
        connect_host(core, port, 11)

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
