"""SNMP diagnostics for a switch: how MoonLan sees the device.

Usage:  python -m moonlan.diag <ip> [--community public] [--timeout 2]
        python -m moonlan.diag --topology
        python -m moonlan.diag --hosts
        python -m moonlan.diag --port <ip> [--iface Gi0/1] [--watch 3]
        python -m moonlan.diag --config
        python -m moonlan.diag --host <ip|mac>
        python -m moonlan.diag --fdb <ip> [--iface 1/3]
        python -m moonlan.diag --stp
        python -m moonlan.diag --loop
        python -m moonlan.diag --walk <ip> <oid> [--limit 500]
        python -m moonlan.diag --topology --anonymize

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
--loop prints the loop-detection state of every switch: the matched
profile, the raw scalars and the raw per-port values, plus the
sysObjectID of every switch no profile covers — which is exactly what
adding a new model needs. --walk dumps any OID subtree, which is how a
private MIB (D-Link 1.3.6.1.4.1.171, HPE 1.3.6.1.4.1.11) gets explored
before it becomes a feature.

--anonymize may be added to any of them. Every report here is a map of
somebody's network — addresses, MAC tables, host names, the port
labels a previous administrator typed in — and that is exactly what
makes it useful in a bug report and exactly what nobody wants
published. The flag rewrites all of it through a table that stays
consistent for the run, so the report still reads as a report. See
moonlan/anonymize.py for what it covers and what it cannot.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
import time
import urllib.request
from collections import Counter

from . import counters, loopdetect, pinger, probes, stp
from .anonymize import Anonymizer, AnonymizingWriter
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
    drop_impossible_links,
    suspect_uplink_ports,
    infer_tree,
    lldp_link_candidates,
    mark_stp_blocking,
    merge_lldp_links,
    normalized_fdb,
    resolve_cycles,
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
    OID_SYS_OBJECT_ID,
    SnmpCollector,
    SwitchData,
    _fmt_mac,
    diagnose_silence,
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
         "retries_on_break": SnmpConfig.retries_on_break,
         # Per-switch settings from config.yaml, so a device with its
         # own timeout is diagnosed on that timeout. Emptied when
         # --community or --timeout is given: an explicit value on the
         # command line is an instruction, not a suggestion, and it
         # applies to every address in the run.
         "per_host": {}}

# Set by --anonymize. Every collector built here then registers the
# names it learns, and stdout rewrites them on the way out.
_ANON: Anonymizer | None = None


class _RegisteringCollector(SnmpCollector):
    """A collector that tells the anonymizer what it just learned.

    One hook rather than a call in each of the eight report
    functions: the names worth hiding are exactly the ones the tool
    reads off the devices, and they all come through here. A report
    added later is anonymised without its author having to remember.
    """

    async def _get(self, host: str, oid: str):
        value = await super()._get(host, oid)
        if oid == OID_SYS_NAME and value is not None and _ANON is not None:
            _ANON.register_switch(str(value))
        return value

    async def collect(self, host: str):
        data = await super().collect(host)
        if _ANON is not None:
            _ANON.register_switch(data.sys_name)
            _ANON.register_names(
                n.sys_name for n in data.lldp_neighbors if n.sys_name
            )
        return data


def _make_collector(community: str, timeout: int) -> SnmpCollector:
    factory = _RegisteringCollector if _ANON is not None else SnmpCollector
    return factory(
        community=community,
        timeout=timeout,
        retries=_SNMP["retries"],
        retries_on_break=_SNMP["retries_on_break"],
        per_host=_SNMP["per_host"],
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
        _verdict, why = await diagnose_silence(collector, ip, OID_SYS_NAME)
        sys.exit(f"{ip} does not respond to SNMP: {why}")

    # 1. General information
    _section("1. General information")
    print(f"sysName:    {sys_name}")
    sys_descr = await collector._get(ip, OID_SYS_DESCR)
    print(f"sysDescr:   {sys_descr if sys_descr is not None else '—'}")
    # the vendor's model identifier — it is what the loop-detection
    # profiles are keyed by, so --loop needs it and so does anyone
    # adding a new model
    sys_object_id = await collector._get(ip, OID_SYS_OBJECT_ID)
    print(f"sysObjectID: {sys_object_id if sys_object_id is not None else '—'}")
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


async def _collect_all(collector, cfg) -> tuple[list, list[tuple[str, int]]]:
    """Polls every configured switch under the same budget as the service.

    v0.6.12 gave each host a time budget so one slow agent could not
    hold the whole scan. The diagnostics kept a bare `asyncio.gather`,
    so `diag --topology` on this network waited eight minutes for a
    RouterOS box while the service it is meant to explain had long
    since moved on. A tool that behaves differently from the thing it
    diagnoses is diagnosing something else.
    """

    async def one(ip: str):
        try:
            return await asyncio.wait_for(
                collector.collect(ip), cfg.host_budget(ip)
            )
        except asyncio.TimeoutError:
            return None

    results = await asyncio.gather(*(one(ip) for ip in cfg.switches))
    collected = []
    over_budget: list[tuple[str, int]] = []
    for ip, data in zip(cfg.switches, results):
        if data is None:
            over_budget.append((ip, cfg.host_budget(ip)))
            continue
        collected.append(data)
    return collected, over_budget


async def run_topology_view(community: str, timeout: int, cfg) -> None:
    """Section 8: poll every configured switch and print the inferred tree."""
    _section("8. Topology view")
    if not cfg.switches:
        sys.exit("no switches in config.yaml")
    collector = _make_collector(community, timeout)
    print(f"polling {len(cfg.switches)} switches from config.yaml…")
    collected, over_budget = await _collect_all(collector, cfg)
    for ip, budget in over_budget:
        print(
            f"{ip}: did not finish inside its {budget} s poll budget — "
            f"excluded from this view"
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
    lldp_pairs = lldp_link_candidates(switches)
    links, uplinks, info = infer_tree(
        switches, switches_on_port, sees, lldp_pairs
    )
    mismatches = merge_lldp_links(links, lldp_pairs, by_ip)
    # The same two passes the service runs, in the same order, or this
    # view would explain a map nobody is looking at
    info["dropped_links"] = drop_impossible_links(
        links, lldp_pairs, uplinks, info.get("root")
    )
    mark_stp_blocking(switches, links)
    info["dropped_links"] += resolve_cycles(
        links, lldp_pairs, uplinks, info.get("root")
    )

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
    # Every line the map would draw, with what is known about it: a
    # link whose ports are down, or whose source is a guess, or that
    # only exists because nothing better was available, reads very
    # differently from one both devices confirmed.
    oper = {
        sw.ip: {
            (p.name or str(p.if_index)): p.oper_up for p in sw.ports.values()
        }
        for sw in switches
    }

    def port_state(ip: str, name: str) -> str:
        if name in ("?", ""):
            return "?"
        up = oper.get(ip, {}).get(name)
        return "?" if up is None else ("up" if up else "DOWN")

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
        speed = (
            f"{link['speed_mbps'] / 1000:g} Gbit/s"
            if link["speed_mbps"] >= 1000
            else f"{link['speed_mbps']} Mbit/s" if link["speed_mbps"]
            else "speed unknown"
        )
        flags = "".join(
            mark for mark, on in (
                ("  [STP BLOCKING]", link.get("stp_blocking")),
                ("  [branch order unknown]", link.get("order_unknown")),
                ("  [in an unresolved ring]", link.get("cycle_unresolved")),
            ) if on
        )
        print(
            f"  {label(link['a'])} [{link['a_port']} "
            f"{port_state(link['a'], link['a_port'])}] — "
            f"{label(link['b'])} [{link['b_port']} "
            f"{port_state(link['b'], link['b_port'])}]"
            f"  {speed}  source: {link.get('source', 'fdb')}"
            f"{trunk}{lacp}{flags}"
        )

    # What the inference took back, and what it could not settle. Both
    # change the picture, and neither is visible in the picture itself.
    print("\nlinks withdrawn by the inference:")
    dropped = info.get("dropped_links") or []
    if not dropped:
        print("  none")
    for entry in dropped:
        why = (
            f"LLDP puts {entry['b']} behind {entry['behind']}"
            if entry.get("reason") == "behind"
            else f"closes a ring with no blocked port: {entry.get('cycle', '')}"
        )
        print(
            f"  {label(entry['a'])} [{entry['a_port']}] — "
            f"{label(entry['b'])} [{entry['b_port']}] "
            f"({entry.get('source', 'fdb')}): {why}"
        )
    unresolved = [link for link in links if link.get("cycle_unresolved")]
    print("rings left standing (no blocked port, nothing to choose by):")
    if not unresolved:
        print("  none")
    for link in unresolved:
        print(
            f"  {label(link['a'])} [{link['a_port']}] — "
            f"{label(link['b'])} [{link['b_port']}] "
            f"({link.get('source', 'fdb')})"
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
    hosts = [dict(row) for row in rows]
    if _ANON is not None:
        # reverse-DNS names out of the inventory: the other half of
        # what a report gives away, and the half no pattern can find
        _ANON.register_names(row.get("name") or "" for row in hosts)
    return hosts


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
    collected, over_budget = await _collect_all(collector, cfg)
    for ip, budget in over_budget:
        print(
            f"{ip}: did not finish inside its {budget} s poll budget — its "
            f"MAC table is missing from every comparison below, so the "
            f"devices behind it will read as \"known but not in any FDB\". "
            f"That is this poll giving up, not the devices going away."
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

    starved = cfg.starved_counters()
    if starved:
        print(
            f"\npoll budget at or above twice counters_interval_seconds "
            f"({cfg.counters_interval_seconds} s):"
        )
        for ip, budget in starved:
            print(f"  {ip}: host_budget_seconds = {budget}")
        print(
            "  ^ while a scan holds one of these, its counters cycle is\n"
            "    skipped, so its rates age visibly between scans. Not an\n"
            "    error — the panel shows them with their age — but lower\n"
            "    the budget for those devices if you can."
        )

    if report.problems:
        print("\nentries in switches: that could not be used:")
        for problem in report.problems:
            print(f"  {problem}")

    _print_node_menu(cfg)

    # The settings each switch is actually polled with. The global
    # section is only half the answer once a switch may carry keys of
    # its own, and "which timeout is this device on" is the first
    # question asked of a device that polls slowly.
    print("\nSNMP settings per switch (* = set for this switch, "
          "the rest inherited from snmp:):")
    if not cfg.switches:
        print("  no switches configured")
        return
    columns = ("timeout", "retries", "retries_on_break",
               "host_budget_seconds", "community")
    header = f"  {'switch':<18}" + "".join(
        f"{name:>21}" for name in columns
    )
    print(header)
    for ip in cfg.switches:
        settings = cfg.host_snmp(ip)
        cells = []
        for name in columns:
            value = getattr(settings, name)
            if name in SECRET_KEYS:
                value = _mask(name, value)
            mark = "*" if name in settings.explicit else " "
            cells.append(f"{str(value) + mark:>21}")
        print(f"  {ip:<18}" + "".join(cells))

    _print_poll_times(cfg)


def _print_node_menu(cfg) -> None:
    """Part of `--config`: what the right-click menu can do here.

    Ping and traceroute run on this machine, so whether they exist is a
    fact about this machine, not about the config — and a menu item
    greyed out as "no traceroute on the server" should be explainable
    from here.
    """
    menu_cfg = cfg.context_menu
    found = probes.find_tools()
    trace = found["traceroute"]
    print("\nnode menu (context_menu):")
    print(
        "  ping:        "
        + (found["ping"] or "NOT FOUND — Ping is disabled in the menu")
    )
    print(
        "  traceroute:  "
        + (f"{trace[0]} at {trace[1]}" if trace else
           "NOT FOUND (neither traceroute nor tracepath) — Traceroute "
           "is disabled in the menu")
    )
    print(
        f"  at most {menu_cfg.max_targets} node(s) per action, "
        f"{menu_cfg.max_running} action(s) running at once"
    )
    print(f"  switch web interface: {menu_cfg.web_scheme}://"
          + "".join(f", {ip} {scheme}://"
                    for ip, scheme in sorted(cfg.switch_web_scheme.items())))
    if cfg.report and cfg.report.menu_problems:
        print("  items left out of the menu:")
        for problem in cfg.report.menu_problems:
            print(f"    {problem}")


def _ask_service(cfg, path: str) -> dict | None:
    """One GET against the running service, or None with a reason.

    Several reports need state that lives in the service process and
    nowhere else — which OIDs are on pause, how long each poll took.
    Guessing at it would be worse than saying it is not available.
    """
    host = cfg.listen_host
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    url = f"http://{host}:{cfg.listen_port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        print(f"could not ask the service at {url}: {exc}")
        return None


def _print_poll_times(cfg) -> None:
    """Section of `--config`: what each poll actually costs.

    `host_budget_seconds` is guesswork until somebody measures the
    poll. This is the measurement, per switch, from the service that
    took it.
    """
    print("\nlast complete poll of each switch (from the running service):")
    data = _ask_service(cfg, "/api/polling")
    if data is None:
        print(
            "  the durations live in the service process. Start MoonLan, "
            "or point listen.host / listen.port at the instance you mean."
        )
        return
    rows = data.get("switches") or []
    if not rows:
        print("  no switches configured")
        return
    print(
        f"  {'switch':<18} {'budget':>8} {'took':>9} {'when':>18}  state"
    )
    for row in rows:
        when = (
            time.strftime("%Y-%m-%d %H:%M", time.localtime(row["polled_at"]))
            if row["polled_at"] else "never"
        )
        took = f"{row['poll_seconds']:.1f} s" if row["poll_seconds"] else "—"
        if row["over_budget_scans"]:
            state = f"{row['over_budget_scans']} scan(s) over budget"
        elif not row["reachable"]:
            state = "no answer"
        else:
            state = "ok"
        print(
            f"  {row['ip']:<18} {str(row['budget_seconds']) + ' s':>8} "
            f"{took:>9} {when:>18}  {state}"
        )
    print(
        "  ^ set host_budget_seconds from the 'took' column, not by eye. "
        "A switch\n    listed as over budget has not been read in full "
        "since the time shown."
    )


def run_layout_view(cfg) -> None:
    """Section 13: the saved map layout, and how far it has drifted.

    The layout lives in the database, but which nodes exist right now
    lives in the running service — so this asks it, the same way
    `--skipped` does.
    """
    _section("13. Saved map layout")
    data = _ask_service(cfg, "/api/layout")
    if data is None:
        print(
            "The saved layout is in the database, but which nodes are on "
            "the map right now is in the running service. Start MoonLan, "
            "or point listen.host / listen.port at the instance you mean."
        )
        return
    nodes = data.get("nodes") or {}
    missing = data.get("missing") or []
    pinned = [node for node, pos in nodes.items() if pos.get("pinned")]
    saved_at = data.get("saved_at") or 0
    print(
        f"saved positions: {len(nodes)}"
        + (
            f", last written {time.strftime('%Y-%m-%d %H:%M', time.localtime(saved_at))}"
            if saved_at else " (nothing saved yet)"
        )
    )
    print(f"placed by hand (pinned): {len(pinned)}")
    for node_id in sorted(pinned)[:20]:
        pos = nodes[node_id]
        print(f"  {node_id:<40} {pos['x']:>9.1f} {pos['y']:>9.1f}")
    if len(pinned) > 20:
        print(f"  … and {len(pinned) - 20} more")

    print(f"\non the map but not in the saved layout: {len(missing)}")
    for node_id in missing[:20]:
        print(f"  {node_id}")
    if len(missing) > 20:
        print(f"  … and {len(missing) - 20} more")
    if missing:
        print(
            "  ^ they appeared after the layout was saved. Place them and\n"
            "    save again, or save to record the map as it is now."
        )

    # The other direction: a saved position whose node is gone. Kept on
    # purpose — a device switched off for the night comes back to its
    # place — and cleaned up by age at the first scan after a restart.
    orphans = data.get("orphans") or []
    print(f"\nsaved positions with no node on the map: {len(orphans)}")
    for node_id in orphans[:20]:
        pos = nodes[node_id]
        when = time.strftime(
            "%Y-%m-%d %H:%M", time.localtime(pos.get("updated_at", 0))
        )
        print(f"  {node_id:<40} placed {when}")
    if len(orphans) > 20:
        print(f"  … and {len(orphans) - 20} more")
    if orphans:
        print(
            f"  ^ kept on purpose: a device switched off comes back to its\n"
            f"    place. Forgotten after layout_keep_days "
            f"({cfg.layout_keep_days:.0f}), at the first scan after a restart."
        )


def run_skipped_view(cfg) -> None:
    """Section 12: OIDs the service has stopped asking for.

    The pause is state of the running process — a CLI run builds its
    own collector and has seen nothing — so this asks the service over
    its own API rather than inventing an answer.
    """
    _section("12. OIDs currently not asked for")
    data = _ask_service(cfg, "/api/skipped-oids")
    if data is None:
        print(
            "This list lives in the running process. Start MoonLan, or "
            "point listen.host / listen.port at the instance you mean."
        )
        return
    print(
        f"rule: pause an OID after {data['strikes']} walk(s) with no rows "
        f"and a timeout, for {data['cooldown_scans']} scan(s)"
    )
    if not data.get("polled"):
        print("\nthe service has not polled anything yet")
        return
    paused = data.get("paused") or []
    if not paused:
        print("\nnothing is on pause: every OID is being asked for")
        return
    print(f"\n{'switch':<18} {'OID':<34} {'strikes':>8} {'scans left':>11}")
    for entry in paused:
        print(
            f"{entry['host']:<18} {entry['oid']:<34} "
            f"{entry['strikes']:>8} {entry['cycles_left']:>11}"
        )
    print(
        "\nThese are not missing because the devices deny having them. "
        "They are missing because MoonLan stopped asking, and it will "
        "ask again when the count above runs out."
    )


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
            collector, host, set(names)
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
    stp.judge_network(per_switch)
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
        # Read by a person, this number invites the wrong conclusion:
        # RouterOS answers 3 (ieee8021d) with RSTP running, and so do
        # others. MoonLan takes the version from dot1dStpVersion below
        # and never from this object.
        print("      (always 3 on some agents, RSTP or not — the version "
              "comes from dot1dStpVersion)")
        if data.version is not None:
            print(f"  dot1dStpVersion               {data.version} "
                  f"({data.version_name})")
        print(f"  dot1dStpPriority              {data.priority}")
        root_note = (
            "   (the agent puts the priority in the low byte; corrected)"
            if data.root_nonstandard else ""
        )
        print(f"  dot1dStpDesignatedRoot        "
              f"{data.designated_root or '—'}{root_note}")
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
            # which of the three tests decided it: a verdict whose
            # basis is invisible is a verdict nobody can check
            print(f"           basis: {data.reason}")
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


async def run_loop_view(community: str, timeout: int, cfg) -> None:
    """Section 16: loop detection as the vendors' MIBs report it.

    Printed raw on purpose. Only the "no loop" value of these agents
    has ever been observed, so the rule downstream is "normal is known,
    everything else is a loop" — and a rule like that is only as
    trustworthy as the values it is applied to. Every number here can
    be compared against the switch's own web interface.

    The last section is the useful one for a network with new
    hardware in it: the switches no profile covers, with the
    sysObjectID to key a new profile by.
    """
    _section("16. Loop detection (vendor private MIBs)")
    if not cfg.switches:
        sys.exit("no switches in config.yaml")
    extra, problems = loopdetect.parse_profiles(cfg.loop_detection.profiles)
    for problem in problems:
        print(f"config.yaml loop_detection.profiles: {problem}")
    profiles = loopdetect.merge_profiles(loopdetect.BUILTIN_PROFILES, extra)
    print("profiles: " + ", ".join(p.name for p in profiles))
    if not cfg.loop_detection.enabled:
        print("loop_detection.enabled is false — the service does not poll this")
    print()

    collector = _make_collector(community, timeout)
    collected = list(
        await asyncio.gather(*(collector.collect(ip) for ip in cfg.switches))
    )
    unprofiled: list[SwitchData] = []
    looping: list[tuple[SwitchData, object]] = []
    for sw in collected:
        name = f"{sw.sys_name} ({sw.ip})" if sw.sys_name else sw.ip
        print(f"--- {name} ---")
        if not sw.reachable:
            print("  does not respond to SNMP — excluded\n")
            continue
        print(f"  sysObjectID                 {sw.sys_object_id or '—'}")
        data = await loopdetect.collect_loop_detection(
            collector, sw.ip, sw.ports, profiles,
            sys_object_id=sw.sys_object_id,
        )
        if not data.supported:
            print("  no profile answers: this model does not report loop")
            print("  detection over SNMP, so nothing is claimed about it —")
            print("  neither that there is a loop nor that there is not\n")
            unprofiled.append(sw)
            continue
        print(f"  profile                     {data.profile} "
              f"(matched by {data.matched_by})")
        print(f"  branch root                 {data.root}")
        # three-valued, and it stays that way: a scalar that did not
        # come back is not a switch with its loop protection off
        global_state = (
            "enabled" if data.enabled is True
            else "DISABLED" if data.enabled is False
            else "UNKNOWN — the global scalar did not come back"
        )
        print(f"  global state                {global_state}")
        print(f"  mode                        {_num(data.mode)}")
        print(f"  detection interval          {_num(data.interval)} s")
        print(f"  recovery time               {_num(data.recover_time)} s")
        print(f"  table rows                  {data.rows}")
        if data.unmapped:
            print(f"  rows with no interface      "
                  f"{', '.join(str(i) for i in data.unmapped)} — not used")
        if data.index_mismatch:
            print("  WARNING: the vendor table and the interface table "
                  "disagree on the port count")
        if data.truncated:
            print(f"  WARNING: the status column stopped answering "
                  f"({data.error})")
        if data.ports:
            print("  ports:")
            for if_index in sorted(data.ports):
                state = data.ports[if_index]
                port = sw.ports.get(if_index)
                verdict = (
                    "LOOP" if state.looped
                    else "unknown" if state.looped is None else "ok"
                )
                if state.lbd_enabled is False:
                    verdict = "not watched"
                # the per-port flag is three-valued too: a port the
                # column had no row for is "?", not "off"
                lbd = (
                    "on " if state.lbd_enabled is True
                    else "off" if state.lbd_enabled is False else "?  "
                )
                print(
                    f"    {(port.name if port else str(if_index)):<12} "
                    f"index {state.port:>4}  "
                    f"LBD {lbd}  "
                    f"status {state.status_raw or '—':<8} {verdict}"
                )
        if data.looped_ports():
            looping.append((sw, data))
        print()

    if looping:
        print("VERDICT: a loop is reported right now")
        for sw, data in looping:
            for state in data.looped_ports():
                port = sw.ports.get(state.if_index)
                print(
                    f"  {sw.sys_name or sw.ip} "
                    f"{port.name if port else state.if_index}: raw status "
                    f"{state.status_raw}"
                )
    else:
        print("VERDICT: no switch with a profile reports a loop")
    if unprofiled:
        print(
            "\nSwitches with no loop-detection profile — each line is "
            "what a\nnew profile in config.yaml needs to be keyed by:"
        )
        for sw in unprofiled:
            print(
                f"  {sw.ip:<15} {sw.sys_name or '—':<28} "
                f"sysObjectID {sw.sys_object_id or 'not answered'}"
            )
        print(
            "\nWalk the branch under that enterprise number to find the\n"
            "objects, e.g.  python -m moonlan.diag --walk "
            f"{unprofiled[0].ip} 1.3.6.1.4.1.<enterprise>"
        )


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
        # "The subtree is empty, or the agent does not implement it"
        # was the old answer, and on a switch whose community string
        # had been changed back to `public` both halves of it were
        # wrong — and pointed away from the cause. Ask before deciding.
        _verdict, why = await diagnose_silence(collector, host, oid)
        print(f"  nothing came back: {why}")
    else:
        print(f"\n{min(count, limit)} row(s)")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m moonlan.diag",
        description="SNMP diagnostics for a switch (read-only, does not touch the DB)",
    )
    parser.add_argument("ip", nargs="?", help="switch IP address")
    parser.add_argument(
        "--community",
        help="SNMP community for every address in this run (by default "
             "each switch uses the one config.yaml gives it)",
    )
    parser.add_argument(
        "--timeout", type=int,
        help="SNMP timeout in seconds for every address in this run (by "
             "default each switch uses the one config.yaml gives it)",
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
        "--loop", action="store_true",
        help="poll all switches from config.yaml and print loop "
             "detection as their private MIBs report it, plus the "
             "sysObjectID of every switch no profile covers",
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
        "--anonymize", action="store_true",
        help="rewrite addresses, MACs and names in the output through a "
             "table that is stable for this run (RFC 5737 / RFC 7042 "
             "documentation ranges) — use it on anything you attach to "
             "an issue",
    )
    parser.add_argument(
        "--layout", action="store_true",
        help="ask the running service about the saved map layout: how "
             "many nodes it holds, how many were placed by hand, and "
             "how far it has drifted from the map as it is now",
    )
    parser.add_argument(
        "--skipped", action="store_true",
        help="ask the running service which OIDs it has stopped "
             "polling on which hosts, and for how many more scans",
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
        or args.host or args.fdb or args.stp or args.walk or args.loop
        or args.skipped or args.layout
    )
    if not modes and not args.ip:
        parser.error(
            "an ip is required unless --topology, --hosts, --host, --fdb, "
            "--stp, --loop, --walk, --port, --skipped, --layout or "
            "--config is given"
        )
    cfg = load_config()
    community = args.community or cfg.snmp.community
    timeout = args.timeout or cfg.snmp.timeout
    _SNMP["retries"] = cfg.snmp.retries
    _SNMP["retries_on_break"] = cfg.snmp.retries_on_break
    if not args.community and not args.timeout:
        _SNMP["per_host"] = cfg.switch_snmp
    if args.anonymize:
        global _ANON
        _ANON = Anonymizer()
        # Wrapping the stream, not each report: the next section
        # somebody adds is then clean without being told to be.
        sys.stdout = AnonymizingWriter(sys.stdout, _ANON)
    if args.config:
        run_config_audit(cfg)
    elif args.skipped:
        run_skipped_view(cfg)
    elif args.layout:
        run_layout_view(cfg)
    elif args.walk:
        asyncio.run(
            run_walk(args.walk[0], args.walk[1], args.limit, community, timeout)
        )
    elif args.stp:
        asyncio.run(run_stp_view(community, timeout, cfg))
    elif args.loop:
        asyncio.run(run_loop_view(community, timeout, cfg))
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
