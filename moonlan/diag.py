"""SNMP diagnostics for a switch: how MoonLan sees the device.

Usage:  python -m moonlan.diag <ip> [--community public] [--timeout 2]
        python -m moonlan.diag --topology
        python -m moonlan.diag --hosts
        python -m moonlan.diag --port <ip> [--iface Gi0/1] [--watch 3]
        python -m moonlan.diag --config
        python -m moonlan.diag --host <ip|mac>
        python -m moonlan.diag --fdb <ip> [--iface 1/3]
        python -m moonlan.diag --stp
        python -m moonlan.diag --walk <ip> <oid> [--limit 500]

Community and timeout default to the values from config.yaml. The tool
writes nothing to the database and does not need the running service.
The output is meant for debugging topology inference: unmapped
bridge-ports, LAG-MIB support, visibility of neighboring switches in
the FDB. --topology polls every switch from config.yaml and prints the
inferred tree: the root, the branch split, the uplinks and the links.
--hosts compares the FDB, the routers' ARP tables and the database to
show how complete the host inventory is. --port prints the raw error,
discard, octet and packet counters of a switch's ports and, with
--watch, the very rates the alarm engine works with. --host explains
one device (database, FDB, ARP, ping) and --fdb dumps a switch's raw
MAC table with the verdict on every row. --stp prints the raw
dot1dStp* values of every switch next to the verdict they produce, so
"STP is not running here" can be checked rather than believed.
--walk dumps any OID subtree, which is how a private MIB (D-Link
1.3.6.1.4.1.171, HPE 1.3.6.1.4.1.11) gets explored before it becomes
a feature.
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
import time
from collections import Counter

from . import counters, pinger, stp
from .config import (
    SECRET_KEYS,
    SnmpConfig,
    load_config,
    parse_uplink_ports,
)
from .snmpval import as_octets, is_octets
from .corruption import find_suspects, sample_mac
from .counters import CounterStore, Sample
from .topology import (
    detect_bridges,
    suspect_uplink_ports,
    infer_tree,
    lldp_link_candidates,
    merge_lldp_links,
    normalized_fdb,
    switch_sightings,
    trunk_ports,
)
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
    OID_PVID,
    OID_Q_FDB_PORT,
    OID_SYS_DESCR,
    OID_SYS_NAME,
    SnmpCollector,
    SwitchData,
    _fmt_mac,
    infer_lag_groups,
    is_random_mac,
    parse_fdb_entry,
)

MAX_IF_ROWS = 40

# Retry settings are not passed through every diagnostic function; they
# come from config.yaml once, in main(), and every collector built here
# uses them. A CLI run must retry exactly the way the service does, or
# it diagnoses a different machine than the one that is running.
_SNMP = {"retries": SnmpConfig.retries,
         "retries_on_break": SnmpConfig.retries_on_break}


def _make_collector(community: str, timeout: int) -> SnmpCollector:
    return SnmpCollector(
        community=community,
        timeout=timeout,
        retries=_SNMP["retries"],
        retries_on_break=_SNMP["retries_on_break"],
    )


def _addresses(addresses: list[str], limit: int = 3) -> str:
    """'10.0.0.1 and 37 more' — a MikroTik announces one per VLAN."""
    if not addresses:
        return ""
    if len(addresses) <= limit:
        return ", ".join(addresses)
    return f"{', '.join(addresses[:limit])} and {len(addresses) - limit} more"


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
        macs.add(_fmt_mac(as_octets(bridge)))
    async for _suffix, value in collector._walk(ip, OID_IF_PHYS_ADDRESS):
        raw = as_octets(value)
        if len(raw) == 6 and any(raw):
            macs.add(_fmt_mac(raw))
    return str(sys_name), macs


async def run_diag(
    ip: str, community: str, timeout: int, config_switches: list[str]
) -> None:
    collector = _make_collector(community, timeout)

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
    bridge_mac = _fmt_mac(as_octets(bridge)) if bridge is not None else ""
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
        raw = as_octets(value)
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
    collector = _make_collector(community, timeout)
    print(f"polling {len(cfg.switches)} switches from config.yaml…")
    collected = list(
        await asyncio.gather(*(collector.collect(ip) for ip in cfg.switches))
    )
    # Like the server: add the management-IP MAC from the routers' ARP
    arp_by_mac: dict[str, str] = {}
    if cfg.routers:
        for table in await asyncio.gather(
            *(collector.collect_arp(ip) for ip in cfg.routers)
        ):
            arp_by_mac.update(table)
        ip_to_mac = {ip: mac for mac, ip in arp_by_mac.items()}
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
    lldp_pairs = lldp_link_candidates(switches)
    mismatches = merge_lldp_links(links, lldp_pairs, by_ip)

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
    # Everything else carries hosts. A port wrongly listed here takes
    # every device behind it off the map, so the reason is spelled out.
    print("trunk ports (excluded from host binding):")
    for ip, ports in sorted(
        trunk_ports(switches, switches_on_port, uplinks, lldp_pairs).items()
    ):
        listed = ", ".join(
            f"{port_name(ip, i)} ({why})" for i, why in sorted(ports.items())
        )
        print(f"  {label(ip)}: {listed or 'none'}")
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
            f"{label(link['b'])} [{link['b_port']}]"
            f"  source: {link.get('source', 'fdb')}{trunk}{lacp}"
        )
    if info["unplaced"]:
        print("unplaced (not visible from the root):")
        for ip in info["unplaced"]:
            print(f"  {label(ip)}")

    # LLDP: what the devices say about each other, and where that
    # disagrees with what the MAC tables implied
    print("\nLLDP neighbours:")
    any_lldp = False
    for sw in switches:
        for neighbor in sw.lldp_neighbors:
            any_lldp = True
            where = (
                f"{port_name(sw.ip, neighbor.local_ifindex)}"
                f" via {neighbor.port_matched_by}"
                if neighbor.local_ifindex is not None
                else f"lldpLocPortNum {neighbor.local_port_num} (UNMATCHED)"
            )
            caps = ", ".join(sorted(neighbor.cap_enabled)) or "no capabilities TLV"
            if neighbor.local_ifindex in sw.lldp_forwarded:
                flag = "  <- did not come from this cable, not used for links"
            elif neighbor.local_ifindex in sw.lldp_crowded:
                flag = "  <- several devices on this port, no link inferred"
            else:
                flag = ""
            rows = (
                f", {neighbor.rows} table rows merged"
                if neighbor.rows > 1 else ""
            )
            print(
                f"  {label(sw.ip)} [{where}] -> "
                f"{neighbor.sys_name or neighbor.chassis_id} "
                f"({neighbor.chassis_id}) port {neighbor.port_id or '?'}"
                + (f", {_addresses(neighbor.mgmt_ips)}"
                   if neighbor.mgmt_ips else "")
                + f", {caps}{rows}{flag}"
            )
    if not any_lldp:
        print("  none — no switch reports an LLDP neighbour")

    print("\nLLDP vs FDB mismatches:")
    if not mismatches:
        print("  none — every LLDP-confirmed link matches the inference")
    for m in mismatches:
        if m["kind"] == "weak_port":
            print(
                f"  {label(m['side'])}: LLDP puts the link on "
                f"{m['lldp_ports'][0]}, but only from lldpRemLocalPortNum "
                f"({m['matched_by']}); the MAC tables say "
                f"{m['fdb_ports'][0]} and are taken instead"
            )
        elif m["kind"] == "ports":
            print(
                f"  {label(m['a'])} — {label(m['b'])}: LLDP says "
                f"{m['lldp_ports'][0]}/{m['lldp_ports'][1]}, the MAC tables "
                f"suggested {m['fdb_ports'][0]}/{m['fdb_ports'][1]}"
            )
        else:
            print(
                f"  {label(m['a'])} [{m['lldp_ports'][0]}] — "
                f"{label(m['b'])} [{m['lldp_ports'][1]}]: reported by LLDP "
                f"only, the MAC tables do not show this link"
            )

    switch_macs = {mac for sw in switches for mac in sw.own_macs}
    bridges, other_devices, unidentified = detect_bridges(
        switches, switch_macs,
        trunk_ports(switches, switches_on_port, uplinks, lldp_pairs),
    )
    # A way out of the network that nobody has told MoonLan about: the
    # branch to the provider looks like any other access port until it
    # is listed in uplink_ports.
    print("\nports that look like a way out of the network:")
    host_ips: list[tuple[str, str, str]] = []
    for sw in switches:
        for mac, if_index in fdb[sw.ip].items():
            address = arp_by_mac.get(mac, "")
            if address:
                host_ips.append((sw.ip, port_name(sw.ip, if_index), address))
    infrastructure = set(cfg.switches) | set(cfg.routers)
    suspects = suspect_uplink_ports(
        switches, host_ips, parse_uplink_ports(cfg.uplink_ports),
        infrastructure=infrastructure,
        infrastructure_macs={
            mac for mac, ip in arp_by_mac.items() if ip in infrastructure
        },
    )
    if not suspects:
        print("  none")
    for (sw_ip, port), why in sorted(suspects.items()):
        print(f"  {label(sw_ip)} {port}: {why['text']}")
        print(f'    add "{sw_ip}:{port}" to uplink_ports in config.yaml')

    print("\nbridges (LLDP capability 'bridge'):")
    if not bridges:
        print("  none")
    for bridge in bridges:
        print(
            f"  {bridge['name']} ({bridge['chassis_id']})"
            + (f" {_addresses(bridge['mgmt_ips'])}" if bridge["mgmt_ips"] else "")
            + f" behind {label(bridge['switch'])} {bridge['port']}"
            + ("  [trunk port]" if bridge["trunk"] else "")
            + ("  [capability assumed: this agent reports none for anyone]"
               if bridge.get("cap_assumed") else "")
        )
    # These are NOT bridges, and they are not anonymous either: they
    # said what they are. Only the section below is unidentified.
    print("\nother identified LLDP devices (routers, phones, stations):")
    if not other_devices:
        print("  none")
    for device in other_devices:
        print(
            f"  {device['name']} ({device['chassis_id']}) — {device['kind']}"
            + (f", {_addresses(device['mgmt_ips'])}"
               if device["mgmt_ips"] else "")
            + f" behind {label(device['switch'])} {device['port']}"
        )

    print("\nunidentified LLDP devices (no capabilities TLV):")
    if not unidentified:
        print("  none")
    for device in unidentified:
        print(
            f"  {device['name']} ({device['chassis_id']})"
            + (f" {_addresses(device['mgmt_ips'])}" if device["mgmt_ips"] else "")
            + f" behind {label(device['switch'])} {device['port']}"
        )
    if unidentified:
        print(
            "  ^ these sent no capabilities TLV. That proves nothing about "
            "what they are, so they get no node on the map and raise no "
            "alarm — most of them are cameras and phones."
        )


def _subnet(ip: str) -> str:
    parts = ip.split(".")
    return ".".join(parts[:3]) + ".0/24" if len(parts) == 4 else ip


def _db_snapshot(path: str) -> list[dict] | None:
    """Reads the hosts table without touching it (read-only URI)."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        return None
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM hosts").fetchall()
    except sqlite3.DatabaseError:
        return None
    finally:
        conn.close()
    return [dict(row) for row in rows]


