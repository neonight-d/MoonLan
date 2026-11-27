"""The hosts on the map and in the database (v0.8.14).

The distorted MACs and the switches' own, the addresses ARP no longer
confirms, how many devices the database knows behind a port and where
it last placed each, the inventory split into the map's hosts, the
groups of devices that are off and the ones off the map, the text of
new_mac, the fields the map takes from the database, the damaged
copies, reverse DNS. Moved out of server.py word for word
(docs/ARCHITECTURE.md); the state is read as rt.X when they run.
Nothing here imports moonlan.server.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from collections import Counter

from ..derived import _effective_monitored
from ..runtime import rt
from ..snmp_collector import is_random_mac
from ..topology import group_beyond_trunk

log = logging.getLogger("moonlan")


def _apply_suspects(
    hosts: list[dict], suspects: dict[tuple[str, str], list[dict]]
) -> None:
    """Marks the distorted MACs and remembers them for the ports panel."""
    rt.suspect_by_port.clear()
    rt.suspect_by_port.update(suspects)
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
    for sw in rt.switch_data.values():
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
    hours = rt.config.ip_confirm_hours
    if hours <= 0:
        return
    now = time.time()
    cutoff = now - hours * 3600
    released = []
    for mac, row in db_rows.items():
        if not row["ip"] or mac in rt.fdb_macs:
            continue
        if row["ip_confirmed"] >= cutoff:
            continue
        if await asyncio.to_thread(
            rt.db.release_ip, mac, now,
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


def _remembered_locations(
    db_rows: dict[str, dict]
) -> dict[str, tuple[str, str]]:
    """MAC -> the (switch, port) the database last placed it at.

    Consulted before a device visible only through a trunk is guessed
    onto one. What the database remembers is a port a device was
    actually seen on; a trunk is where its frames happened to pass.
    """
    return {
        mac: (row["switch_ip"], row["port"])
        for mac, row in db_rows.items()
        if row["switch_ip"] and row["port"]
    }


def _known_hosts_per_port(
    db_rows: dict[str, dict], switch_ips: set[str]
) -> dict[tuple[str, str], int]:
    """How many devices the database knows behind each port, counting
    the ones inside the grace window — the same set that stays on the
    map — so the unmanaged-switch threshold sees the whole group and
    not only the MACs this particular poll happened to catch."""
    grace = rt.config.host_grace_hours * 3600
    now = time.time()
    counts: Counter = Counter()
    for row in db_rows.values():
        if not row["confirmed"]:
            continue  # not a device yet: it must not push a port over
                      # the unmanaged-switch threshold
        if row.get("corrupt_of") and rt.config.filter_suspect_macs:
            continue  # a damaged copy, never one
        if row["switch_ip"] in switch_ips and row["port"]:
            if grace > 0 and now - row["last_seen"] < grace:
                counts[(row["switch_ip"], row["port"])] += 1
    return dict(counts)


def _mark_recent(
    unlocated: list[dict], db_rows: dict[str, dict], now: float
) -> None:
    """`recent` on every record off the map (v0.8.7): seen by anybody —
    a switch, ARP, a ping — inside host_grace_hours. "Not on map" is
    the recent ones, devices that are there now without a place; the
    rest is "Not seen for a while", an archive of addresses that left.
    Before, the two were one list of 480, and the two devices that
    mattered were lost in it.

    `seen_at` is that moment, and `forget_at` when purge_old_hosts
    deletes the record (None while host_retention_days is 0) — the
    page words both. Called again on every /api/topology: pings move
    `last_ping_ok` between scans.

    ARP is a sign of presence only while the record holds an address
    (v0.8.8). `last_arp` stays when the address goes — to another MAC
    (set_ips) or nowhere, unconfirmed (release_ip) — and kept 140 of
    142 records without an IP "recent" for host_grace_hours after the
    very ARP that showed the device was not there. `seen_at` is still
    the last sighting of any kind, and `seen_by` says which: "fdb",
    "arp" or "ping".

    A ping too, since v0.8.9: it goes to the address, and once the
    address is another device's, the answer is that device's."""
    grace = rt.config.host_grace_hours * 3600
    days = rt.config.host_retention_days
    for host in unlocated:
        row = db_rows.get(host["mac"], {})
        fdb = row.get("last_seen", 0) or 0
        arp = row.get("last_arp", 0) or 0
        ping = row.get("last_ping_ok", 0) or 0
        seen = max(fdb, arp, ping)
        host["seen_at"] = seen
        host["seen_by"] = (
            "" if not seen else "fdb" if seen == fdb
            else "arp" if seen == arp else "ping"
        )
        holds_address = bool(host.get("ip") or row.get("ip"))
        present = max(fdb, max(arp, ping) if holds_address else 0)
        # seen this very scan, on an uplink: recent by definition
        host["recent"] = bool(host.get("uplink_only")) or (
            grace > 0 and now - present < grace
        )
        kept_since = max(
            row.get("last_seen", 0) or 0, row.get("last_arp", 0) or 0
        )
        host["forget_at"] = kept_since + days * 86400 if days > 0 else None


def _assemble_hosts(
    fresh: list[dict],
    db_rows: dict[str, dict],
    pseudo_switches: list[dict],
    switch_ips: set[str],
    unconfirmed: set[str] | None = None,
    merged: dict[str, str] | None = None,
    nodes_on_port: dict[tuple[str, str], dict] | None = None,
    uplink_only: dict[str, list[tuple[str, str]]] | None = None,
    trunk_names: dict[str, list[str]] | None = None,
) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
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

    `uplink_only` are MACs every sighting of which was on an uplink.
    They go off the map whatever the database remembers about them,
    because what it remembers is an earlier guess at the same question
    — that is how twenty devices of the rest of the network came to be
    drawn behind a switch with every port but its uplink dark.
    """
    for h in fresh:
        h["stale"] = False
    now = time.time()
    merged = merged or {}
    grace = rt.config.host_grace_hours * 3600
    uplink_only = uplink_only or {}
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
        # a device drawn as its bridge node keeps that identity while
        # it is quiet, instead of reappearing as a bare MAC beside it
        if mac in merged:
            host["merged_into"] = merged[mac]
        if mac in uplink_only:
            # seen this very scan, and seen nowhere that means anything
            host["stale"] = False
            host["unlocated"] = True
            host["uplink_only"] = True
            host["seen_on"] = [
                {"switch": ip, "port": port}
                for ip, port in uplink_only[mac]
            ]
            host["switch"] = ""
            host["port"] = ""
            host["off_map"] = "uplink_only"
            unlocated.append(host)
            continue
        # a record with a switch and no port has no place on that switch
        # (v0.8.6: the ports a poll has no name for are reset to '')
        located = row["switch_ip"] in switch_ips and bool(row["port"])
        if located and grace > 0 and now - row["last_seen"] < grace:
            via = pseudo_by_port.get((row["switch_ip"], row["port"]))
            if via:
                host["via"] = via
            on_map.append(host)
        else:
            host["unlocated"] = True
            # why it is not on the map: a place it had, until the grace
            # window ran out — or no place at all (ARP only, a port
            # reset, a switch that is not drawn from a reading)
            host["off_map"] = "expired" if located else "no_port"
            unlocated.append(host)
    _mark_recent(unlocated, db_rows, now)
    # devices ARP still knows first, then by how recently anything saw them
    unlocated.sort(
        key=lambda h: (
            not db_rows[h["mac"]]["ip"],
            -max(db_rows[h["mac"]]["last_arp"], db_rows[h["mac"]]["last_seen"]),
        )
    )
    # Devices with no place of their own go first: "we do not know
    # where this is" is a stronger statement than "this has been quiet
    # for a while", and the offline grouping below leaves anything that
    # already has a `via` alone. So one trunk is one node, with the
    # quiet ones counted inside it rather than standing beside it.
    silent = {
        mac for mac, row in db_rows.items()
        if row.get("ip") and not row.get("ping_up")
    }
    trunk_groups = group_beyond_trunk(
        on_map, trunk_names or {}, rt.config.trunk_group_threshold,
        nodes_on_port, silent,
    )
    return (
        on_map, _group_offline(on_map, db_rows, nodes_on_port), unlocated,
        trunk_groups,
    )


