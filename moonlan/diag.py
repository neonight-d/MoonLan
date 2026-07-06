"""Диагностика коммутатора по SNMP: как MoonLan видит устройство.

Запуск:  python -m moonlan.diag <ip> [--community public] [--timeout 2]

Community и timeout по умолчанию берутся из config.yaml. Утилита ничего
не пишет в БД и не требует запущенного сервиса. Вывод предназначен для
разбора проблем построения топологии: несмаппленные bridge-порты,
поддержка LAG-MIB, видимость соседних коммутаторов в FDB.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter

from .config import load_config
from .snmp_collector import (
    IF_TYPE_ETHERNET,
    OID_BRIDGE_ADDRESS,
    OID_FDB_PORT,
    OID_IF_DESCR,
    OID_IF_NAME,
    OID_IF_OPER_STATUS,
    OID_IF_PHYS_ADDRESS,
    OID_IF_TYPE,
    OID_LAG_ATTACHED_ID,
    OID_PORT_IFINDEX,
    OID_Q_FDB_PORT,
    OID_SYS_DESCR,
    OID_SYS_NAME,
    SnmpCollector,
    _fmt_mac,
)

MAX_IF_ROWS = 40


def _section(title: str) -> None:
    print(f"\n=== {title} ===")


async def _own_macs_light(
    collector: SnmpCollector, ip: str
) -> tuple[str | None, set[str]]:
    """sysName и own_macs соседа (bridge MAC + ifPhysAddress), без FDB."""
    sys_name = await collector._get(ip, OID_SYS_NAME)
    if sys_name is None:
        return None, set()
    macs: set[str] = set()
    bridge = await collector._get(ip, OID_BRIDGE_ADDRESS)
    if bridge is not None:
        macs.add(_fmt_mac(bytes(bridge)))
    async for _suffix, value in collector._walk(ip, OID_IF_PHYS_ADDRESS):
        raw = bytes(value)
        if len(raw) == 6 and any(raw):
            macs.add(_fmt_mac(raw))
    return str(sys_name), macs


async def run_diag(
    ip: str, community: str, timeout: int, config_switches: list[str]
) -> None:
    collector = SnmpCollector(community=community, timeout=timeout)

    sys_name = await collector._get(ip, OID_SYS_NAME)
    if sys_name is None:
        sys.exit(
            f"{ip} не отвечает по SNMP. Проверьте community, timeout "
            f"и доступность устройства."
        )

    # 1. Общие сведения
    _section("1. Общие сведения")
    print(f"sysName:    {sys_name}")
    sys_descr = await collector._get(ip, OID_SYS_DESCR)
    print(f"sysDescr:   {sys_descr if sys_descr is not None else '—'}")
    bridge = await collector._get(ip, OID_BRIDGE_ADDRESS)
    bridge_mac = _fmt_mac(bytes(bridge)) if bridge is not None else ""
    print(f"bridge MAC: {bridge_mac or '—'}")

    # 2. Интерфейсы
    _section("2. Интерфейсы (ifTable)")
    if_types: dict[int, int] = {}
    if_names: dict[int, str] = {}
    if_oper: dict[int, int] = {}
    async for suffix, value in collector._walk(ip, OID_IF_DESCR):
        if_names[suffix[0]] = str(value).strip()
    async for suffix, value in collector._walk(ip, OID_IF_NAME):
        name = str(value).strip()
        if name:
            if_names[suffix[0]] = name
    async for suffix, value in collector._walk(ip, OID_IF_TYPE):
        if_types[suffix[0]] = int(value)
    async for suffix, value in collector._walk(ip, OID_IF_OPER_STATUS):
        if_oper[suffix[0]] = int(value)
    indexes = sorted(set(if_names) | set(if_types) | set(if_oper))
    physical = [i for i in indexes if if_types.get(i) == IF_TYPE_ETHERNET]
    print(
        f"записей ifTable: {len(indexes)}, "
        f"из них физических (ifType={IF_TYPE_ETHERNET}): {len(physical)}"
    )
    for i in indexes[:MAX_IF_ROWS]:
        oper = {1: "up", 2: "down"}.get(if_oper.get(i, 0), "?")
        print(
            f"  ifIndex {i:>4}  type {if_types.get(i, '?'):>4}  "
            f"oper {oper:<4}  {if_names.get(i, '')}"
        )
    if len(indexes) > MAX_IF_ROWS:
        print(f"  … и ещё {len(indexes) - MAX_IF_ROWS} строк")

    # 3. dot1dBasePortIfIndex
    _section("3. dot1dBasePortIfIndex (bridge-port -> ifIndex)")
    port_map: dict[int, int] = {}
    async for suffix, value in collector._walk(ip, OID_PORT_IFINDEX):
        port_map[suffix[0]] = int(value)
    if not port_map:
        print("таблица пуста")
    for bridge_port in sorted(port_map):
        print(f"  bridge-port {bridge_port:>4} -> ifIndex {port_map[bridge_port]}")

    # 4. own_macs
    _section("4. own_macs")
    own_macs: set[str] = set()
    if bridge_mac:
        own_macs.add(bridge_mac)
    async for _suffix, value in collector._walk(ip, OID_IF_PHYS_ADDRESS):
        raw = bytes(value)
        if len(raw) == 6 and any(raw):
            own_macs.add(_fmt_mac(raw))
    print(f"всего: {len(own_macs)} (MAC management-IP из ARP сюда не входит)")
    for mac in sorted(own_macs)[:10]:
        print(f"  {mac}")
    if len(own_macs) > 10:
        print(f"  … и ещё {len(own_macs) - 10}")

    # 5. FDB
    _section("5. FDB (таблица MAC-адресов)")
    fdb: dict[str, int] = {}  # MAC -> bridge-port (первое вхождение)
    for label, oid in (("BRIDGE-MIB", OID_FDB_PORT), ("Q-BRIDGE-MIB", OID_Q_FDB_PORT)):
        count = 0
        async for suffix, value in collector._walk(ip, oid):
            count += 1
            mac = ":".join(f"{octet:02x}" for octet in suffix[-6:])
            fdb.setdefault(mac, int(value))
        print(f"{label}: {count} записей")
    print(f"уникальных MAC: {len(fdb)}")
    per_port = Counter(fdb.values())
    for bridge_port in sorted(per_port):
        if bridge_port == 0:
            mapped = "порт 0 (CPU / сам коммутатор)"
        elif bridge_port in port_map:
            mapped = f"ifIndex {port_map[bridge_port]}"
        else:
            mapped = "НЕТ в dot1dBasePortIfIndex (синтетический ifIndex "
            mapped += f"{-bridge_port})"
        print(f"  bridge-port {bridge_port:>4}: {per_port[bridge_port]:>4} MAC, {mapped}")

    # 6. LAG-MIB
    _section("6. IEEE8023-LAG-MIB (dot3adAggPortAttachedAggID)")
    lag: dict[int, int] = {}
    async for suffix, value in collector._walk(ip, OID_LAG_ATTACHED_ID):
        lag[suffix[0]] = int(value)
    if not lag:
        print("записей нет — LAG-MIB не поддерживается или недоступен")
    else:
        print(f"записей: {len(lag)}")
        for member in sorted(lag):
            note = (
                "  <- член агрегата"
                if lag[member] not in (0, member)
                else ""
            )
            print(f"  ifIndex {member:>4} -> aggregate {lag[member]}{note}")

    # 7. Другие коммутаторы из config.switches
    _section("7. Другие коммутаторы из config.yaml в FDB этого устройства")
    others = [other for other in config_switches if other != ip]
    if not others:
        print("в config.yaml нет других коммутаторов")
    for other_ip in others:
        other_name, other_macs = await _own_macs_light(collector, other_ip)
        if other_name is None:
            print(f"{other_ip}: не отвечает по SNMP — пропущен")
            continue
        seen = {mac: fdb[mac] for mac in other_macs if mac in fdb}
        if seen:
            where = ", ".join(
                f"{mac} на bridge-port {bp}" for mac, bp in sorted(seen.items())
            )
            print(f"{other_ip} ({other_name}): ВИДЕН — {where}")
        else:
            print(
                f"{other_ip} ({other_name}): НЕ виден в FDB "
                f"(проверено MAC: {len(other_macs)})"
            )


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m moonlan.diag",
        description="Диагностика коммутатора по SNMP (только чтение, БД не трогает)",
    )
    parser.add_argument("ip", help="IP-адрес коммутатора")
    parser.add_argument(
        "--community", help="SNMP community (по умолчанию из config.yaml)"
    )
    parser.add_argument(
        "--timeout", type=int, help="таймаут SNMP в секундах (по умолчанию из config.yaml)"
    )
    args = parser.parse_args()
    cfg = load_config()
    asyncio.run(
        run_diag(
            args.ip,
            args.community or cfg.snmp.community,
            args.timeout or cfg.snmp.timeout,
            cfg.switches,
        )
    )


if __name__ == "__main__":
    main()
