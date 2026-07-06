"""Построение топологии сети из данных, собранных с коммутаторов.

Алгоритм v0.4:

1. LACP: ifIndex физических портов-членов агрегата приводится к ifIndex
   агрегата, поэтому агрегат участвует во всех расчётах (связи, uplink,
   привязка хостов) как один логический порт.
2. Прямые связи. Коммутатор опознаётся в чужих FDB по любому MAC из
   own_macs (базовый MAC моста, MAC интерфейсов, MAC management-IP), а не
   только по dot1dBaseBridgeAddress: реальные кадры уходят с MAC
   интерфейсов. Для пары (A, B) берём порт pA, где A видит B, и порт pB,
   где B видит A. Обозначим S(A, p) — множество *других опрошенных
   коммутаторов*, видимых в FDB A на порту p. Связь A(pA)—B(pB) прямая
   тогда и только тогда, когда S(A, pA) ∩ S(B, pB) = ∅: через два порта,
   смотрящих друг на друга, не должен быть виден один и тот же третий
   коммутатор. Это отсекает ложные связи «луч — луч» в звезде, где каждый
   луч видит все остальные лучи через центр. Если видимость односторонняя
   (B не видит A нигде), применяется правило исключения: B — прямой сосед
   A на pA, если среди коммутаторов S(A, pA) нет такого D, «за» которым
   находится B (D видит B на порту, где не видит A). Такая связь строится
   с b_port = «?».
3. Uplink-порты: порт, где виден любой другой коммутатор, либо порт
   с подозрительно большим числом MAC (> UPLINK_MAC_THRESHOLD) — за таким
   почти наверняка другой коммутатор, пусть даже неуправляемый.
4. Конечные устройства. MAC-адреса на остальных портах — хосты.
   Один и тот же MAC может быть виден с нескольких коммутаторов;
   хост привязывается к тому порту, где кроме него меньше всего
   других MAC (это и есть порт непосредственного подключения).
5. Неуправляемые коммутаторы. Если на не-uplink порту найдено больше
   unmanaged_threshold хостов, за портом почти наверняка коммутатор
   без SNMP или точка доступа: хосты группируются под pseudo-узлом.
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
UNMANAGED_THRESHOLD = 3  # хостов на порту; больше — рисуем pseudo-коммутатор


@dataclass
class TopologyState:
    """Потокобезопасное хранилище текущей топологии."""

    switches: list[dict] = field(default_factory=list)
    links: list[dict] = field(default_factory=list)
    hosts: list[dict] = field(default_factory=list)
    pseudo_switches: list[dict] = field(default_factory=list)
    vlan_names: dict[int, str] = field(default_factory=dict)
    last_scan: float = 0.0
    scanning: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def update(
        self,
        switches: list[dict],
        links: list[dict],
        hosts: list[dict],
        pseudo_switches: list[dict],
        vlan_names: dict[int, str],
    ) -> None:
        with self._lock:
            self.switches = switches
            self.links = links
            self.hosts = hosts
            self.pseudo_switches = pseudo_switches
            self.vlan_names = vlan_names
            self.last_scan = time.time()

    def as_dict(self) -> dict:
        with self._lock:
            return {
                "switches": self.switches,
                "links": self.links,
                "hosts": self.hosts,
                "pseudo_switches": self.pseudo_switches,
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


def _make_link(a: SwitchData, pa: int, b: SwitchData, pb: int | None) -> dict:
    """Связь A(pA)—B(pB); pb=None — порт B неизвестен (односторонняя видимость)."""
    link = {
        "a": a.ip,
        "b": b.ip,
        "a_port": _port_name(a, pa),
        "b_port": _port_name(b, pb) if pb is not None else "?",
        "speed_mbps": 0,
        "lag": None,
    }
    a_members, a_speed = _lag_info(a, pa)
    b_members, b_speed = _lag_info(b, pb) if pb is not None else ([], 0)
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
        port = a.ports.get(pa) or (b.ports.get(pb) if pb is not None else None)
        link["speed_mbps"] = port.speed_mbps if port else 0
    return link


def _one_way_link(
    a: SwitchData,
    pa: int | None,
    b: SwitchData,
    pb: int | None,
    sees: dict[str, dict[str, int]],
    switches_on_port: dict[str, dict[int, set[str]]],
) -> dict | None:
    """Запасной алгоритм: связь при односторонней видимости.

    viewer видит target на порту p, target не видит viewer нигде.
    target — прямой сосед, если среди других коммутаторов S(viewer, p)
    нет такого D, «за» которым находится target: D видит target на порту,
    на котором D не видит viewer.
    """
    viewer, port, target = (a, pa, b) if pb is None else (b, pb, a)
    candidates = switches_on_port[viewer.ip].get(port, set())

    def behind(d_ip: str) -> bool:
        viewer_port = sees[d_ip].get(viewer.ip)
        return any(
            target.ip in ips and p != viewer_port
            for p, ips in switches_on_port[d_ip].items()
        )

    direct = target.ip in candidates and not any(
        d_ip != target.ip and behind(d_ip) for d_ip in candidates
    )
    if not direct:
        log.warning(
            "FDB неполна: %s видит %s, но %s не видит %s, и прямого соседа "
            "определить не удалось — связь не строится",
            viewer.ip, target.ip, target.ip, viewer.ip,
        )
        return None
    log.info(
        "Связь %s—%s построена по односторонней видимости (%s не видит %s)",
        viewer.ip, target.ip, target.ip, viewer.ip,
    )
    return _make_link(viewer, port, target, None)


def build_topology(
    collected: Iterable[SwitchData],
    unmanaged_threshold: int = UNMANAGED_THRESHOLD,
) -> tuple[list[dict], list[dict], list[dict], list[dict], dict[int, str]]:
    """Превращает данные опроса в узлы и связи для схемы."""
    switches = [sw for sw in collected if sw.reachable]
    # Любой MAC из own_macs опознаёт коммутатор; bridge_mac — на случай,
    # если own_macs не заполнен (например, старые данные)
    mac_to_switch: dict[str, SwitchData] = {}
    for sw in switches:
        for mac in sw.own_macs | ({sw.bridge_mac} if sw.bridge_mac else set()):
            mac_to_switch.setdefault(mac, sw)
    # FDB с приведением членов LACP к логическому порту-агрегату
    fdb = {
        sw.ip: {
            mac: sw.lag_members.get(if_index, if_index)
            for mac, if_index in sw.fdb.items()
        }
        for sw in switches
    }

    # S(A, p): какие коммутаторы видны на каждом порту A;
    # sees[A][B] — порт, где A видит B (если на нескольких — где чаще)
    switches_on_port: dict[str, dict[int, set[str]]] = {}
    sees: dict[str, dict[str, int]] = {}
    for sw in switches:
        per_port: dict[int, set[str]] = {}
        sightings: dict[str, Counter] = {}
        for mac, if_index in fdb[sw.ip].items():
            neighbor = mac_to_switch.get(mac)
            if neighbor is None or neighbor.ip == sw.ip:
                continue
            per_port.setdefault(if_index, set()).add(neighbor.ip)
            sightings.setdefault(neighbor.ip, Counter())[if_index] += 1
        switches_on_port[sw.ip] = per_port
        sees[sw.ip] = {
            ip: counts.most_common(1)[0][0] for ip, counts in sightings.items()
        }

    # 1. Прямые связи: пересечение множеств видимых коммутаторов пусто
    links: list[dict] = []
    for i, a in enumerate(switches):
        for b in switches[i + 1:]:
            pa = sees[a.ip].get(b.ip)
            pb = sees[b.ip].get(a.ip)
            if pa is None and pb is None:
                continue
            if pa is None or pb is None:
                link = _one_way_link(a, pa, b, pb, sees, switches_on_port)
                if link:
                    links.append(link)
                continue
            if switches_on_port[a.ip][pa] & switches_on_port[b.ip][pb]:
                continue  # через эти порты виден третий коммутатор — связь не прямая
            links.append(_make_link(a, pa, b, pb))

    # 2. Uplink-порты: виден другой коммутатор или слишком много MAC
    uplink_ports: dict[str, set[int]] = {}
    for sw in switches:
        uplink_ports[sw.ip] = set(switches_on_port[sw.ip])
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
    hosts_per_port: dict[tuple[str, int], list[dict]] = {}
    for mac, (_, sw_ip, if_index) in sorted(best_location.items()):
        sw = switch_by_ip[sw_ip]
        host = {
            "mac": mac,
            "switch": sw_ip,
            "port": _port_name(sw, if_index),
            "vlan": sw.port_pvid.get(if_index, 0),
            "name": "",  # имена и IP добавляются из БД (ARP/DNS)
        }
        hosts.append(host)
        hosts_per_port.setdefault((sw_ip, if_index), []).append(host)

    # 5. Много хостов на не-uplink порту — за ним неуправляемый коммутатор
    pseudo_switches: list[dict] = []
    if unmanaged_threshold > 0:
        for (sw_ip, if_index), port_hosts in sorted(hosts_per_port.items()):
            if len(port_hosts) <= unmanaged_threshold:
                continue
            pseudo_id = f"pseudo:{sw_ip}:{if_index}"
            for host in port_hosts:
                host["via"] = pseudo_id
            pseudo_switches.append({
                "id": pseudo_id,
                "switch": sw_ip,
                "port": _port_name(switch_by_ip[sw_ip], if_index),
                "host_count": len(port_hosts),
            })

    switch_dicts = [{
        "ip": sw.ip,
        "name": sw.sys_name or sw.ip,
        "mac": sw.bridge_mac,
        "descr": sw.sys_descr,
        # Только физические порты (ifType 6): агрегаты, CPU- и
        # VLAN-интерфейсы не завышают счётчик
        "ports_total": sum(1 for p in sw.ports.values() if p.is_physical),
        "ports_up": sum(
            1 for p in sw.ports.values() if p.is_physical and p.oper_up
        ),
    } for sw in switches]

    # Имена VLAN со всех коммутаторов (при совпадении ID первый выигрывает)
    vlan_names: dict[int, str] = {}
    for sw in switches:
        for vlan_id, name in sw.vlan_names.items():
            if name:
                vlan_names.setdefault(vlan_id, name)

    return switch_dicts, links, hosts, pseudo_switches, vlan_names