async def run_host_inventory(community: str, timeout: int, cfg) -> None:
    """Section 9: how complete the host inventory is and what is missing.

    Polls the FDB of every configured switch and the ARP tables of
    every configured router, then compares the two: devices behind a
    router (or behind an unpolled switch) show up in ARP only and can
    never appear on an L2 map.
    """
    _section("9. Host inventory")
    if not cfg.switches:
        sys.exit("no switches in config.yaml")
    collector = _make_collector(community, timeout)
    collected = list(
        await asyncio.gather(*(collector.collect(ip) for ip in cfg.switches))
    )
    switch_macs = {mac for sw in collected for mac in sw.own_macs}

    print("MAC addresses in the FDB (switch MACs excluded):")
    fdb_macs: set[str] = set()
    for sw in collected:
        if not sw.reachable:
            print(f"  {sw.ip}: does not respond to SNMP")
            continue
        macs = set(sw.fdb) - switch_macs
        fdb_macs |= macs
        print(f"  {sw.sys_name or sw.ip} ({sw.ip}): {len(macs)}")
    print(f"  unique across all switches: {len(fdb_macs)}")

    print("\nARP sources:")
    arp: dict[str, str] = {}
    if not cfg.routers:
        print("  none configured — host IP addresses are unknown")
    for ip in cfg.routers:
        table = await collector.collect_arp(ip)
        print(f"  {ip}: {len(table)} entries")
        arp.update(table)
    arp_macs = set(arp) - switch_macs
    if cfg.routers:
        print(f"  unique MACs from all sources: {len(arp_macs)}")

    print("\nFDB vs ARP:")
    both = fdb_macs & arp_macs
    print(f"  on a switch port and in ARP (located, IP known): {len(both)}")
    print(f"  on a switch port only (no IP known): {len(fdb_macs - arp_macs)}")
    print(
        f"  in ARP only (not on any polled port): {len(arp_macs - fdb_macs)}"
    )

    print("\nKnown IP addresses by subnet:")
    per_subnet: dict[str, list[int]] = {}
    for mac, ip in arp.items():
        if mac in switch_macs:
            continue
        counts = per_subnet.setdefault(_subnet(ip), [0, 0])
        counts[0] += 1
        if mac in fdb_macs:
            counts[1] += 1
    if not per_subnet:
        print("  no ARP data")
    for subnet, (total, located) in sorted(
        per_subnet.items(), key=lambda kv: (-kv[1][0], kv[0])
    ):
        note = "" if located else "   <- no device of this subnet is on any port"
        print(f"  {subnet}: {total} devices, {located} on switch ports{note}")

    print(f"\nDatabase ({cfg.db_path}):")
    rows = _db_snapshot(cfg.db_path)
    if rows is None:
        print("  not readable (the service has not created it yet)")
        return
    grace = cfg.host_grace_hours * 3600
    now = time.time()
    missing = [r for r in rows if r["mac"] not in fdb_macs]
    in_grace = [
        r for r in missing
        if r["switch_ip"] and now - r["last_seen"] < grace
    ]
    print(f"  hosts: {len(rows)}")
    print(
        f"  not in any current FDB: {len(missing)} "
        f"(still within the {cfg.host_grace_hours:g} h grace window: "
        f"{len(in_grace)})"
    )
    print(f"  never located (no switch port): "
          f"{sum(1 for r in rows if not r['switch_ip'])}")
    # Seen too few times to be called a device: stored, but on no map
    # and in no alarm. A large number here means damaged frames.
    print(
        f"  unconfirmed (fewer than {cfg.new_host_confirm_scans} sightings, "
        f"no IP): {sum(1 for r in rows if not r.get('confirmed'))}"
    )
    print(f"  without an IP: {sum(1 for r in rows if not r['ip'])}")
    print(f"  without a name: {sum(1 for r in rows if not r['name'])}")