def _group_offline(
    on_map: list[dict],
    db_rows: dict[str, dict],
    nodes_on_port: dict[tuple[str, str], dict] | None = None,
) -> list[dict]:
    """Collects the stale hosts of one port under a single group node.

    A port whose devices are all offline gets a "temporary location"
    node — the devices are shown where they were last seen, and one
    node says so instead of a dozen grey dots. Ports that already have
    a pseudo-switch keep it: their hosts hang off that node.

    `nodes_on_port` is what build_topology put on each port — a named
    bridge, or an anonymous pseudo-switch. The group hangs off that
    node rather than off the switch. Behind sw1 1/27 there were three
    neighbours of the switch at once: RouterOS-C with 22 live
    devices, a "switch without SNMP", and an "Offline · 19" of
    10.1.5.x addresses standing beside them — one segment drawn as
    three places, with the quiet half apparently somewhere else
    entirely.
    """
    threshold = rt.config.offline_group_threshold
    if threshold <= 0:
        return []
    nodes_on_port = nodes_on_port or {}
    by_port: dict[tuple[str, str], list[dict]] = {}
    for host in on_map:
        if host.get("merged_into"):
            continue  # it is drawn as its bridge, not as a dot
        if host["stale"] and not host.get("via"):
            by_port.setdefault((host["switch"], host["port"]), []).append(host)
    groups: list[dict] = []
    for (sw_ip, port), members in sorted(by_port.items()):
        if len(members) < threshold:
            continue  # one or two lone dots read fine on their own
        group_id = f"offline:{sw_ip}:{port}"
        for host in members:
            host["via"] = group_id
        parent = nodes_on_port.get((sw_ip, port))
        groups.append({
            "id": group_id,
            "switch": sw_ip,
            "port": port,
            "count": len(members),
            # the live devices of this port hang off this node too, and
            # the quiet ones belong in the same place
            "via": parent["id"] if parent else "",
            "via_name": parent["name"] if parent else "",
            "last_seen_max": max(
                db_rows[m["mac"]]["last_seen"] for m in members
            ),
        })
    return groups


