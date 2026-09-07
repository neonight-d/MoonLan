"""MoonLan web service: REST API and the static web UI."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__, corruption, counters, demo, pinger
from .alarms import AlarmEngine
from .config import Config, load_config
from .db import Database
from .notify import Notifier
from .snmp_collector import (
    SnmpCollector,
    SwitchData,
    is_random_mac,
    is_valid_mac,
)
from .topology import FdbStability, TopologyState, build_topology, port_name

log = logging.getLogger("moonlan")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

state = TopologyState()
config: Config = load_config()
# In demo mode the DB lives in memory so the real one is not polluted
db = Database(":memory:" if config.demo else config.db_path)

# Ping state of switches (they are not in the hosts table): ip -> {ping_up, last_ping_ok}
switch_ping: dict[str, dict] = {}

# FDB merged with previous polls: protects links from MAC table aging
fdb_stability = FdbStability()

# Latest raw poll data per switch: the ports API, the counters loop and
# the link load labels all need port lists, speeds and LAG composition
switch_data: dict[str, SwitchData] = {}

counter_store = counters.CounterStore()
notifier = Notifier(config, demo=config.demo)
alarm_engine = AlarmEngine(
    db, notifier, config.thresholds, config.notifications
)
demo_counters = demo.DemoCounters() if config.demo else None

# The first scan is the initial inventory: every MAC is "new" there,
# alerting on all of them would be pure noise
first_scan_done = False

# MACs present in the FDB of the latest scan. A host missing from it is
# "stale": still drawn at its last known port during the grace window,
# but never alarmed on — its presence is no longer confirmed.
fdb_macs: set[str] = set()

# (switch ip, port) of the pseudo-switches drawn last time: a group
# that already exists must not vanish while any device is left on the
# port, or the nodes would jump around between scans
prev_pseudo_ports: set[tuple[str, str]] = set()

# (switch ip, port) -> the distorted MACs of the latest scan, for the
# ports panel and the diagnostics
suspect_by_port: dict[tuple[str, str], list[dict]] = {}

# One SnmpEngine per process: a new engine per cycle leaks sockets and
# MIB state (OSError 24, MibNotFoundError, growing RSS). Recreate only
# if the SNMP config ever changes at runtime — it currently cannot.
_collector: SnmpCollector | None = None


def get_collector() -> SnmpCollector:
    global _collector
    if _collector is None:
        _collector = SnmpCollector(
            community=config.snmp.community,
            timeout=config.snmp.timeout,
            retries=config.snmp.retries,
        )
    return _collector


async def run_scan() -> None:
    """One cycle of polling all switches and rebuilding the topology."""
    global first_scan_done, fdb_macs, prev_pseudo_ports
    if state.scanning:
        return
    state.scanning = True
    try:
        arp: dict[str, str] = {}
        if config.demo:
            collected = demo.demo_network()
        else:
            collector = get_collector()
            collected = list(
                await asyncio.gather(*(collector.collect(ip) for ip in config.switches))
            )
            if config.routers:
                arp = await collect_arp(collector)
            # The MAC of the switch's management IP is also its MAC:
            # neighbors see the switch under it in their FDB tables
            ip_to_mac = {ip: mac for mac, ip in arp.items()}
            for sw in collected:
                mac = ip_to_mac.get(sw.ip)
                if mac:
                    sw.own_macs.add(mac)
        for sw in collected:
            switch_data[sw.ip] = sw
        # A MAC has to be seen more than once before it counts as a
        # device (unless ARP vouches for it); the verdict is needed
        # before the topology so that unconfirmed addresses stay out of
        # the per-port device counts as well as off the map
        db_rows = await asyncio.to_thread(db.hosts_by_mac)
        # An empty database is the initial inventory — there is no map
        # yet for a phantom to pollute, and waiting would only show the
        # operator an empty screen on the first poll
        confirm_scans = config.new_host_confirm_scans if db_rows else 1
        unconfirmed = await asyncio.to_thread(
            db.projected_unconfirmed,
            {mac for sw in collected if sw.reachable for mac in sw.fdb},
            confirm_scans,
        )
        # What the database already knows about each port feeds the
        # unmanaged-switch threshold, so groups survive FDB aging
        switches, links, hosts, pseudo_switches, vlan_names = build_topology(
            collected,
            config.unmanaged_threshold,
            fdb_stability=None if config.demo else fdb_stability,
            known_hosts_per_port=_known_hosts_per_port(
                db_rows, {sw.ip for sw in collected if sw.reachable}
            ),
            sticky_pseudo_ports=prev_pseudo_ports,
            unconfirmed_macs=unconfirmed,
        )
        prev_pseudo_ports = {
            (p["switch"], p["port"]) for p in pseudo_switches
        }
        # Addresses a bit or two away from a real one on the same port:
        # a failing cable, not new devices. They are held back from
        # confirmation for as long as they look like copies — otherwise
        # a permanently damaged port would simply confirm its phantoms.
        suspects = corruption.find_suspects(
            hosts, unconfirmed, config.thresholds.corruption_hamming_bits
        )
        _apply_suspects(hosts, suspects)
        suspect_macs = {
            s["mac"] for found in suspects.values() for s in found
        }
        new_macs = await asyncio.to_thread(
            db.upsert_hosts, hosts, confirm_scans,
            suspect_macs if config.filter_suspect_macs else set(),
        )
        if config.demo:
            await asyncio.to_thread(demo.enrich_db, db, hosts)
        else:
            if arp:
                # ARP knows devices no switch port ever showed (behind a
                # router or an unpolled switch): keep them as inventory
                # with an empty switch_ip. Switches and routers
                # themselves are map nodes, not hosts.
                infra_ips = set(config.switches) | set(config.routers)
                own_macs = _switch_macs()
                arp_hosts = {
                    mac: ip for mac, ip in arp.items()
                    if mac not in own_macs and ip not in infra_ips
                }
                created = await asyncio.to_thread(
                    db.set_ips, arp_hosts, True
                )
                if created:
                    log.info(
                        "ARP: %d devices known but not seen on any switch "
                        "port", created,
                    )
            await resolve_names()
        # ARP vouches for an address, so a newcomer it knows joins the
        # map in the same poll it first appeared in
        new_macs += await asyncio.to_thread(db.confirm_hosts_with_ip)
        if new_macs:
            log.info("New MACs confirmed: %d", len(new_macs))
        unconfirmed = await asyncio.to_thread(db.unconfirmed_macs)
        if unconfirmed:
            log.info(
                "MACs awaiting confirmation (%d poll(s) each): %d",
                confirm_scans, len(unconfirmed),
            )
        if not config.filter_suspect_macs:
            unconfirmed -= suspect_macs  # draw the damage instead
        hosts = [h for h in hosts if h["mac"] not in unconfirmed]
        fdb_macs = {h["mac"] for h in hosts}
        db_rows = await asyncio.to_thread(db.hosts_by_mac)
        await _release_unconfirmed_ips(db_rows)
        hosts, offline_groups, unlocated = _assemble_hosts(
            hosts, db_rows, pseudo_switches, {sw["ip"] for sw in switches},
            unconfirmed,
        )
        _merge_db_fields(hosts, db_rows)
        _merge_db_fields(unlocated, db_rows)
        state.update(
            switches, links, hosts, pseudo_switches, vlan_names, unlocated,
            offline_groups,
        )
        if config.demo:
            await run_ping()  # set the switches' ping state right away

        await alarm_engine.on_scan(
            {sw.ip: sw.reachable for sw in collected},
            {sw.ip: sw.sys_name or sw.ip for sw in collected},
        )
        await alarm_engine.on_corruption(
            {f"{sw_ip}:{port}": found
             for (sw_ip, port), found in suspects.items()}
        )
        if new_macs and first_scan_done:
            rows = await asyncio.to_thread(db.hosts_by_mac)
            await alarm_engine.on_new_macs(
                new_macs,
                {
                    mac: f"{rows[mac]['switch_ip']} / {rows[mac]['port']}"
                    for mac in new_macs if mac in rows
                },
            )
        first_scan_done = True
        log.info(
            "Scan finished: %d switches, %d links, %d hosts",
            len(switches), len(links), len(hosts),
        )
    finally:
        state.scanning = False


def _apply_suspects(
    hosts: list[dict], suspects: dict[tuple[str, str], list[dict]]
) -> None:
    """Marks the distorted MACs and remembers them for the ports panel."""
    suspect_by_port.clear()
    suspect_by_port.update(suspects)
    flat = {
        s["mac"]: s for found in suspects.values() for s in found
    }
    for host in hosts:
        found = flat.get(host["mac"])
        if found:
            host["suspect_corrupt"] = True
            host["suspect_of"] = found["sample"]
    if flat:
        log.info(
            "Suspected frame corruption: %d distorted MACs on %d port(s): %s",
            len(flat), len(suspects),
            ", ".join(f"{ip} {port}" for ip, port in sorted(suspects)),
        )


def _switch_macs() -> set[str]:
    """Every MAC belonging to a polled switch — those are nodes of the
    map, never hosts, even when a router's ARP table lists them."""
    macs: set[str] = set()
    for sw in switch_data.values():
        macs |= sw.own_macs
        if sw.bridge_mac:
            macs.add(sw.bridge_mac)
    return macs