def _mask(key: str, value) -> str:
    """Credentials are not printed, only whether they are set."""
    if key.rsplit(".", 1)[-1] in SECRET_KEYS and value:
        return "***"
    if isinstance(value, list):
        return "[" + ", ".join(str(v) for v in value) + "]"
    return str(value)


def run_config_audit(cfg) -> None:
    """Section 11: the effective configuration and where it came from.

    A config.yaml written for an older version keeps overriding
    settings whose meaning has changed (errors_per_minute: 10 used to
    count discards too) and misses the ones added since, which then
    apply their defaults silently. Both are visible here.
    """
    _section("11. Effective configuration")
    report = cfg.report
    if report is None:
        sys.exit("configuration report is not available")
    print(
        f"config.yaml: {report.path}"
        + ("" if report.exists else "  (not found — every setting is a default)")
    )
    print(f"\n{'setting':<42} {'value':<28} source")
    for key, value, source in report.values:
        print(f"{key:<42} {_mask(key, value):<28} {source}")

    print(
        f"\nfrom config.yaml: {len(report.overrides)}, "
        f"defaults: {len(report.defaults)}"
    )

    print("\nkeys in config.yaml MoonLan does not know (ignored):")
    if report.unknown:
        for key in report.unknown:
            print(f"  {key}")
        print("  ^ a typo, or a setting removed in a later version")
    else:
        print("  none")

    print("\nkeys missing from config.yaml (defaults apply):")
    missing = report.defaults if report.exists else []
    if missing:
        for key, value, _ in missing:
            print(f"  {key} = {_mask(key, value)}")
    else:
        print("  none")


