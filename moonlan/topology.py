"""Построение топологии сети из данных, собранных с коммутаторов.

Алгоритм v0.4:

1. LACP: ifIndex физических портов-членов агрегата приводится к ifIndex
   агрегата, поэтому агрегат участвует во всех расчётах (связи, uplink,
   привязка хостов) как один логический порт.
2. Прямые связи. Для пары коммутаторов (A, B) берём порт pA, где A видит
   базовый MAC B, и порт pB, где B видит A. Обозначим S(A, p) — множество
   базовых MAC *других опрошенных коммутаторов*, видимых в FDB A на порту p.
   Связь A(pA)—B(pB) прямая тогда и только тогда, когда
   S(A, pA) ∩ S(B, pB) = ∅: через два порта, смотрящих друг на друга,
   не должен быть виден один и тот же третий коммутатор. Это отсекает
   ложные связи «луч — луч» в звезде, где каждый луч видит все остальные
   лучи через центр.
3. Uplink-порты: порт, где виден любой другой коммутатор, либо порт
   с подозрительно большим числом MAC (> UPLINK_MAC_THRESHOLD) — за таким
   почти наверняка другой коммутатор, пусть даже неуправляемый.
4. Конечные устройства. MAC-адреса на остальных портах — хосты.
   Один и тот же MAC может быть виден с нескольких коммутаторов;
   хост привязывается к тому порту, где кроме него меньше всего
   других MAC (это и есть порт непосредственного подключения).
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable

from .snmp_collector import SwitchData

log = logging.getLogger(__name__)

UPLINK_MAC_THRESHOLD = 8


@dataclass
class TopologyState:
    """Потокобезопасное хранилище текущей топологии."""

    switches: list[dict] = field(default_factory=list)
    links: list[dict] = field(default_factory=list)
    hosts: list[dict] = field(default_factory=list)
    vlan_names: dict[int, str] = field(default_factory=dict)
    last_scan: float = 0.0
    scanning: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def update(
        self,
        switches: list[dict],
        links: list[dict],
        hosts: list[dict],
        vlan_names: dict[int, str],
    ) -> None:
        with self._lock:
            self.switches = switches
            self.links = links
            self.hosts = hosts
            self.vlan_names = vlan_names
            self.last_scan = time.time()

    def as_dict(self) -> dict:
        with self._lock:
            return {
                "switches": self.switches,
                "links": self.links,
                "hosts": self.hosts,
                "vlan_names": self.vlan_names,
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


def _port_name(sw: SwitchData, if_index: int) -> str:
    port = sw.ports.get(if_index)
    return port.name if port and port.name else str(if_index)


def _lag_info(sw: SwitchData, agg_index: int) -> tuple[list[str], int]:
    """Имена физических портов-членов агрегата и суммарная скорость."""
    members = sorted(m for m, agg in sw.lag_members.items() if agg == agg_index)
    names = [_port_name(sw, m) for m in members]
    speed = sum(sw.ports[m].speed_mbps for m in members if m in sw.ports)
    return names, speed


def _make_link(a: SwitchData, pa: int, b: SwitchData, pb: int) -> dict:
    link = {
        "a": a.ip,
        "b": b.ip,
        "a_port": _port_name(a, pa),
        "b_port": _port_name(b, pb),
        "speed_mbps": 0,
        "lag": None,
    }
    a_members, a_speed = _lag_info(a, pa)
    b_members, b_speed = _lag_info(b, pb)
    if a_members or b_members:
        members = a_members or b_members
        link["lag"] = {
            "members": members,
            "count": len(members),
            "a_members": a_members,
            "b_members": b_members,
        }
        link["speed_mbps"] = a_speed or b_speed
    else:
        port = a.ports.get(pa) or b.ports.get(pb)
        link["speed_mbps"] = port.speed_mbps if port else 0
    return link


def build_topology(
    collected: Iterable[SwitchData],
) -> tuple[list[dict], list[dict], list[dict], dict[int, str]]:
    """Превращает данные опроса в узлы и связи для схемы."""
    switches = [sw for sw in collected if sw.reachable]
    mac_to_switch = {sw.bridge_mac: sw for sw in switches if sw.bridge_mac}
    # FDB с приведением членов LACP к логическому порту-агрегату
    fdb = {
        sw.ip: {
            mac: sw.lag_members.get(if_index, if_index)
            for mac, if_index in sw.fdb.items()
        }
        for sw in switches
    }

    # S(A, p): чужие базовые MAC по портам; sees[A][mac B] — порт, где A видит B
    switch_macs_on_port: dict[str, dict[int, set[str]]] = {}
    sees: dict[str, dict[str, int]] = {}
    for sw in switches:
        per_port: dict[int, set[str]] = {}
        where: dict[str, int] = {}
        for mac, if_index in fdb[sw.ip].items():
            if mac in mac_to_switch and mac != sw.bridge_mac:
                per_port.setdefault(if_index, set()).add(mac)
                where[mac] = if_index
        switch_macs_on_port[sw.ip] = per_port
        sees[sw.ip] = where

    # 1. Прямые связи: пересечение множеств чужих MAC должно быть пустым
    links: list[dict] = []
    for i, a in enumerate(switches):
        for b in switches[i + 1:]:
            if not a.bridge_mac or not b.bridge_mac:
                continue
            pa = sees[a.ip].get(b.bridge_mac)
            pb = sees[b.ip].get(a.bridge_mac)
            if pa is None and pb is None:
                continue
            if pa is None or pb is None:
                blind, seen = (a, b) if pa is None else (b, a)
                log.warning(
                    "FDB неполна: %s видит %s, но %s не видит %s — связь не строится",
                    seen.ip, blind.ip, blind.ip, seen.ip,
                )
                continue
            if switch_macs_on_port[a.ip][pa] & switch_macs_on_port[b.ip][pb]:
                continue  # через эти порты виден третий коммутатор — связь не прямая
            links.append(_make_link(a, pa, b, pb))

    # 2. Uplink-порты: виден другой коммутатор или слишком много MAC
    uplink_ports: dict[str, set[int]] = {}
    for sw in switches:
        uplink_ports[sw.ip] = set(switch_macs_on_port[sw.ip])
        for if_index, count in Counter(fdb[sw.ip].values()).items():
            if count > UPLINK_MAC_THRESHOLD:
                uplink_ports[sw.ip].add(if_index)

    # 3. Конечные устройства: выбираем порт с минимумом «соседей» по MAC
    best_location: dict[str, tuple[int, str, int]] = {}  # mac -> (macs_on_port, sw_ip, ifIndex)
    switch_macs = set(mac_to_switch)
    for sw in switches:
        macs_per_port = Counter(fdb[sw.ip].values())
        for mac, if_index in fdb[sw.ip].items():
            if mac in switch_macs or if_index in uplink_ports[sw.ip]:
                continue
            candidate = (macs_per_port[if_index], sw.ip, if_index)
            if mac not in best_location or candidate < best_location[mac]:
                best_location[mac] = candidate

    switch_by_ip = {sw.ip: sw for sw in switches}
    hosts = []
    for mac, (_, sw_ip, if_index) in sorted(best_location.items()):
        sw = switch_by_ip[sw_ip]
        hosts.append({
            "mac": mac,
            "switch": sw_ip,
            "port": _port_name(sw, if_index),
            "vlan": sw.port_pvid.get(if_index, 0),
            "name": "",  # имена и IP добавляются из БД (ARP/DNS)
        })

    switch_dicts = [{
        "ip": sw.ip,
        "name": sw.sys_name or sw.ip,
        "mac": sw.bridge_mac,
        "descr": sw.sys_descr,
        "ports_total": len(sw.ports),
        "ports_up": sum(1 for p in sw.ports.values() if p.oper_up),
    } for sw in switches]

    # Имена VLAN со всех коммутаторов (при совпадении ID первый выигрывает)
    vlan_names: dict[int, str] = {}
    for sw in switches:
        for vlan_id, name in sw.vlan_names.items():
            if name:
                vlan_names.setdefault(vlan_id, name)

    return switch_dicts, links, hosts, vlan_names
