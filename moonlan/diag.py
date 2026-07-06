"""SNMP diagnostics for a switch: how MoonLan sees the device.

Usage:  python -m moonlan.diag <ip> [--community public] [--timeout 2]

Community and timeout default to the values from config.yaml. The tool
writes nothing to the database and does not need the running service.
The output is meant for debugging topology inference: unmapped
bridge-ports, LAG-MIB support, visibility of neighboring switches in
the FDB.
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
    """A neighbor's sysName and own_macs (bridge MAC + ifPhysAddress), no FDB."""
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
            f"{ip} does not respond to SNMP. Check the community, "
            f"the timeout and device availability."
        )

    # 1. General information
    _section("1. General information")
    print(f"sysName:    {sys_name}")
    sys_descr = await collector._get(ip, OID_SYS_DESCR)
    print(f"sysDescr:   {sys_descr if sys_descr is not None else '—'}")
    bridge = await collector._get(ip, OID_BRIDGE_ADDRESS)
    bridge_mac = _fmt_mac(bytes(bridge)) if bridge is not None else ""
    print(f"bridge MAC: {bridge_mac or '—'}")

    # 2. Interfaces
    _section("2. Interfaces (ifTable)")
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
        f"ifTable entries: {len(indexes)}, "
        f"physical (ifType={IF_TYPE_ETHERNET}): {len(physical)}"
    )
    for i in indexes[:MAX_IF_ROWS]:
        oper = {1: "up", 2: "down"}.get(if_oper.get(i, 0), "?")
        print(
            f"  ifIndex {i:>4}  type {if_types.get(i, '?'):>4}  "
            f"oper {oper:<4}  {if_names.get(i, '')}"
        )
    if len(indexes) > MAX_IF_ROWS:
        print(f"  … and {len(indexes) - MAX_IF_ROWS} more rows")

    # 3. dot1dBasePortIfIndex
    _section("3. dot1dBasePortIfIndex (bridge-port -> ifIndex)")
    port_map: dict[int, int] = {}
    async for suffix, value in collector._walk(ip, OID_PORT_IFINDEX):
        port_map[suffix[0]] = int(value)
    if not port_map:
        print("the table is empty")
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
    print(f"total: {len(own_macs)} (the management-IP MAC from ARP is not included)")
    for mac in sorted(own_macs)[:10]:
        print(f"  {mac}")
    if len(own_macs) > 10:
        print(f"  … and {len(own_macs) - 10} more")

    # 5. FDB
    _section("5. FDB (MAC address table)")
    fdb: dict[str, int] = {}  # MAC -> bridge-port (first occurrence)
    for label, oid in (("BRIDGE-MIB", OID_FDB_PORT), ("Q-BRIDGE-MIB", OID_Q_FDB_PORT)):
        count = 0
        async for suffix, value in collector._walk(ip, oid):
            count += 1
            mac = ":".join(f"{octet:02x}" for octet in suffix[-6:])
            fdb.setdefault(mac, int(value))
        print(f"{label}: {count} entries")
    print(f"unique MACs: {len(fdb)}")
    per_port = Counter(fdb.values())
    for bridge_port in sorted(per_port):
        if bridge_port == 0:
            mapped = "port 0 (CPU / the switch itself)"
        elif bridge_port in port_map:
            mapped = f"ifIndex {port_map[bridge_port]}"
        else:
            mapped = "NOT in dot1dBasePortIfIndex (synthetic ifIndex "
            mapped += f"{-bridge_port})"
        print(f"  bridge-port {bridge_port:>4}: {per_port[bridge_port]:>4} MACs, {mapped}")

    # 6. LAG-MIB
    _section("6. IEEE8023-LAG-MIB (dot3adAggPortAttachedAggID)")
    lag: dict[int, int] = {}
    async for suffix, value in collector._walk(ip, OID_LAG_ATTACHED_ID):
        lag[suffix[0]] = int(value)
    if not lag:
        print("no entries — LAG-MIB is not supported or not available")
    else:
        print(f"entries: {len(lag)}")
        for member in sorted(lag):
            note = (
                "  <- aggregate member"
                if lag[member] not in (0, member)
                else ""
            )
            print(f"  ifIndex {member:>4} -> aggregate {lag[member]}{note}")

    # 7. Other switches from config.switches
    _section("7. Other switches from config.yaml in this device's FDB")
    others = [other for other in config_switches if other != ip]
    if not others:
        print("no other switches in config.yaml")
    for other_ip in others:
        other_name, other_macs = await _own_macs_light(collector, other_ip)
        if other_name is None:
            print(f"{other_ip}: does not respond to SNMP — skipped")
            continue
        seen = {mac: fdb[mac] for mac in other_macs if mac in fdb}
        if seen:
            where = ", ".join(
                f"{mac} on bridge-port {bp}" for mac, bp in sorted(seen.items())
            )
            print(f"{other_ip} ({other_name}): VISIBLE — {where}")
        else:
            print(
                f"{other_ip} ({other_name}): NOT visible in the FDB "
                f"(MACs checked: {len(other_macs)})"
            )


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m moonlan.diag",
        description="SNMP diagnostics for a switch (read-only, does not touch the DB)",
    )
    parser.add_argument("ip", help="switch IP address")
    parser.add_argument(
        "--community", help="SNMP community (defaults to config.yaml)"
    )
    parser.add_argument(
        "--timeout", type=int, help="SNMP timeout in seconds (defaults to config.yaml)"
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