FDB_TABLES = ((OID_FDB_PORT, 6, "dot1dTpFdbPort"),
              (OID_Q_FDB_PORT, 7, "dot1qTpFdbPort"))


async def _fdb_rows(collector: SnmpCollector, host: str) -> list[dict]:
    """Every MAC-table row of a switch, raw OID and verdict included."""
    rows: list[dict] = []
    for oid, expected_len, table in FDB_TABLES:
        async for suffix, value in collector._walk(host, oid):
            mac, bridge_port, reason = parse_fdb_entry(
                suffix, value, expected_len
            )
            rows.append({
                "table": table,
                "oid": oid + "." + ".".join(str(part) for part in suffix),
                "suffix_len": len(suffix),
                "mac": mac,
                "bridge_port": bridge_port,
                "reason": reason,
            })
    return rows


async def _port_maps(
    collector: SnmpCollector, host: str
) -> tuple[dict[int, int], dict[int, str], dict[int, int]]:
    """(bridge-port -> ifIndex, ifIndex -> name, ifIndex -> PVID)."""
    port_to_ifindex: dict[int, int] = {}
    async for suffix, value in collector._walk(host, OID_PORT_IFINDEX):
        port_to_ifindex[suffix[0]] = int(value)
    names, _speeds = await _port_labels(collector, host)
    pvid: dict[int, int] = {}
    async for suffix, value in collector._walk(host, OID_PVID):
        if_index = port_to_ifindex.get(suffix[0])
        if if_index is not None:
            pvid[if_index] = int(value)
    return port_to_ifindex, names, pvid


def _corruption_section(
    host: str, rows: list[dict], names: dict[int, str],
    port_to_ifindex: dict[int, int], cfg,
) -> None:
    """The distorted copies in this MAC table, grouped by port.

    The database says which addresses are real (confirmed, or holding
    an IP); everything else on the same port is measured against them.
    """
    print("\nsuspected corrupted MACs:")
    db_rows = {r["mac"]: r for r in _db_snapshot(cfg.db_path) or []}
    hosts: list[dict] = []
    pending: set[str] = set()
    for row in rows:
        if row["reason"]:
            continue
        if_index = port_to_ifindex.get(row["bridge_port"])
        port = names.get(if_index, str(row["bridge_port"]))
        hosts.append({"mac": row["mac"], "switch": host, "port": port})
        record = db_rows.get(row["mac"])
        if record is None or (
            not record.get("confirmed") and not record["ip"]
        ):
            pending.add(row["mac"])
    suspects = find_suspects(
        hosts, pending, cfg.thresholds.corruption_hamming_bits
    )
    if not suspects:
        print("  none — every address here is either confirmed or far "
              "from any confirmed one")
        return
    for (_ip, port), found in sorted(suspects.items()):
        sample = sample_mac(found)
        print(f"  {port}: {len(found)} copies of {sample}")
        for s in found:
            record = db_rows.get(s["mac"], {})
            has_ip = record.get("ip") or "no IP"
            print(
                f"    {s['mac']}  {s['distance']} bit(s) from "
                f"{s['sample']}  {has_ip}"
            )
    print(
        f"  ^ addresses within {cfg.thresholds.corruption_hamming_bits} bits "
        f"of a real one on the same port: the switch learned them from "
        f"damaged frames"
    )


async def run_fdb_dump(
    host: str, port_filter: str | None, community: str, timeout: int, cfg
) -> None:
    """Section 12: the raw MAC table with the verdict on every row.

    This is how a claim like "these 32 devices on port 1/3 are not
    real" gets settled: the raw OID and its suffix length are printed
    next to the address they would have produced.
    """
    _section(f"12. MAC table: {host}")
    collector = _make_collector(community, timeout)
    port_to_ifindex, names, _pvid = await _port_maps(collector, host)
    if not names:
        sys.exit(f"{host} does not respond to SNMP")
    rows = await _fdb_rows(collector, host)
    if not rows:
        print("the MAC table is empty")
        return

    accepted = rejected = local_admin = 0
    bad_suffix = 0
    shown = 0
    for row in rows:
        if_index = port_to_ifindex.get(row["bridge_port"])
        name = names.get(if_index, "")
        if row["reason"]:
            rejected += 1
            if "suffix" in row["reason"]:
                bad_suffix += 1
        else:
            accepted += 1
            if is_random_mac(row["mac"]):
                local_admin += 1
        # A rejected row has no port to attribute it to, and those are
        # exactly what a "what is on this port?" question is about, so
        # the filter only applies to accepted rows.
        if port_filter and not row["reason"] and not (
            port_filter == name
            or (if_index is not None and port_filter == str(if_index))
            or port_filter == str(row["bridge_port"])
        ):
            continue
        shown += 1
        where = (
            f"bridge-port {row['bridge_port']}"
            + (f" -> ifIndex {if_index}" if if_index is not None else "")
            + (f" ({name})" if name else "")
        )
        verdict = f"REJECTED: {row['reason']}" if row["reason"] else "ok"
        if not row["reason"] and is_random_mac(row["mac"]):
            verdict += ", locally administered"
        print(
            f"  {row['table']:<14} len {row['suffix_len']}  "
            f"{row['mac'] or '—':<18} {where:<40} {verdict}"
        )
        print(f"      OID {row['oid']}")
    if port_filter and not shown:
        print(f"  no MAC-table row points at port {port_filter}")
    elif port_filter:
        print(f"\n(rejected rows are listed whatever port is asked for: "
              f"they carry no usable port)")

    print(
        f"\nsummary: {len(rows)} rows, accepted {accepted}, "
        f"rejected {rejected} (bad suffix: {bad_suffix}, "
        f"bad MAC: {rejected - bad_suffix})"
    )
    print(f"locally administered (randomized) MACs among accepted: {local_admin}")
    _corruption_section(host, rows, names, port_to_ifindex, cfg)