def _new_device_text(mac: str, row: dict) -> str:
    """The text of new_mac — in the alarms, the journal, Telegram and the
    mail: who made the device and where it came in, so the administrator
    knows who joined the network without opening the map. "new device by
    GIGA-BYTE TECHNOLOGY CO.,LTD. on access-sw-1 (10.0.0.21) Gi0/9"."""
    maker = rt.vendors.lookup(mac)
    who = {
        "found": f"new device by {maker.name}",
        "unregistered": "new device, its maker not in the IEEE register,",
        "local": "new device with a random or hand-set address",
    }.get(maker.status, "new device")
    switch = row.get("switch_ip", "")
    if not switch:
        return who
    name = next((sw["name"] for sw in rt.state.as_dict()["switches"]
                 if sw["ip"] == switch), "")
    where = f"{name} ({switch})" if name and name != switch else switch
    return f"{who} on {where} {row.get('port', '')}".rstrip()


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
        # who made it: the name, or why there is none (vendor_status)
        h.update(rt.vendors.fields(h["mac"]))


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
        for mac, ip in await asyncio.to_thread(rt.db.hosts_without_name)
        if now - rt._dns_attempts.get(mac, 0) >= DNS_RETRY_SECONDS
    ]
    if not candidates:
        return
    for mac, _ in candidates:
        rt._dns_attempts[mac] = now
    names = await asyncio.gather(*(_reverse_dns(ip) for _, ip in candidates))
    resolved = 0
    for (mac, _), name in zip(candidates, names):
        if name:
            await asyncio.to_thread(rt.db.set_name, mac, name)
            resolved += 1
    if resolved:
        log.info("Reverse DNS: got %d names out of %d", resolved, len(candidates))


def _marked_copies(db_rows: dict[str, dict]) -> set[str]:
    """Records marked as damaged copies (v0.8.5)."""
    return {mac for mac, row in db_rows.items() if row.get("corrupt_of")}
