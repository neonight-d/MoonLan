"""Построение топологии сети из данных, собранных с коммутаторов.

Алгоритм v0.1 (упрощённый, но рабочий для типовых сетей):

1. Uplink-порты. Если на порту коммутатора A виден базовый MAC
   коммутатора B — это магистраль A—B. Дополнительно порт считается
   магистральным, если за ним видно подозрительно много MAC-адресов
   (по умолчанию > 8): за таким портом почти наверняка другой
   коммутатор, пусть даже неуправляемый.
2. Конечные устройства. MAC-адреса на остальных портах — хосты.
   Один и тот же MAC может быть виден с нескольких коммутаторов;
   хост привязывается к тому порту, где кроме него меньше всего
   других MAC (это и есть порт непосредственного подключения).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Iterable

from .snmp_collector import SwitchData

UPLINK_MAC_THRESHOLD = 8


@dataclass
class TopologyState:
    """Потокобезопасное хранилище текущей топологии."""

    switches: list[dict] = field(default_factory=list)
    links: list[dict] = field(default_factory=list)
    hosts: list[dict] = field(default_factory=list)
    last_scan: float = 0.0
    scanning: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def update(self, switches: list[dict], links: list[dict], hosts: list[dict]) -> None:
        with self._lock:
            self.switches = switches
            self.links = links
            self.hosts = hosts
            self.last_scan = time.time()

    def as_dict(self) -> dict:
        with self._lock:
            return {
                "switches": self.switches,
                "links": self.links,
                "hosts": self.hosts,
                "last_scan": self.last_scan,
                "scanning": self.scanning,
            }

    def search(self, query: str) -> list[dict]:
        q = query.strip().lower()
        if not q:
            return []
        found: list[dict] = []
        with self._lock:
            for sw in self.switches:
                haystack = f"{sw['name']} {sw['ip']} {sw.get('mac', '')}".lower()
                if q in haystack:
                    found.append({"type": "switch", **sw})
            for host in self.hosts:
                haystack = (
                    f"{host['mac']} {host.get('name', '')} {host.get('ip', '')}"
                ).lower()
                if q in haystack:
                    found.append({"type": "host", **host})
        return found


def build_topology(collected: Iterable[SwitchData]) -> tuple[list[dict], list[dict], list[dict]]:
    """Превращает данные опроса в узлы и связи для схемы."""
    switches = [sw for sw in collected if sw.reachable]
    mac_to_switch = {sw.bridge_mac: sw for sw in switches if sw.bridge_mac}

    uplink_ports: dict[str, set[int]] = {sw.ip: set() for sw in switches}
    links: list[dict] = []
    seen_pairs: set[frozenset[str]] = set()

    # 1. Магистрали по базовым MAC соседних коммутаторов
    for sw in switches:
        for mac, if_index in sw.fdb.items():
            neighbor = mac_to_switch.get(mac)
            if neighbor is None or neighbor.ip == sw.ip:
                continue
            uplink_ports[sw.ip].add(if_index)
            pair = frozenset((sw.ip, neighbor.ip))
            if pair not in seen_pairs:
                seen_pairs.add(pair)
                port = sw.ports.get(if_index)
                links.append({
                    "a": sw.ip,
                    "b": neighbor.ip,
                    "a_port": port.name if port else str(if_index),
                    "speed_mbps": port.speed_mbps if port else 0,
                })

    # 2. Порты со слишком большим числом MAC — тоже магистрали
    for sw in switches:
        macs_per_port: dict[int, int] = {}
        for if_index in sw.fdb.values():
            macs_per_port[if_index] = macs_per_port.get(if_index, 0) + 1
        for if_index, count in macs_per_port.items():
            if count > UPLINK_MAC_THRESHOLD:
                uplink_ports[sw.ip].add(if_index)

    # 3. Конечные устройства: выбираем порт с минимумом «соседей» по MAC
    best_location: dict[str, tuple[int, str, int]] = {}  # mac -> (macs_on_port, sw_ip, ifIndex)
    switch_macs = set(mac_to_switch)
    for sw in switches:
        macs_per_port: dict[int, int] = {}
        for if_index in sw.fdb.values():
            macs_per_port[if_index] = macs_per_port.get(if_index, 0) + 1
        for mac, if_index in sw.fdb.items():
            if mac in switch_macs or if_index in uplink_ports[sw.ip]:
                continue
            candidate = (macs_per_port[if_index], sw.ip, if_index)
            if mac not in best_location or candidate < best_location[mac]:
                best_location[mac] = candidate

    hosts = []
    for mac, (_, sw_ip, if_index) in sorted(best_location.items()):
        sw = next(s for s in switches if s.ip == sw_ip)
        port = sw.ports.get(if_index)
        hosts.append({
            "mac": mac,
            "switch": sw_ip,
            "port": port.name if port else str(if_index),
            "name": "",  # имена и IP появятся в v0.3 (ARP/DNS/Ping)
        })

    switch_dicts = [{
        "ip": sw.ip,
        "name": sw.sys_name or sw.ip,
        "mac": sw.bridge_mac,
        "descr": sw.sys_descr,
        "ports_total": len(sw.ports),
        "ports_up": sum(1 for p in sw.ports.values() if p.oper_up),
    } for sw in switches]

    return switch_dicts, links, hosts