def _is_mac(text: str) -> bool:
    parts = text.lower().split(":")
    return len(parts) == 6 and all(
        len(p) == 2 and all(c in "0123456789abcdef" for c in p) for p in parts
    )


async def run_host_diag(
    target: str, community: str, timeout: int, cfg
) -> None:
    """Section 13: everything known about one device, and why it is
    considered offline."""
    _section(f"13. Device: {target}")
    target = target.strip().lower()
    mac = target if _is_mac(target) else ""
    ip = "" if mac else target

    rows = _db_snapshot(cfg.db_path) or []
    record = next(
        (r for r in rows if (mac and r["mac"] == mac) or (ip and r["ip"] == ip)),
        None,
    )
    print("database record:")
    if record is None:
        print("  none — MoonLan has never stored this device")
    else:
        mac = mac or record["mac"]
        ip = ip or record["ip"]
        for key in sorted(record):
            value = record[key]
            if key in ("first_seen", "last_seen", "last_ping_ok",
                       "last_arp", "ip_confirmed"):
                value = (
                    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value))
                    if value else "never"
                )
            print(f"  {key:<13} {value}")
    if not mac and not ip:
        sys.exit("give an IP address or a MAC address")

    collector = _make_collector(community, timeout)

    # Which MACs identify a switch: a port carrying one is a trunk, and
    # a device seen only on trunks is behind equipment nobody polls
    switch_macs: set[str] = set()
    for switch in cfg.switches:
        _name, macs = await _own_macs_light(collector, switch)
        switch_macs |= macs

    print("\nMAC tables of the switches:")
    seen_on: list[str] = []
    trunk_only = True
    for switch in cfg.switches:
        if not mac:
            print(f"  {switch}: no MAC known, nothing to look for")
            continue
        port_to_ifindex, names, pvid = await _port_maps(collector, switch)
        if not names:
            print(f"  {switch}: does not respond to SNMP")
            continue
        rows_here = await _fdb_rows(collector, switch)
        trunks = {
            r["bridge_port"] for r in rows_here
            if not r["reason"] and r["mac"] in switch_macs
        }
        found = [r for r in rows_here if r["mac"] == mac]
        if not found:
            print(f"  {switch}: not in the MAC table")
            continue
        for row in found:
            if_index = port_to_ifindex.get(row["bridge_port"])
            name = names.get(if_index, str(if_index))
            state = f"REJECTED ({row['reason']})" if row["reason"] else "accepted"
            trunk = row["bridge_port"] in trunks
            if not row["reason"]:
                seen_on.append(
                    f"{switch} port {name}" + (" (a trunk)" if trunk else "")
                )
                trunk_only = trunk_only and trunk
            print(
                f"  {switch}: port {name}, VLAN "
                f"{pvid.get(if_index, '—')}, {state}"
                + (", trunk port — another switch is behind it" if trunk else "")
            )
            print(f"      OID {row['oid']}")

    print("\nARP tables of the routers:")
    arp_says_ip = ""
    arp_says_mac = ""
    for router in cfg.routers:
        table = await collector.collect_arp(router)
        if not table:
            print(f"  {router}: no answer or empty ARP table")
            continue
        by_ip = {value: key for key, value in table.items()}
        mine = table.get(mac, "")
        holder = by_ip.get(ip, "")
        print(
            f"  {router}: this MAC -> {mine or 'not listed'}; "
            f"this IP -> {holder or 'not listed'}"
        )
        arp_says_ip = arp_says_ip or mine
        arp_says_mac = arp_says_mac or holder
    if not cfg.routers:
        print("  no routers configured — MoonLan cannot confirm IP ownership")

    replied = None
    if ip:
        print(f"\nping {ip}:")
        replied = await pinger.ping(ip)
        print("  replies" if replied else "  no reply")

    print("\nsummary:")
    print("  " + _host_summary(
        record, mac, ip, seen_on, arp_says_mac, replied,
        trunk_only and bool(seen_on),
    ))


