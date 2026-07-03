"""Демо-режим: виртуальная сеть, чтобы посмотреть MoonLan без коммутаторов."""

from __future__ import annotations

import random

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