async def _release_unconfirmed_ips(db_rows: dict[str, dict]) -> None:
    """Takes the IP off hosts that no longer own it.

    Pings go by IP while staleness is judged by MAC, so a record whose
    MAC left every switch table keeps "answering" with whatever device
    holds that address now — the host looks alive hours after it left.
    Once ARP has not confirmed the pair for ip_confirm_hours, the
    address is released and the record stops being pinged. db_rows is
    updated in place so the rest of this scan sees the change.
    """
    hours = config.ip_confirm_hours
    if hours <= 0:
        return
    now = time.time()
    cutoff = now - hours * 3600
    released = []
    for mac, row in db_rows.items():
        if not row["ip"] or mac in fdb_macs:
            continue
        if row["ip_confirmed"] >= cutoff:
            continue
        if await asyncio.to_thread(
            db.release_ip, mac, now,
            f"not confirmed by ARP for {hours:g} h",
        ):
            released.append(mac)
            row["ip"] = ""
            row["ip_confirmed"] = 0
            row["ping_up"] = 0
    if released:
        log.info(
            "Released %d IP addresses of hosts missing from every MAC "
            "table and unconfirmed by ARP", len(released),
        )


def _known_hosts_per_port(
    db_rows: dict[str, dict], switch_ips: set[str]
) -> dict[tuple[str, str], int]:
    """How many devices the database knows behind each port, counting
    the ones inside the grace window — the same set that stays on the
    map — so the unmanaged-switch threshold sees the whole group and
    not only the MACs this particular poll happened to catch."""
    grace = config.host_grace_hours * 3600
    now = time.time()
    counts: Counter = Counter()
    for row in db_rows.values():
        if not row["confirmed"]:
            continue  # not a device yet: it must not push a port over
                      # the unmanaged-switch threshold
        if row["switch_ip"] in switch_ips and row["port"]:
            if grace > 0 and now - row["last_seen"] < grace:
                counts[(row["switch_ip"], row["port"])] += 1
    return dict(counts)