def _host_summary(
    record, mac: str, ip: str, seen_on: list[str],
    arp_holder: str, replied: bool | None, trunk_only: bool = False,
) -> str:
    """One sentence on why the device looks the way it looks."""
    if seen_on:
        where = ", ".join(seen_on)
        if not trunk_only:
            return f"the MAC is in the table of {where} — the device is present"
        placed = (
            f"MoonLan draws it on {record['switch_ip']} {record['port']}"
            if record and record["switch_ip"] else "MoonLan has not placed it yet"
        )
        return (
            f"the MAC is visible ONLY on trunk ports ({where}), so the device "
            f"hangs off a switch MoonLan does not poll; its location is "
            f"approximate — {placed}"
        )
    if record is None:
        return "nothing is known about this device"
    parts = ["no switch reports this MAC, so the device counts as offline"]
    if record["last_seen"]:
        hours = (time.time() - record["last_seen"]) / 3600
        parts.append(f"it was last seen on a port {hours:.1f} h ago")
    if ip and replied:
        if arp_holder and arp_holder != mac:
            parts.append(
                f"the address answers but ARP now maps it to {arp_holder} — "
                "another device took it over"
            )
        else:
            parts.append(
                "the address still answers: the device may have changed its "
                "MAC or moved behind equipment MoonLan does not poll"
            )
    elif ip:
        parts.append("the address does not answer either")
    return "; ".join(parts) + "."


WATCH_INTERVAL = 60  # seconds between --watch measurements


async def _port_labels(
    collector: SnmpCollector, host: str
) -> tuple[dict[int, str], dict[int, int]]:
    """ifIndex -> port name and link speed, without a full poll."""
    names: dict[int, str] = {}
    speeds: dict[int, int] = {}
    async for suffix, value in collector._walk(host, OID_IF_DESCR):
        names[suffix[0]] = str(value)
    async for suffix, value in collector._walk(host, OID_IF_NAME):
        name = str(value).strip()
        if name:
            names[suffix[0]] = name
    async for suffix, value in collector._walk(host, OID_IF_HIGH_SPEED):
        speeds[suffix[0]] = int(value)
    return names, speeds


def _select_ports(
    samples: dict[int, Sample],
    names: dict[int, str],
    iface: str | None,
    oper: dict[int, bool] | None = None,
) -> tuple[list[int], list[int]]:
    """(ports to print, the noisy ones among them).

    With --iface, exactly that port. Without it, every port that is up,
    busiest first — and separately the ones with a non-zero error or
    discard counter.

    Until v0.6.4 the noisy ports were ALL this returned, and an empty
    list ended the command. The one switch whose counters are
    suspiciously empty was therefore the one switch the command would
    say nothing about.
    """
    if iface:
        if iface.isdigit() and int(iface) in samples:
            return [int(iface)], []
        wanted = iface.lower()
        chosen = [
            i for i in samples if names.get(i, str(i)).lower() == wanted
        ]
        return chosen, []

    def total(if_index: int, attrs: tuple[str, ...]) -> int:
        s = samples[if_index]
        return sum(getattr(s, a) or 0 for a in attrs)

    noisy = [
        i for i in samples
        if total(i, ("in_errors", "out_errors", "in_discards", "out_discards"))
    ]
    noisy.sort(
        key=lambda i: total(
            i, ("in_errors", "out_errors", "in_discards", "out_discards")
        ),
        reverse=True,
    )
    oper = oper or {}
    shown = [i for i in samples if oper.get(i)] or list(samples)
    shown.sort(key=lambda i: total(i, ("in_octets", "out_octets")), reverse=True)
    return shown, noisy


def _num(value) -> str:
    """A counter, or "no answer" — which is not the same as zero."""
    return "n/a" if value is None else str(value)


def _rate(value: float | None, digits: int = 2) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def _print_columns(columns: dict[str, counters.ColumnStatus]) -> None:
    """Which counter columns the agent answered for, and with what.

    This is the line that separates "no errors" from "we never got an
    answer" — the distinction mb0 turned on.
    """
    print("counter columns:")
    for name in sorted(columns):
        status = columns[name]
        print(f"  {name:<14} {status.oid:<28} {status.verdict()}")


def _print_raw(
    if_index: int,
    s: Sample,
    names: dict[int, str],
    speeds: dict[int, int],
    oper: dict[int, bool],
) -> None:
    name = names.get(if_index, str(if_index))
    status = "up" if oper.get(if_index) else "down"
    print(
        f"  {name:<16} {status:<5} {speeds.get(if_index, 0):>6} Mbit/s  "
        f"errors in/out {_num(s.in_errors)}/{_num(s.out_errors)}  "
        f"discards in/out {_num(s.in_discards)}/{_num(s.out_discards)}"
    )
    print(
        f"  {'':<16} octets in/out {_num(s.in_octets)}/{_num(s.out_octets)}  "
        f"unicast packets in/out {_num(s.in_pkts)}/{_num(s.out_pkts)}"
    )


