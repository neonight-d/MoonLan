"""SNMP diagnostics for a switch: how MoonLan sees the device.

Usage:  python -m moonlan.diag <ip> [--community public] [--timeout 2]
        python -m moonlan.diag --topology

Community and timeout default to the values from config.yaml. The tool
writes nothing to the database and does not need the running service.
The output is meant for debugging topology inference: unmapped
bridge-ports, LAG-MIB support, visibility of neighboring switches in
the FDB. --topology polls every switch from config.yaml and prints the
inferred tree: the root, the branch split, the uplinks and the links.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter

from .config import load_config
from .topology import infer_tree, normalized_fdb, switch_sightings
from .snmp_collector import (
    PHYSICAL_IF_TYPES,
    OID_BRIDGE_ADDRESS,
    OID_FDB_PORT,
    OID_IF_DESCR,
    OID_IF_HIGH_SPEED,
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
    SwitchData,
    _fmt_mac,
    infer_lag_groups,
)

MAX_IF_ROWS = 40


def _section(title: str) -> None:
    print(f"\n=== {title} ===")


def _lag_group_line(bridge_port: int, members: list[int], speeds: dict[int, int]) -> str:
    """'LAG on bridge-port 1: members 1, 2 (2×1000 Mbit/s)'."""
    member_speeds = [speeds.get(m, 0) for m in members]
    if member_speeds and len(set(member_speeds)) == 1 and member_speeds[0]:
        speed = f"{len(members)}×{member_speeds[0]} Mbit/s"
    else:
        speed = f"total {sum(member_speeds)} Mbit/s"
    member_list = ", ".join(str(m) for m in members)
    return f"LAG on bridge-port {bridge_port}: members {member_list} ({speed})"


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
    if_speeds: dict[int, int] = {}
    async for suffix, value in collector._walk(ip, OID_IF_HIGH_SPEED):
        if_speeds[suffix[0]] = int(value)
    indexes = sorted(set(if_names) | set(if_types) | set(if_oper))
    physical = [i for i in indexes if if_types.get(i) in PHYSICAL_IF_TYPES]
    types_str = "/".join(str(t) for t in sorted(PHYSICAL_IF_TYPES))
    print(
        f"ifTable entries: {len(indexes)}, "
        f"physical (ifType {types_str}): {len(physical)}"
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
    # LAG composition inferred from the dot1dBasePortIfIndex gaps
    physical = {i for i in indexes if if_types.get(i) in PHYSICAL_IF_TYPES}
    synthetic = {bp for bp in per_port if bp != 0 and bp not in port_map}
    groups = infer_lag_groups(physical, set(port_map), synthetic)
    print("inferred LAG groups (from missing bridge-ports):")
    if not groups:
        print("  none")
    for bridge_port, members in sorted(groups.items()):
        print(f"  {_lag_group_line(bridge_port, members, if_speeds)}")

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


async def run_topology_view(community: str, timeout: int, cfg) -> None:
    """Section 8: poll every configured switch and print the inferred tree."""
    _section("8. Topology view")
    if not cfg.switches:
        sys.exit("no switches in config.yaml")
    collector = SnmpCollector(community=community, timeout=timeout)
    print(f"polling {len(cfg.switches)} switches from config.yaml…")
    collected = list(
        await asyncio.gather(*(collector.collect(ip) for ip in cfg.switches))
    )
    # Like the server: add the management-IP MAC from the routers' ARP
    if cfg.routers:
        merged: dict[str, str] = {}
        for table in await asyncio.gather(
            *(collector.collect_arp(ip) for ip in cfg.routers)
        ):
            merged.update(table)
        ip_to_mac = {ip: mac for mac, ip in merged.items()}
        for sw in collected:
            mac = ip_to_mac.get(sw.ip)
            if mac:
                sw.own_macs.add(mac)

    switches = [sw for sw in collected if sw.reachable]
    for sw in collected:
        if not sw.reachable:
            print(f"{sw.ip}: does not respond to SNMP — excluded")
    if not switches:
        sys.exit("no reachable switches")

    by_ip = {sw.ip: sw for sw in switches}
    fdb = normalized_fdb(switches)
    switches_on_port, sees = switch_sightings(switches, fdb)
    links, uplinks, info = infer_tree(switches, switches_on_port, sees)

    def label(ip: str) -> str:
        sw = by_ip.get(ip)
        return f"{sw.sys_name} ({ip})" if sw and sw.sys_name else ip

    def port_name(ip: str, port) -> str:
        if port is None:
            return "?"
        sw = by_ip[ip]
        p = sw.ports.get(port)
        return p.name if p and p.name else str(port)

    root_ip = info["root"]
    print(f"root: {label(root_ip)}, "
          f"sees {len(sees[root_ip])} of {len(switches) - 1} switches")
    print("branches by root port:")
    if not info["branches"]:
        print("  none")
    for port, members in sorted(info["branches"].items()):
        names = ", ".join(label(ip) for ip in members)
        print(f"  {port_name(root_ip, port)}: {names}")
    print("uplinks:")
    for ip, port in sorted(uplinks.items()):
        print(f"  {label(ip)}: {port_name(ip, port)}")
    print("LAG groups:")
    any_groups = False
    for sw in switches:
        speeds = {p.if_index: p.speed_mbps for p in sw.ports.values()}
        for bridge_port, members in sorted(sw.lag_groups.items()):
            any_groups = True
            print(f"  {label(sw.ip)}: "
                  f"{_lag_group_line(bridge_port, members, speeds)}")
    if not any_groups:
        print("  none")
    print("links:")
    if not links:
        print("  none")
    for link in links:
        trunk = "  (LAG trunk)" if link["lag"] and link["lag"].get("trunk") else ""
        lacp = (
            f"  (LACP ×{link['lag']['count']})"
            if link["lag"] and link["lag"].get("count", 0) > 1
            else ""
        )
        print(
            f"  {label(link['a'])} [{link['a_port']}] — "
            f"{label(link['b'])} [{link['b_port']}]{trunk}{lacp}"
        )
    if info["unplaced"]:
        print("unplaced (not visible from the root):")
        for ip in info["unplaced"]:
            print(f"  {label(ip)}")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m moonlan.diag",
        description="SNMP diagnostics for a switch (read-only, does not touch the DB)",
    )
    parser.add_argument("ip", nargs="?", help="switch IP address")
    parser.add_argument(
        "--community", help="SNMP community (defaults to config.yaml)"
    )
    parser.add_argument(
        "--timeout", type=int, help="SNMP timeout in seconds (defaults to config.yaml)"
    )
    parser.add_argument(
        "--topology", action="store_true",
        help="poll all switches from config.yaml and print the inferred topology",
    )
    args = parser.parse_args()
    if not args.topology and not args.ip:
        parser.error("an ip is required unless --topology is given")
    cfg = load_config()
    community = args.community or cfg.snmp.community
    timeout = args.timeout or cfg.snmp.timeout
    if args.topology:
        asyncio.run(run_topology_view(community, timeout, cfg))
    else:
        asyncio.run(run_diag(args.ip, community, timeout, cfg.switches))


if __name__ == "__main__":
    main()