def _assemble_hosts(
    fresh: list[dict],
    db_rows: dict[str, dict],
    pseudo_switches: list[dict],
    switch_ips: set[str],
    unconfirmed: set[str] | None = None,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Splits the known inventory into map hosts, offline groups and
    off-map devices.

    On the map: this scan's FDB hosts, plus hosts whose MAC left the
    FDB less than host_grace_hours ago, drawn at their last known port
    (FDB entries age out in minutes — without the grace window a quiet
    device blinks in and out on every scan) and marked stale. Stale
    hosts sharing a port are collected under one offline group instead
    of surrounding the switch with a cloud of grey dots.

    Off the map: everything else the DB knows — devices ARP sees but
    no switch port ever showed, and hosts whose grace window ran out.

    Unconfirmed MACs are in neither list: they are stored, and nothing
    more, until enough polls agree that they exist.
    """
    for h in fresh:
        h["stale"] = False
    now = time.time()
    grace = config.host_grace_hours * 3600
    known = {h["mac"] for h in fresh} | _switch_macs() | (unconfirmed or set())
    pseudo_by_port = {(p["switch"], p["port"]): p["id"] for p in pseudo_switches}
    on_map = list(fresh)
    unlocated: list[dict] = []
    for mac, row in db_rows.items():
        if mac in known:
            continue
        host = {
            "mac": mac,
            "switch": row["switch_ip"],
            "port": row["port"],
            "vlan": row["vlan"],
            "name": "",
            "stale": True,
        }
        located = row["switch_ip"] in switch_ips
        if located and grace > 0 and now - row["last_seen"] < grace:
            via = pseudo_by_port.get((row["switch_ip"], row["port"]))
            if via:
                host["via"] = via
            on_map.append(host)
        else:
            host["unlocated"] = True
            unlocated.append(host)
    # devices ARP still knows first, then by how recently anything saw them
    unlocated.sort(
        key=lambda h: (
            not db_rows[h["mac"]]["ip"],
            -max(db_rows[h["mac"]]["last_arp"], db_rows[h["mac"]]["last_seen"]),
        )
    )
    return on_map, _group_offline(on_map, db_rows), unlocated


def _group_offline(on_map: list[dict], db_rows: dict[str, dict]) -> list[dict]:
    """Collects the stale hosts of one port under a single group node.

    A port whose devices are all offline gets a "temporary location"
    node — the devices are shown where they were last seen, and one
    node says so instead of a dozen grey dots. Ports that already have
    a pseudo-switch keep it: their hosts hang off that node.
    """
    threshold = config.offline_group_threshold
    if threshold <= 0:
        return []
    by_port: dict[tuple[str, str], list[dict]] = {}
    for host in on_map:
        if host["stale"] and not host.get("via"):
            by_port.setdefault((host["switch"], host["port"]), []).append(host)
    groups: list[dict] = []
    for (sw_ip, port), members in sorted(by_port.items()):
        if len(members) < threshold:
            continue  # one or two lone dots read fine on their own
        group_id = f"offline:{sw_ip}:{port}"
        for host in members:
            host["via"] = group_id
        groups.append({
            "id": group_id,
            "switch": sw_ip,
            "port": port,
            "count": len(members),
            "last_seen_max": max(
                db_rows[m["mac"]]["last_seen"] for m in members
            ),
        })
    return groups


def _effective_monitored(row: dict) -> bool:
    """monitored_by_default=true restores the old alert-on-everything
    behavior regardless of the per-host flag."""
    return config.monitored_by_default or bool(row.get("monitored", 0))


def _merge_db_fields(hosts: list[dict], db_hosts: dict[str, dict]) -> None:
    """Enriches topology hosts with DB fields (IP, name, ping state)."""
    for h in hosts:
        row = db_hosts.get(h["mac"], {})
        h["ip"] = row.get("ip", "")
        h["name"] = row.get("name", "")
        h["ping_up"] = bool(row.get("ping_up", 0))
        h["last_ping_ok"] = row.get("last_ping_ok", 0)
        h["first_seen"] = row.get("first_seen", 0)
        h["last_seen"] = row.get("last_seen", 0)
        h["last_arp"] = row.get("last_arp", 0)
        h["ip_confirmed"] = row.get("ip_confirmed", 0)
        h["monitored"] = _effective_monitored(row)
        # phones and laptops randomize their MAC per network, which is
        # why one device can leave a trail of one-off entries
        h["random_mac"] = is_random_mac(h["mac"])


async def collect_arp(collector: SnmpCollector) -> dict[str, str]:
    """The merged ARP table of all routers: MAC -> IP."""
    tables = await asyncio.gather(
        *(collector.collect_arp(ip) for ip in config.routers)
    )
    merged: dict[str, str] = {}
    for table in tables:  # on conflict the last entry wins
        merged.update(table)
    return merged


# mac -> unix time of the last reverse DNS attempt
_dns_attempts: dict[str, float] = {}
DNS_RETRY_SECONDS = 3600
DNS_TIMEOUT = 1.0


async def _reverse_dns(ip: str) -> str:
    try:
        name, _, _ = await asyncio.wait_for(
            asyncio.to_thread(socket.gethostbyaddr, ip), timeout=DNS_TIMEOUT
        )
        return name
    except (OSError, asyncio.TimeoutError):
        return ""


async def resolve_names() -> None:
    """Reverse DNS for hosts with an IP but no name, at most once an hour per host."""
    now = time.time()
    candidates = [
        (mac, ip)
        for mac, ip in await asyncio.to_thread(db.hosts_without_name)
        if now - _dns_attempts.get(mac, 0) >= DNS_RETRY_SECONDS
    ]
    if not candidates:
        return
    for mac, _ in candidates:
        _dns_attempts[mac] = now
    names = await asyncio.gather(*(_reverse_dns(ip) for _, ip in candidates))
    resolved = 0
    for (mac, _), name in zip(candidates, names):
        if name:
            await asyncio.to_thread(db.set_name, mac, name)
            resolved += 1
    if resolved:
        log.info("Reverse DNS: got %d names out of %d", resolved, len(candidates))


async def periodic_scan() -> None:
    while True:
        try:
            await run_scan()
        except Exception:
            log.exception("Network scan failed")
        interval = config.scan_interval_minutes
        await asyncio.sleep(interval * 60 if interval > 0 else 3600)


async def run_ping() -> None:
    """One ping cycle: every host with an IP and every switch."""
    now = time.time()
    if config.demo:
        # No real pings: the demo scenario drives the ping state
        # (one host stays silent, another recovers after a while)
        results_by_mac = await asyncio.to_thread(demo.ping_results, db)
        for sw in state.as_dict()["switches"]:
            switch_ping[sw["ip"]] = {"ping_up": True, "last_ping_ok": now}
    else:
        targets = await asyncio.to_thread(db.hosts_with_ip)
        # One ping per unique address, the result applied to every host
        # holding it: IPs are unique in the DB since v0.5.3, but a
        # single probe per address is the right shape regardless
        unique_ips = list(dict.fromkeys(ip for _, ip in targets))
        ips = unique_ips + list(config.switches)
        if not ips:
            return
        results = await pinger.ping_many(ips)
        results_by_mac = {mac: results.get(ip, False) for mac, ip in targets}
        await asyncio.to_thread(db.update_ping, results_by_mac, now)
        for ip in config.switches:
            prev = switch_ping.get(ip, {})
            up = results.get(ip, False)
            switch_ping[ip] = {
                "ping_up": up,
                "last_ping_ok": now if up else prev.get("last_ping_ok", 0),
            }
    if results_by_mac:
        meta = await asyncio.to_thread(db.hosts_by_mac)
        for mac, row in meta.items():
            row["monitored"] = _effective_monitored(row)
            # A located host missing from the latest FDB is not
            # confirmed present, so it raises no alarms. Hosts that
            # were never located (ARP-only) keep alerting normally.
            row["stale"] = bool(row["switch_ip"]) and mac not in fdb_macs
        await alarm_engine.on_ping(results_by_mac, meta)


async def periodic_ping() -> None:
    while True:
        try:
            await run_ping()
        except Exception:
            log.exception("Ping monitoring failed")
        await asyncio.sleep(max(config.ping_interval_seconds, 1))


def _lag_groups(sw: SwitchData) -> dict[int, list[int]]:
    """Aggregate ifIndex -> member ifIndexes: both IEEE8023-LAG-MIB
    aggregates and D-Link trunks inferred from bridge-port gaps
    (keyed by the negative synthetic ifIndex)."""
    groups: dict[int, list[int]] = {}
    for member, aggregate in sw.lag_members.items():
        groups.setdefault(aggregate, []).append(member)
    for bridge_port, members in sw.lag_groups.items():
        groups.setdefault(-bridge_port, []).extend(members)
    return groups


def _lag_label(sw: SwitchData, members: list[int]) -> str:
    """Stable alarm label of a LAG, e.g. "lag[Slot0/1+Slot0/2]".

    Built from the member port names: a member going down changes only
    its oper status, never the composition, while the synthetic
    bridge-port number D-Link exposes DOES change on member flaps —
    keying alarms by it left them hanging active forever (v0.5.1 bug).
    """
    names = [port_name(sw, m) for m in sorted(members)]
    return "lag[" + "+".join(names) + "]"


def _port_metrics(
    sw: SwitchData, rates: dict[int, counters.PortRates]
) -> list[dict]:
    """Alarm inputs for one counters cycle.

    Physical ports are evaluated individually; LAG members get
    speed_mbps=0 so the utilization rule fires on the aggregate entry
    (sum of members against the total speed) instead, per the spec.
    """
    groups = _lag_groups(sw)
    in_lag = {m for members in groups.values() for m in members}
    metrics: list[dict] = []
    for if_index, r in rates.items():
        port = sw.ports.get(if_index)
        if port is None or not port.is_physical:
            continue
        metrics.append({
            "port": port.name or str(if_index),
            "speed_mbps": 0 if if_index in in_lag else port.speed_mbps,
            "in_mbps": r.in_mbps,
            "out_mbps": r.out_mbps,
            "in_errors_per_min": r.in_errors_per_min,
            "out_errors_per_min": r.out_errors_per_min,
            "in_discards_per_min": r.in_discards_per_min,
            "out_discards_per_min": r.out_discards_per_min,
            "error_ratio": r.error_ratio,
        })
    for aggregate, members in groups.items():
        member_rates = [rates[m] for m in members if m in rates]
        member_ports = [sw.ports[m] for m in members if m in sw.ports]
        # utilization is judged against the ACTIVE capacity: a degraded
        # LAG saturates earlier
        metrics.append({
            "port": _lag_label(sw, members),
            "speed_mbps": sum(p.speed_mbps for p in member_ports if p.oper_up),
            "in_mbps": sum(r.in_mbps for r in member_rates),
            "out_mbps": sum(r.out_mbps for r in member_rates),
            # member errors are already alarmed individually
            "in_errors_per_min": 0.0,
            "out_errors_per_min": 0.0,
            "in_discards_per_min": 0.0,
            "out_discards_per_min": 0.0,
            "error_ratio": None,
            "lag_total": len(member_ports),
            "lag_up": sum(1 for p in member_ports if p.oper_up),
        })
    return metrics


async def run_counters() -> None:
    """One light counters poll of every switch (no topology rebuild)."""
    if not switch_data:
        return  # port lists are unknown until the first scan
    oper_by_ip: dict[str, dict[int, bool]] = {}
    if config.demo:
        # the demo flips port states directly in switch_data
        samples_by_ip = demo_counters.sample(list(switch_data.values()))
    else:
        collector = get_collector()
        ips = list(config.switches)
        collected = await asyncio.gather(
            *(counters.collect_samples(collector, ip) for ip in ips)
        )
        samples_by_ip = {ip: samples for ip, (samples, _) in zip(ips, collected)}
        oper_by_ip = {ip: oper for ip, (_, oper) in zip(ips, collected)}
    for ip, samples in samples_by_ip.items():
        if not samples:
            continue
        sw = switch_data.get(ip)
        oper = oper_by_ip.get(ip) or {}
        if sw is not None:
            # fresh port states at the counters cadence: LAG degradation
            # and the ports panel must not wait for the next full scan
            for if_index, up in oper.items():
                port = sw.ports.get(if_index)
                if port is not None:
                    port.oper_up = up
        speeds = (
            {p.if_index: p.speed_mbps for p in sw.ports.values()}
            if sw is not None else None
        )
        rates = counter_store.update(ip, samples, speeds)
        if sw is not None:
            await alarm_engine.on_counters(ip, _port_metrics(sw, rates))
    await alarm_engine.janitor(_observed_subjects())


def _observed_subjects() -> set[tuple[str, str]]:
    """(type, subject) pairs that exist in the current switch data —
    the janitor auto-clears active alarms that fell out of this set."""
    observed: set[tuple[str, str]] = set()
    for ip, sw in switch_data.items():
        labels = [
            p.name or str(p.if_index)
            for p in sw.ports.values() if p.is_physical
        ]
        labels += [_lag_label(sw, members) for members in _lag_groups(sw).values()]
        for label in labels:
            subject = f"{ip}:{label}"
            for alarm_type in (
                "port_errors", "port_discards", "port_util", "port_hosts_down",
                "port_frame_corruption",
            ):
                observed.add((alarm_type, subject))
            if label.startswith("lag["):
                observed.add(("lag_degraded", subject))
    return observed


async def periodic_counters() -> None:
    while True:
        try:
            await run_counters()
        except Exception:
            log.exception("Counter polling failed")
        await asyncio.sleep(max(config.counters_interval_seconds, 5))


def resource_usage() -> tuple[int, int]:
    """(open file descriptors, RSS in kB) — leak watch, Linux only."""
    fds = len(os.listdir("/proc/self/fd"))
    rss = 0
    with open("/proc/self/status", encoding="ascii", errors="replace") as f:
        for line in f:
            if line.startswith("VmRSS:"):
                rss = int(line.split()[1])
                break
    return fds, rss


RESOURCE_LOG_SECONDS = 600


async def periodic_resource_log() -> None:
    while True:
        await asyncio.sleep(RESOURCE_LOG_SECONDS)
        try:
            fds, rss = resource_usage()
            # INFO on purpose: the leak watch must be visible in
            # journalctl at the default log level
            log.info("open fds: %d, rss: %d kB", fds, rss)
        except OSError:
            pass  # not a Linux /proc — skip silently


def _rates_max_age() -> float:
    return max(config.counters_interval_seconds, 5) * 3


def _refresh_link_lag(link: dict) -> None:
    """Recomputes LAG member states, active count and speed from the
    latest port data, so a member going down between topology rebuilds
    is reflected on the edge without waiting for the next scan."""
    lag = link.get("lag")
    if not lag or not lag.get("members"):
        return
    lag = dict(lag)  # the caller passes a copy of the link, not of lag
    active_counts: list[int] = []
    speeds: dict[str, int] = {}
    for side in ("a", "b"):
        sw = switch_data.get(link[side])
        names = lag.get(f"{side}_members")
        if sw is None or not names:
            continue
        by_name = {p.name: p for p in sw.ports.values() if p.name}
        states = [bool(by_name.get(n) and by_name[n].oper_up) for n in names]
        lag[f"{side}_states"] = states
        active_counts.append(sum(states))
        speeds[side] = sum(
            by_name[n].speed_mbps
            for n, up in zip(names, states) if up and n in by_name
        )
    if not active_counts:
        return
    lag["active"] = min(active_counts)
    link["lag"] = lag
    if len(speeds) == 2:
        link["speed_mbps"] = min(speeds.values())
    elif speeds:
        link["speed_mbps"] = next(iter(speeds.values()))


def _link_load(link: dict) -> dict | None:
    """Current traffic through a link, summed over LAG members.

    Prefers the A side (the parent in the tree); falls back to the B
    side with in/out flipped. in_mbps/out_mbps are from A's viewpoint:
    out = toward B (downstream).
    """
    lag = link.get("lag") or {}
    for side, flip in (("a", False), ("b", True)):
        sw = switch_data.get(link[side])
        if sw is None:
            continue
        rates = counter_store.current(link[side], max_age=_rates_max_age())
        if not rates:
            continue
        names = lag.get(f"{side}_members") or [link[f"{side}_port"]]
        by_name = {p.name: p.if_index for p in sw.ports.values() if p.name}
        found = [
            rates[by_name[n]]
            for n in names
            if n in by_name and by_name[n] in rates
        ]
        if not found:
            continue
        in_mbps = sum(r.in_mbps for r in found)
        out_mbps = sum(r.out_mbps for r in found)
        if flip:  # B's in is A's out and vice versa
            in_mbps, out_mbps = out_mbps, in_mbps
        return {"in_mbps": round(in_mbps, 1), "out_mbps": round(out_mbps, 1)}
    return None


def _log_config() -> None:
    """One line on how much of the configuration is actually yours —
    a config.yaml left over from an older version silently overrides
    settings whose defaults have changed since."""
    report = config.report
    if report is None:
        return
    summary = (
        f"Config: {len(report.overrides)} from {report.path}, "
        f"{len(report.defaults)} defaults"
    )
    if report.unknown:
        log.warning(
            "%s; unknown keys ignored: %s — check them with "
            "python -m moonlan.diag --config",
            summary, ", ".join(report.unknown),
        )
    else:
        log.info("%s", summary)


async def purge_invalid_macs() -> None:
    """Startup cleanup: drop records whose MAC cannot be real.

    Before the FDB rows were validated, malformed table entries were
    stored as devices; those records are still in the database and on
    the map.
    """
    rows = await asyncio.to_thread(db.hosts_by_mac)
    bad = [mac for mac in rows if not is_valid_mac(mac)]
    if not bad:
        return
    removed = await asyncio.to_thread(db.delete_hosts, bad)
    log.info(
        "Removed %d host records with impossible MAC addresses "
        "(multicast, reserved or malformed)", removed,
    )
    await asyncio.to_thread(
        db.add_event, time.time(), "hosts_purged", "",
        f"{removed} records with impossible MAC addresses",
    )


async def purge_old_hosts() -> None:
    """Startup cleanup: drop hosts nothing has seen for retention days."""
    days = config.host_retention_days
    if days <= 0:
        return
    now = time.time()
    removed = await asyncio.to_thread(db.purge_old_hosts, now - days * 86400)
    if removed:
        log.info(
            "Removed %d hosts not seen for %g days", removed, days
        )
        await asyncio.to_thread(
            db.add_event, now, "hosts_purged", "",
            f"{removed} hosts not seen for {days:g} days",
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    _log_config()
    if config.demo:
        log.info("MoonLan started in DEMO mode (virtual network)")
    elif not config.switches:
        log.warning(
            "No switches are configured in config.yaml. Add addresses to "
            "the switches section or start with MOONLAN_DEMO=1."
        )
    await purge_invalid_macs()
    await purge_old_hosts()
    await alarm_engine.load()
    await alarm_engine.clear_missing_hosts(
        set(await asyncio.to_thread(db.hosts_by_mac))
    )
    tasks = [
        asyncio.create_task(periodic_scan()),
        asyncio.create_task(periodic_ping()),
        asyncio.create_task(periodic_counters()),
        asyncio.create_task(periodic_resource_log()),
    ]
    yield
    for task in tasks:
        task.cancel()


app = FastAPI(title="MoonLan", version=__version__, lifespan=lifespan)


@app.get("/api/topology")
async def api_topology() -> JSONResponse:
    topo = state.as_dict()
    # Fresh DB data: ping is updated more often than SNMP polls happen
    db_hosts = await asyncio.to_thread(db.hosts_by_mac)
    hosts = [dict(h) for h in topo["hosts"]]
    _merge_db_fields(hosts, db_hosts)
    topo["hosts"] = hosts
    unlocated = [dict(h) for h in topo.get("unlocated", [])]
    _merge_db_fields(unlocated, db_hosts)
    topo["unlocated"] = unlocated
    topo["switches"] = [
        {
            **sw,
            "ping_up": switch_ping.get(sw["ip"], {}).get("ping_up", False),
            "last_ping_ok": switch_ping.get(sw["ip"], {}).get("last_ping_ok", 0),
        }
        for sw in topo["switches"]
    ]
    # Live trunk load on the edges; recomputed on every request so the
    # periodic UI refresh picks it up without a topology rebuild
    links = []
    for link in topo["links"]:
        link = dict(link)
        _refresh_link_lag(link)
        load = _link_load(link)
        if load:
            link["load"] = load
        links.append(link)
    topo["links"] = links
    return JSONResponse(topo)


@app.get("/api/switch/{ip}/ports")
async def api_switch_ports(ip: str) -> dict:
    """Port table of one switch with current rates."""
    sw = switch_data.get(ip)
    if sw is None:
        return {"switch": ip, "name": ip, "ports": []}
    rates = counter_store.current(ip, max_age=_rates_max_age())
    db_hosts = await asyncio.to_thread(db.hosts_by_mac)
    host_counts: Counter = Counter()
    monitored_counts: Counter = Counter()
    for h in state.as_dict()["hosts"]:
        if h["switch"] != ip:
            continue
        host_counts[h["port"]] += 1
        if _effective_monitored(db_hosts.get(h["mac"], {})):
            monitored_counts[h["port"]] += 1
    member_of: dict[int, str] = {}
    for aggregate, members in _lag_groups(sw).items():
        for m in members:
            member_of[m] = port_name(sw, aggregate)
    ports = []
    for p in sw.ports.values():
        r = rates.get(p.if_index)
        name = p.name or str(p.if_index)
        ports.append({
            "if_index": p.if_index,
            "name": name,
            "oper_up": p.oper_up,
            "is_physical": p.is_physical,
            "speed_mbps": p.speed_mbps,
            "pvid": sw.port_pvid.get(p.if_index, 0),
            "lag": member_of.get(p.if_index, ""),
            "in_mbps": round(r.in_mbps, 1) if r else None,
            "out_mbps": round(r.out_mbps, 1) if r else None,
            "errors_per_min": round(r.errors_per_min, 1) if r else None,
            "discards_per_min": round(r.discards_per_min, 1) if r else None,
            "hosts": host_counts.get(name, 0),
            "monitored_hosts": monitored_counts.get(name, 0),
            # MACs that look like damaged copies of a real one here
            "suspect_macs": suspect_by_port.get((ip, name), []),
        })
    # active ports first, then by port number
    ports.sort(key=lambda p: (not p["oper_up"], abs(p["if_index"])))
    return {
        "switch": ip,
        "name": sw.sys_name or ip,
        "ports": ports,
        # so the panel can colour the values it shows
        "thresholds": {
            "errors_per_minute": config.thresholds.errors_per_minute,
            "discards_per_minute": config.thresholds.discards_per_minute,
        },
    }


def _alarm_meta(
    row: dict, hosts: dict[str, dict], sw_names: dict[str, str]
) -> dict:
    """Subject broken into display parts so the UI parses no strings:
    the device label, and the switch and port it belongs to."""
    subject = row["subject"]
    meta = {"display": subject, "switch_ip": "", "switch_name": "", "port": ""}
    if row["type"] in ("host_down", "new_mac"):
        host = hosts.get(subject, {})
        meta["display"] = host.get("name") or host.get("ip") or subject
        meta["switch_ip"] = host.get("switch_ip", "")
        meta["port"] = host.get("port", "")
    elif row["type"] == "switch_down":
        meta["switch_ip"] = subject
    else:
        ip, sep, port = subject.partition(":")
        if sep:
            meta["switch_ip"] = ip
            meta["port"] = (
                "LAG " + port[4:-1]
                if port.startswith("lag[") and port.endswith("]")
                else port
            )
    if meta["switch_ip"]:
        meta["switch_name"] = sw_names.get(meta["switch_ip"], meta["switch_ip"])
        if row["type"] not in ("host_down", "new_mac"):
            meta["display"] = meta["switch_name"]
    return meta


@app.get("/api/alarms")
async def api_alarms(
    active: int = Query(default=1, ge=0, le=1),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict:
    rows = await asyncio.to_thread(db.alarms, bool(active), limit)
    db_hosts = await asyncio.to_thread(db.hosts_by_mac)
    sw_names = {sw["ip"]: sw["name"] for sw in state.as_dict()["switches"]}
    flapping = alarm_engine.flapping_keys()
    stats = alarm_engine.raise_stats()
    for row in rows:
        key = (row["type"], row["subject"])
        row.update(_alarm_meta(row, db_hosts, sw_names))
        row["flapping"] = key in flapping
        row["raise_count"], row["last_raise"] = stats.get(key, (1, row["ts_raised"]))
    return {
        "alarms": rows,
        "flap_window_hours": config.notifications.flap_window_seconds / 3600,
    }


@app.post("/api/alarms/{alarm_id}/clear")
async def api_clear_alarm(alarm_id: int):
    row = await alarm_engine.manual_clear(alarm_id)
    if row is None:
        return JSONResponse({"error": "no such active alarm"}, status_code=404)
    return {"id": alarm_id, "cleared": True}


@app.get("/api/journal")
async def api_journal(limit: int = Query(default=100, ge=1, le=1000)) -> dict:
    return {"events": await asyncio.to_thread(db.journal, limit)}


class HostPatch(BaseModel):
    monitored: bool


@app.patch("/api/host/{mac}")
async def api_patch_host(mac: str, body: HostPatch):
    mac = mac.lower()
    ok = await asyncio.to_thread(db.set_monitored, mac, body.monitored)
    if not ok:
        return JSONResponse({"error": "unknown host"}, status_code=404)
    return {"mac": mac, "monitored": body.monitored}


@app.post("/api/scan")
async def api_scan() -> dict:
    asyncio.create_task(run_scan())
    return {"status": "started"}


@app.get("/api/search")
async def api_search(q: str = Query(default="")) -> dict:
    return {"query": q, "results": state.search(q)}


@app.get("/api/status")
async def api_status() -> dict:
    try:
        open_fds, rss_kb = resource_usage()
    except OSError:
        open_fds, rss_kb = 0, 0
    return {
        "version": __version__,
        "demo": config.demo,
        "switch_count": len(state.switches),
        "host_count": len(state.hosts),
        "unlocated_count": len(state.unlocated),
        "last_scan": state.last_scan,
        "scanning": state.scanning,
        "uptime_hint": time.time(),
        "open_fds": open_fds,
        "rss_kb": rss_kb,
    }


# The static web UI comes last so it does not shadow /api/*
app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