async def run_port_counters(
    host: str, iface: str | None, watch: int, community: str, timeout: int
) -> None:
    """Section 10: raw port counters and, with --watch, the deltas and
    rates the alarm engine computes from them.

    Every requested measurement is taken whatever the counters hold:
    the measurements are the point of the command, and a switch with
    nothing in its error columns is exactly the one worth measuring.
    """
    _section(f"10. Port counters: {host}")
    collector = _make_collector(community, timeout)
    names, speeds = await _port_labels(collector, host)
    if not names:
        sys.exit(f"{host} does not respond to SNMP")
    store = CounterStore()

    for measurement in range(max(1, watch)):
        if measurement:
            print(f"\n… waiting {WATCH_INTERVAL} s")
            await asyncio.sleep(WATCH_INTERVAL)
        samples, oper, columns = await counters.collect_samples(
            collector, host
        )
        rates = store.update(host, samples, speeds)
        if not measurement:
            _print_columns(columns)
        ports, noisy = _select_ports(samples, names, iface, oper)
        if not ports:
            print(f"  {iface}: no such port" if iface else "  no ports answered")
            return
        print(f"\nraw counters ({time.strftime('%H:%M:%S')}):")
        for if_index in ports:
            _print_raw(if_index, samples[if_index], names, speeds, oper)
        if not iface:
            print(
                "ports with a non-zero error or discard counter: "
                + (", ".join(names.get(i, str(i)) for i in noisy) or "none")
            )
        if not rates:
            continue  # first measurement is the baseline
        print("rates since the previous measurement:")
        for if_index in ports:
            r = rates.get(if_index)
            if r is None:
                continue
            share = (
                f", {r.error_ratio * 100:.4f}% of frames"
                if r.error_ratio is not None else ""
            )
            print(
                f"  {names.get(if_index, str(if_index)):<16} "
                f"in {_rate(r.in_mbps)} Mbit/s, out {_rate(r.out_mbps)} "
                f"Mbit/s, errors {_rate(r.errors_per_min, 1)}/min "
                f"(in {_rate(r.in_errors_per_min, 1)}, "
                f"out {_rate(r.out_errors_per_min, 1)}"
                f"{share}), discards {_rate(r.discards_per_min, 0)}/min "
                f"(in {_rate(r.in_discards_per_min, 0)}, "
                f"out {_rate(r.out_discards_per_min, 0)})"
            )
        if any(not st.complete for st in columns.values()):
            print(
                "  ^ n/a means the agent returned nothing for that port, "
                "which is not the same as zero — see the column report "
                "above for which columns were short"
            )


async def run_stp_view(community: str, timeout: int, cfg) -> None:
    """Section 14: the raw dot1dStp* values next to the verdict.

    The point is that the verdict can be checked. A switch with STP
    disabled reports priority 0, cost 0 and itself as root; printing
    those numbers together with the reason they are being ignored is
    the difference between a diagnosis and a guess.
    """
    _section("14. Spanning tree (BRIDGE-MIB dot1dStp*)")
    if not cfg.switches:
        sys.exit("no switches in config.yaml")
    collector = _make_collector(community, timeout)
    collected = list(
        await asyncio.gather(*(collector.collect(ip) for ip in cfg.switches))
    )
    reachable = [sw for sw in collected if sw.reachable]
    for sw in collected:
        if not sw.reachable:
            print(f"{sw.ip}: does not respond to SNMP — excluded")
    if not reachable:
        sys.exit("no reachable switches")

    per_switch = {sw.ip: sw.stp for sw in reachable if sw.stp is not None}
    verdict = stp.network_verdict(per_switch)
    if verdict["verdict"] == "not_operating":
        headline = (
            "STP is not running anywhere: not one switch passes the "
            "'the tree actually converged' test"
        )
    elif verdict["verdict"] == "single":
        root = next(iter(verdict["roots"]))
        headline = f"one spanning tree, root {root}"
    else:
        headline = f"the tree is fragmented: {len(verdict['roots'])} roots"
    print(f"verdict: {headline}\n")

    for sw in reachable:
        data = sw.stp
        name = f"{sw.sys_name} ({sw.ip})" if sw.sys_name else sw.ip
        print(f"--- {name} ---")
        if data is None or not data.supported:
            print("  dot1dStp* is not answered at all\n")
            continue
        enabled = sum(1 for p in data.ports.values() if p.enabled)
        active = sum(
            1 for p in data.ports.values() if p.state != stp.STATE_DISABLED
        )
        print(f"  dot1dStpProtocolSpecification  {data.protocol_spec} "
              f"({data.protocol_name})")
        if data.version is not None:
            print(f"  dot1dStpVersion               {data.version} "
                  f"({data.version_name})")
        print(f"  dot1dStpPriority              {data.priority}")
        print(f"  dot1dStpDesignatedRoot        {data.designated_root or '—'}")
        print(f"  dot1dStpRootCost              {data.root_cost}")
        print(f"  dot1dStpRootPort              {data.root_port}")
        print(f"  dot1dStpTopChanges            {data.top_changes}")
        print(f"  dot1dStpTimeSinceTopologyChange {data.time_since_change} "
              f"ticks ({data.time_since_change / 100:.0f} s)")
        print(f"  sysUpTime                     {data.sys_uptime} ticks "
              f"({data.sys_uptime / 100:.0f} s)")
        print(f"  ports with dot1dStpPortEnable = enabled: {enabled} "
              f"of {len(data.ports)}")
        print(f"  ports not in state disabled(1):          {active}")
        if data.operating:
            role = "ROOT BRIDGE" if data.is_root(sw.own_macs) else "in the tree"
            print(f"  verdict: operating — {role}")
        else:
            print(f"  verdict: NOT operating — {data.reason}")
            print("           root, cost and root port above are ignored")
        if data.ports:
            print("  ports:")
            for bridge_port in sorted(data.ports):
                port = data.ports[bridge_port]
                edge = ""
                if port.oper_edge is not None:
                    edge = f"  edge {'yes' if port.oper_edge else 'no'}"
                print(
                    f"    bridge-port {bridge_port:>4} "
                    f"{port.name or '(unmapped)':<16} "
                    f"{port.state_name:<11} "
                    f"{'enabled' if port.enabled else 'disabled':<9} "
                    f"cost {port.path_cost:<7} "
                    f"designated bridge {port.designated_bridge or '—'}{edge}"
                )
        print()

    if verdict["verdict"] == "fragmented":
        print("roots and who follows them:")
        for root, ips in sorted(verdict["roots"].items()):
            print(f"  {root}: {', '.join(ips)}")


