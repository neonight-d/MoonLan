"""Опрос коммутатора по SNMP v2c.

Собираем минимум, нужный для построения топологии:
- sysName, sysDescr                        (SNMPv2-MIB)
- список интерфейсов, скорость, статус     (IF-MIB)
- базовый MAC моста                        (BRIDGE-MIB, dot1dBaseBridgeAddress)
- соответствие bridge-port -> ifIndex      (BRIDGE-MIB, dot1dBasePortIfIndex)
- таблица пересылки MAC -> bridge-port     (BRIDGE-MIB, dot1dTpFdbPort;
                                            Q-BRIDGE-MIB, dot1qTpFdbPort)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from pysnmp.hlapi.v3arch.asyncio import (
    CommunityData,
    ContextData,
    ObjectIdentity,
    ObjectType,
    SnmpEngine,
    UdpTransportTarget,
    get_cmd,
    walk_cmd,
)

log = logging.getLogger(__name__)

# OID'ы (числовые, чтобы не зависеть от загрузки MIB-файлов)
OID_SYS_NAME = "1.3.6.1.2.1.1.5.0"
OID_SYS_DESCR = "1.3.6.1.2.1.1.1.0"
OID_IF_DESCR = "1.3.6.1.2.1.2.2.1.2"          # ifDescr.<ifIndex>
OID_IF_NAME = "1.3.6.1.2.1.31.1.1.1.1"        # ifName.<ifIndex>
OID_IF_OPER_STATUS = "1.3.6.1.2.1.2.2.1.8"    # 1=up, 2=down
OID_IF_HIGH_SPEED = "1.3.6.1.2.1.31.1.1.1.15" # Мбит/с
OID_BRIDGE_ADDRESS = "1.3.6.1.2.1.17.1.1.0"   # dot1dBaseBridgeAddress
OID_PORT_IFINDEX = "1.3.6.1.2.1.17.1.4.1.2"   # dot1dBasePortIfIndex.<port>
OID_FDB_PORT = "1.3.6.1.2.1.17.4.3.1.2"       # dot1dTpFdbPort.<6 байт MAC>
OID_Q_FDB_PORT = "1.3.6.1.2.1.17.7.1.2.2.1.2" # dot1qTpFdbPort.<fdbId>.<6 байт MAC>
OID_ARP_PHYS = "1.3.6.1.2.1.4.22.1.2"         # ipNetToMediaPhysAddress.<ifIndex>.<IP>
# dot3adAggPortAttachedAggID.<ifIndex члена> -> ifIndex агрегата (IEEE8023-LAG-MIB)
OID_LAG_ATTACHED_ID = "1.2.840.10006.300.43.1.2.1.1.13"
OID_PVID = "1.3.6.1.2.1.17.7.1.4.5.1.1"       # dot1qPvid.<bridge-port>
OID_VLAN_NAME = "1.3.6.1.2.1.17.7.1.4.3.1.1"  # dot1qVlanStaticName.<VLAN ID>


@dataclass
class PortInfo:
    if_index: int
    name: str = ""
    oper_up: bool = False
    speed_mbps: int = 0


@dataclass
class SwitchData:
    """Всё, что удалось собрать с одного коммутатора."""

    ip: str
    reachable: bool = False
    sys_name: str = ""
    sys_descr: str = ""
    bridge_mac: str = ""
    ports: dict[int, PortInfo] = field(default_factory=dict)   # ifIndex -> порт
    fdb: dict[str, int] = field(default_factory=dict)          # MAC -> ifIndex
    lag_members: dict[int, int] = field(default_factory=dict)  # ifIndex члена -> ifIndex агрегата
    port_pvid: dict[int, int] = field(default_factory=dict)    # ifIndex -> PVID (untagged VLAN)
    vlan_names: dict[int, str] = field(default_factory=dict)   # VLAN ID -> имя


def _fmt_mac(raw: bytes) -> str:
    return ":".join(f"{b:02x}" for b in raw)


class SnmpCollector:
    def __init__(self, community: str, timeout: int = 2, retries: int = 1):
        self._community = CommunityData(community, mpModel=1)  # v2c
        self._timeout = timeout
        self._retries = retries
        self._engine = SnmpEngine()

    async def _target(self, host: str) -> UdpTransportTarget:
        return await UdpTransportTarget.create(
            (host, 161), timeout=self._timeout, retries=self._retries
        )

    async def _get(self, host: str, oid: str):
        """GET одного значения; None при ошибке."""
        error_ind, error_status, _, var_binds = await get_cmd(
            self._engine,
            self._community,
            await self._target(host),
            ContextData(),
            ObjectType(ObjectIdentity(oid)),
        )
        if error_ind or error_status:
            log.debug("%s GET %s: %s", host, oid, error_ind or error_status)
            return None
        return var_binds[0][1]

    async def _walk(self, host: str, oid: str):
        """WALK поддерева; отдаёт пары (суффикс OID, значение)."""
        base = tuple(int(x) for x in oid.split("."))
        objects = walk_cmd(
            self._engine,
            self._community,
            await self._target(host),
            ContextData(),
            ObjectType(ObjectIdentity(oid)),
            lexicographicMode=False,
        )
        async for error_ind, error_status, _, var_binds in objects:
            if error_ind or error_status:
                log.debug("%s WALK %s: %s", host, oid, error_ind or error_status)
                return
            for name, value in var_binds:
                suffix = tuple(name)[len(base):]
                yield suffix, value

    async def collect(self, host: str) -> SwitchData:
        """Полный опрос одного коммутатора."""
        data = SwitchData(ip=host)

        sys_name = await self._get(host, OID_SYS_NAME)
        if sys_name is None:
            log.warning("Коммутатор %s не отвечает по SNMP", host)
            return data

        data.reachable = True
        data.sys_name = str(sys_name)
        sys_descr = await self._get(host, OID_SYS_DESCR)
        data.sys_descr = str(sys_descr) if sys_descr is not None else ""

        bridge_mac = await self._get(host, OID_BRIDGE_ADDRESS)
        if bridge_mac is not None:
            data.bridge_mac = _fmt_mac(bytes(bridge_mac))

        # Интерфейсы. Название порта — ifName; ifDescr лишь запасной вариант:
        # D-Link кладёт в ifDescr модель и прошивку целиком.
        async for suffix, value in self._walk(host, OID_IF_DESCR):
            if_index = suffix[0]
            data.ports[if_index] = PortInfo(if_index=if_index, name=str(value))
        async for suffix, value in self._walk(host, OID_IF_NAME):
            port = data.ports.get(suffix[0])
            name = str(value).strip()
            if port and name:
                port.name = name
        async for suffix, value in self._walk(host, OID_IF_OPER_STATUS):
            port = data.ports.get(suffix[0])
            if port:
                port.oper_up = int(value) == 1
        async for suffix, value in self._walk(host, OID_IF_HIGH_SPEED):
            port = data.ports.get(suffix[0])
            if port:
                port.speed_mbps = int(value)

        # LACP: членство физических портов в агрегатах. Если коммутатор
        # не поддерживает IEEE8023-LAG-MIB, walk просто ничего не отдаст.
        async for suffix, value in self._walk(host, OID_LAG_ATTACHED_ID):
            member, aggregate = suffix[0], int(value)
            if aggregate and aggregate != member:
                data.lag_members[member] = aggregate

        # bridge-port -> ifIndex
        port_to_ifindex: dict[int, int] = {}
        async for suffix, value in self._walk(host, OID_PORT_IFINDEX):
            port_to_ifindex[suffix[0]] = int(value)

        # VLAN: PVID портов (Q-BRIDGE-MIB, индекс — bridge-port) и имена VLAN
        async for suffix, value in self._walk(host, OID_PVID):
            if_index = port_to_ifindex.get(suffix[0])
            if if_index is not None:
                data.port_pvid[if_index] = int(value)
        async for suffix, value in self._walk(host, OID_VLAN_NAME):
            data.vlan_names[suffix[-1]] = str(value).strip()

        # Таблица MAC-адресов: BRIDGE-MIB и Q-BRIDGE-MIB (у Q-BRIDGE в
        # суффиксе перед MAC стоит fdbId, поэтому берём последние 6 байт)
        for fdb_oid in (OID_FDB_PORT, OID_Q_FDB_PORT):
            async for suffix, value in self._walk(host, fdb_oid):
                bridge_port = int(value)
                if bridge_port == 0:  # 0 — MAC самого коммутатора / CPU
                    continue
                mac = ":".join(f"{octet:02x}" for octet in suffix[-6:])
                if mac in data.fdb:
                    continue
                if_index = port_to_ifindex.get(bridge_port)
                if if_index is not None:
                    data.fdb[mac] = if_index

        log.info(
            "%s (%s): портов %d, MAC-адресов %d",
            data.sys_name, host, len(data.ports), len(data.fdb),
        )
        return data

    async def collect_arp(self, host: str) -> dict[str, str]:
        """ARP-таблица устройства (маршрутизатора): MAC -> IP.

        Индекс ipNetToMediaPhysAddress — <ifIndex>.<4 октета IP>,
        значение — MAC (6 байт).
        """
        arp: dict[str, str] = {}
        async for suffix, value in self._walk(host, OID_ARP_PHYS):
            raw = bytes(value)
            if len(raw) != 6:
                continue
            ip = ".".join(str(octet) for octet in suffix[-4:])
            arp[_fmt_mac(raw)] = ip
        log.info("ARP с %s: записей %d", host, len(arp))
        return arp