WALK_DEFAULT_LIMIT = 500


async def run_walk(
    host: str, oid: str, limit: int, community: str, timeout: int
) -> None:
    """Section 15: a raw walk of any subtree.

    Private MIBs are where Loopback Detection lives (D-Link
    1.3.6.1.4.1.171, HPE 1.3.6.1.4.1.11), and there is no way to write
    a feature against one without first seeing what the agent actually
    returns. Strings are printed both as text and as hex, because half
    of what comes back from these branches is neither.
    """
    _section(f"15. Walk {oid} on {host}")
    collector = _make_collector(community, timeout)
    count = 0
    async for suffix, value in collector._walk(host, oid):
        count += 1
        if count > limit:
            print(f"  … stopped at {limit} rows (--limit raises the ceiling)")
            return
        full = oid + ("." + ".".join(str(part) for part in suffix) if suffix else "")
        kind = type(value).__name__
        text = str(value)
        print(f"  {full}  ({kind}) = {text}")
        # Hex only for something that really is a string of octets. A
        # number has no hex form worth printing, and asking for one used
        # to allocate a buffer as long as the number itself.
        if is_octets(value):
            raw = as_octets(value)
            if raw and (raw != text.encode("utf-8", "replace")
                        or not text.isprintable()):
                print(f"      hex: {raw.hex(' ')}")
    if count == 0:
        print("  the subtree is empty, or the agent does not implement it")
    else:
        print(f"\n{min(count, limit)} row(s)")


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
    parser.add_argument(
        "--hosts", action="store_true",
        help="compare FDB, ARP and the database: how complete the "
             "host inventory is and which subnets are missing from it",
    )
    parser.add_argument(
        "--host", metavar="IP_OR_MAC",
        help="everything known about one device: the database record, "
             "which switch tables and ARP tables hold it, a live ping "
             "and why it is considered offline",
    )
    parser.add_argument(
        "--fdb", metavar="SWITCH_IP",
        help="dump the raw MAC table of a switch: OID, suffix length, "
             "parsed address and whether the row was rejected",
    )
    parser.add_argument(
        "--stp", action="store_true",
        help="poll all switches from config.yaml and print the raw "
             "dot1dStp* values together with the verdict on whether "
             "the spanning tree is operating at all",
    )
    parser.add_argument(
        "--walk", nargs=2, metavar=("SWITCH_IP", "OID"),
        help="walk any OID subtree and print raw OIDs, types and values "
             "(strings as text and as hex) — for exploring private MIBs",
    )
    parser.add_argument(
        "--limit", type=int, default=WALK_DEFAULT_LIMIT, metavar="N",
        help=f"maximum rows for --walk (default {WALK_DEFAULT_LIMIT}), so a "
             f"stray subtree is not downloaded whole",
    )
    parser.add_argument(
        "--config", action="store_true",
        help="print the effective configuration: every setting, its "
             "value and whether it comes from config.yaml or a default",
    )
    parser.add_argument(
        "--port", metavar="SWITCH_IP",
        help="print raw error, discard, octet and packet counters of "
             "the switch's ports",
    )
    parser.add_argument(
        "--iface", help="one port for --port and --fdb (name or ifIndex)"
    )
    parser.add_argument(
        "--watch", type=int, default=1, metavar="N",
        help=f"repeat the --port measurement N times every "
             f"{WATCH_INTERVAL} s and print the rates the alarm engine sees",
    )
    args = parser.parse_args()
    modes = (
        args.topology or args.hosts or args.port or args.config
        or args.host or args.fdb or args.stp or args.walk
    )
    if not modes and not args.ip:
        parser.error(
            "an ip is required unless --topology, --hosts, --host, --fdb, "
            "--stp, --walk, --port or --config is given"
        )
    cfg = load_config()
    community = args.community or cfg.snmp.community
    timeout = args.timeout or cfg.snmp.timeout
    _SNMP["retries"] = cfg.snmp.retries
    _SNMP["retries_on_break"] = cfg.snmp.retries_on_break
    if args.config:
        run_config_audit(cfg)
    elif args.walk:
        asyncio.run(
            run_walk(args.walk[0], args.walk[1], args.limit, community, timeout)
        )
    elif args.stp:
        asyncio.run(run_stp_view(community, timeout, cfg))
    elif args.host:
        asyncio.run(run_host_diag(args.host, community, timeout, cfg))
    elif args.fdb:
        asyncio.run(run_fdb_dump(args.fdb, args.iface, community, timeout, cfg))
    elif args.port:
        asyncio.run(
            run_port_counters(
                args.port, args.iface, args.watch, community, timeout
            )
        )
    elif args.hosts:
        asyncio.run(run_host_inventory(community, timeout, cfg))
    elif args.topology:
        asyncio.run(run_topology_view(community, timeout, cfg))
    else:
        asyncio.run(run_diag(args.ip, community, timeout, cfg.switches))


if __name__ == "__main__":
    main()
