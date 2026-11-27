"""The map, step by step (v0.8.14).

_build_map was 499 lines in server.py: one pass from what a scan read
to the map published and the alarms told, every stage reading what the
ones before it found. Here it is cut into its steps, in the order
poller._build_map calls them, and what one step hands the next is a
field of MapPass. Each step is a run of the old body word for word,
the comments with it; the only change is x -> p.x for a variable that
goes from one step to another (docs/ARCHITECTURE.md).
tests/test_build_map_effects.py holds the calls they make to the
database, the alarms and the state, and their order, to what the whole
function did. The orchestrator stays in the poller: in the demo it
pings right after the map is published, and nothing here imports the
poller or moonlan.server.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field

from .. import __version__, corruption, demo
from ..about import STARTED_AT
from ..derived import _copies_window, _subject_inbound_errors, _uplink_ports
from ..housekeeping import hide_confirmed_copies
from ..routers import (
    Placement,
    RouterIdentity,
    own_macs as router_own_macs,
    place as place_routers,
)
from ..runtime import rt
from ..snmp_collector import (
    READING_FORMAT,
    STATE_FRESH,
    STATE_NEVER,
    SwitchData,
)
from ..topology import build_topology, suspect_uplink_ports
from .bridges import (
    _infrastructure,
    _is_known_bridge,
    _known_bridge_ids,
    _lldp_rows,
    _merge_bridge_identity,
    _nodes_on_port,
    _suppressed_bridges,
)
from .hosts import (
    _apply_suspects,
    _assemble_hosts,
    _known_hosts_per_port,
    _marked_copies,
    _merge_db_fields,
    _new_device_text,
    _release_unconfirmed_ips,
    _remembered_locations,
    _switch_macs,
    resolve_names,
)
from .router_nodes import (
    _mark_router_nodes,
    _merge_router_macs,
    _release_router_macs,
    _remembered_router_macs,
    _router_addresses,
    _router_nodes_take_over,
)
from .switches import (
    _dropped_link_details,
    _judge_stp,
    _retire_port_names,
    _stp_report,
    _stub_switches,
)
from .upkeep import _purge_layout_once, _reconcile_notes

log = logging.getLogger("moonlan")


@dataclass(eq=False)
class MapPass:
    """One pass from a scan to the map: what it got, and what its steps
    hand each other."""

    # what the pass got
    collected: list[SwitchData]
    arp: dict[str, str]
    observe: bool
    identities: list[RouterIdentity] | None = None
    # what the steps find, in the order they find it
    over_budget: list[str] = field(default_factory=list)
    readings: list[tuple[str, float, str]] = field(default_factory=list)
    own: dict[str, RouterIdentity] = field(default_factory=dict)
    db_rows: dict[str, dict] = field(default_factory=dict)
    confirm_scans: int | None = None
    with_ip: set[str] = field(default_factory=set)
    grace: float | None = None
    copy_candidates: set[str] = field(default_factory=set)
    voting_out: set[str] = field(default_factory=set)
    switches: list[dict] = field(default_factory=list)
    links: list[dict] = field(default_factory=list)
    hosts: list[dict] = field(default_factory=list)
    pseudo_switches: list[dict] = field(default_factory=list)
    vlan_names: dict[int, str] = field(default_factory=dict)
    bridges: list[dict] = field(default_factory=list)
    topo_info: dict = field(default_factory=dict)
    suspects: dict[tuple[str, str], list[dict]] = field(default_factory=dict)
    suspect_macs: set[str] = field(default_factory=set)
    uplink_only: dict = field(default_factory=dict)
    new_macs: list[str] = field(default_factory=list)
    unconfirmed: set[str] = field(default_factory=set)
    placement: Placement | None = None
    offline_groups: list[dict] = field(default_factory=list)
    unlocated: list[dict] = field(default_factory=list)
    trunk_groups: list[dict] = field(default_factory=list)
    now: float | None = None
    bridge_rows: dict[str, dict] = field(default_factory=dict)
    router_ips: dict[str, str] = field(default_factory=dict)
    stp_report: dict = field(default_factory=dict)
    answered: list[SwitchData] = field(default_factory=list)


def keep_readings(p: MapPass) -> None:
    """What this scan read becomes rt.switch_data, and the readings worth
    saving are put aside."""
    p.over_budget = [sw.ip for sw in p.collected if sw.over_budget]
    for sw in p.collected:
        previous = rt.switch_data.get(sw.ip)
        if previous is not None and sw.loop_detection is None:
            # Loop detection is refreshed by the counters cycle,
            # not by the scan. A fresh SwitchData carries none, and
            # dropping the last one made every switch report "the
            # model does not say" for up to a minute after every
            # scan — a claim about hardware, made because of our
            # own bookkeeping.
            sw.loop_detection = previous.loop_detection
        rt.switch_data[sw.ip] = sw
    # The last full reading of each switch read in full, for the next
    # start (v0.8.7). Taken here, before the spanning-tree test and the
    # map write their conclusions into the objects: a reading is what
    # the switch said, and the restored one is judged again.
    p.readings = [
        (sw.ip, sw.polled_at, json.dumps(sw.to_dict(), separators=(",", ":")))
        for sw in p.collected if p.observe and sw.map_state == STATE_FRESH
    ]


def judge_spanning_tree(p: MapPass) -> None:
    """The spanning tree, judged across the switches."""
    # The cross-switch spanning-tree test comes BEFORE the map is
    # built, because the map reads its results. A root recognised
    # only because its neighbours follow its address (v0.6.11) has
    # `operating` and `confirmed_root` set by judge_network — and
    # judge_network used to run after build_topology, so the node
    # was drawn with a plain border, no root caption and its
    # blocking ports ignored, while the STP panel two panels away
    # called it the root. One order of operations, three wrong
    # fields.
    _judge_stp(p.collected)


async def retire_port_names(p: MapPass) -> None:
    """The names of ports from before v0.8.6, renamed or reset by this
    poll."""
    # Port names from before v0.8.6 in the database, renamed or
    # reset by what this poll knows — before anything below reads
    # the database by port name
    if p.observe:
        await _retire_port_names(p.collected)


async def vouch_for_routers(p: MapPass) -> None:
    """The routers' own MACs, out of what the damaged-copy rules did to
    them."""
    # A router's own MACs vouch for themselves the way an IP from ARP
    # does (v0.8.11): never a damaged copy, never held back. What the
    # copy rules did to them before is undone here, before anything
    # below reads the database.
    p.identities = [identity for identity in p.identities or () if identity]
    p.own = router_own_macs(p.identities)
    if p.observe and p.own:
        await _release_router_macs(p.own)


async def weigh_addresses(p: MapPass) -> None:
    """What the database holds and has not accepted yet: the MACs that wait
    for confirmation, the candidates for copies, who does not vote."""
    # A MAC has to be seen more than once before it counts as a
    # device (unless ARP vouches for it); the verdict is needed
    # before the topology so that unconfirmed addresses stay out of
    # the per-port device counts as well as off the map
    p.db_rows = await asyncio.to_thread(rt.db.hosts_by_mac)
    # An empty database is the initial inventory — there is no map
    # yet for a phantom to pollute, and waiting would only show the
    # operator an empty screen on the first poll
    p.confirm_scans = rt.config.new_host_confirm_scans if p.db_rows else 1
    # Only switches actually read this cycle. A switch that ran out
    # of budget contributes the table it produced last time, and
    # counting a MAC again out of a copy of one reading is how an
    # address gets "seen in three polls" without anyone looking for
    # it twice.
    unconfirmed = await asyncio.to_thread(
        rt.db.projected_unconfirmed,
        {
            mac for sw in p.collected
            if sw.reachable and not sw.from_reading
            for mac in sw.fdb
        },
        p.confirm_scans,
    ) - set(p.own)
    # Damaged copies of a MAC are looked for among the addresses the
    # database has not accepted, and measured against the ones it
    # had accepted BEFORE this poll (v0.8.5). The projection above
    # will not do for that: a copy seen for the second time is not
    # in it — it is about to be confirmed — and it used to be taken
    # for a sample and confirmed. An empty database is the initial
    # inventory, as above: nothing in it to be a copy of.
    p.with_ip = set(p.arp) | set(p.own)
    p.grace = rt.config.host_grace_hours * 3600
    p.copy_candidates = corruption.candidates(
        {mac for sw in p.collected if sw.reachable for mac in sw.fdb},
        p.db_rows, p.with_ip,
    ) if p.db_rows else set()
    # …and the ones that will be found to be copies keep out of the
    # per-port device counts already, or a few of them seen twice
    # would make the port behind one real device a switch without
    # SNMP. Neither do the records marked as copies (v0.8.5).
    p.voting_out = unconfirmed
    if rt.config.filter_suspect_macs:
        p.voting_out = unconfirmed | _marked_copies(p.db_rows)
        p.voting_out |= corruption.likely_copies(
            {sw.ip: set(sw.fdb) for sw in p.collected if sw.reachable},
            p.copy_candidates,
            corruption.samples_by_switch(
                [], p.db_rows, p.with_ip, p.grace, time.time()
            ),
            rt.config.thresholds.corruption_hamming_bits,
        )


def infer_topology(p: MapPass) -> None:
    """The topology: switches, links, devices on their ports, switches
    without SNMP, bridges."""
    # What the database already knows about each port feeds the
    # unmanaged-switch threshold, so groups survive FDB aging
    (
        p.switches, p.links, p.hosts, p.pseudo_switches, p.vlan_names, p.bridges,
        p.topo_info,
    ) = build_topology(
        p.collected,
        rt.config.unmanaged_threshold,
        # a reading drawn at the start does not touch the smoothing
        fdb_stability=None if rt.config.demo or not p.observe else rt.fdb_stability,
        known_hosts_per_port=_known_hosts_per_port(
            p.db_rows, {sw.ip for sw in p.collected if sw.reachable}
        ),
        sticky_pseudo_ports=rt.prev_pseudo_ports,
        unconfirmed_macs=p.voting_out,
        place_trunk_only=rt.config.place_trunk_only_hosts,
        uplink_ports=_uplink_ports(),
        remembered_locations=_remembered_locations(p.db_rows),
    )


async def journal_dropped_links(p: MapPass) -> None:
    """The links the inference took back, into the journal."""
    # Links the inference removed, and rings it could not resolve.
    # The syslog line is for the moment it happens; the journal is
    # so the history of these decisions can be read from the
    # interface, where the map they changed is.
    for dropped in p.topo_info.get("dropped_links", []) if p.observe else ():
        await asyncio.to_thread(
            rt.db.add_event, time.time(), "link_dropped", "",
            _dropped_link_details(dropped),
        )


def add_stub_switches(p: MapPass) -> None:
    """A node for every switch with no reading; the ports of switches
    without SNMP kept for the next scan."""
    # The switches with no reading anywhere (v0.8.7): an empty node
    # each, standing on the port its management MAC is seen on — and
    # that MAC is the switch, not a device beside it
    stubs = _stub_switches(p.collected, p.hosts)
    stub_macs = {
        mac for sw in p.collected
        if not sw.reachable for mac in sw.own_macs
    }
    p.hosts = [h for h in p.hosts if h["mac"] not in stub_macs]
    p.switches = p.switches + stubs
    rt.prev_pseudo_ports = {
        (p["switch"], p["port"]) for p in p.pseudo_switches
    }


def find_copies(p: MapPass) -> None:
    """The damaged copies among the devices, marked — once the stubs have
    taken their switches' MACs away."""
    # Addresses a bit or two away from a real one on the same
    # switch: damaged frames, not new devices. They are held back
    # from confirmation for as long as they look like copies —
    # otherwise a permanently damaged port would simply confirm its
    # phantoms. A sample counts on any port of the switch and for
    # the grace window after it was last seen: a device switched
    # off or moved to the next port leaves its copies behind.
    p.suspects = corruption.find_suspects(
        p.hosts, p.copy_candidates,
        corruption.samples_by_switch(
            p.hosts, p.db_rows, p.with_ip, p.grace, time.time()
        ),
        rt.config.thresholds.corruption_hamming_bits,
    )
    _apply_suspects(p.hosts, p.suspects)
    p.suspect_macs = {
        s["mac"] for found in p.suspects.values() for s in found
    }


async def record_sightings(p: MapPass) -> None:
    """What this scan saw, written down: sightings, ARP, names,
    confirmations, copies taken off; the new MACs for the alarm."""
    # Devices seen only on uplinks are not on the map, but they
    # were seen: the sighting is recorded with no location at all,
    # so the inventory keeps them without anyone claiming a port.
    # `approximate` is what stops the empty location from
    # overwriting whatever the database already holds.
    p.uplink_only = p.topo_info.get("uplink_only", {})
    stale_switches = {
        sw.ip for sw in p.collected if sw.reachable and sw.from_reading
    }
    seen_nowhere = [
        {"mac": mac, "switch": "", "port": "", "vlan": 0,
         "approximate": True}
        for mac, sightings in p.uplink_only.items()
        # every sighting of it came out of a saved table: nobody
        # saw this address either
        if any(ip not in stale_switches for ip, _port in sightings)
    ]
    # A device behind a switch that ran out of budget is drawn from
    # the reading that did arrive, and that is right — the map must
    # not lose a whole branch every cycle. Recording it as a
    # sighting is a different matter: "last seen" is a moment in
    # time, seen_count counts polls a MAC was found in, and a
    # confirmation is the claim that several polls agree. None of
    # the three survives being fed the same reading twice.
    observed = [h for h in p.hosts if not h.get("from_saved")]
    copied = len(p.hosts) - len(observed)
    if copied and p.observe:
        log.info(
            "%d device(s) behind %d switch(es) without a full poll in "
            "this scan (over budget or not answering) are drawn from "
            "their last readings: they stay on the map, and none of it "
            "is recorded as a sighting",
            copied, len(stale_switches),
        )
    p.new_macs = []
    if not p.observe:
        pass
    elif rt.config.demo:
        p.new_macs = await asyncio.to_thread(
            rt.db.upsert_hosts, observed + seen_nowhere, p.confirm_scans,
            p.suspect_macs if rt.config.filter_suspect_macs else set(),
            set(p.own),
        )
        await asyncio.to_thread(demo.enrich_db, rt.db, p.hosts)
    else:
        p.new_macs = await asyncio.to_thread(
            rt.db.upsert_hosts, observed + seen_nowhere, p.confirm_scans,
            p.suspect_macs if rt.config.filter_suspect_macs else set(),
            set(p.own),
        )
        if p.arp:
            # ARP knows devices no switch port ever showed (behind a
            # router or an unpolled switch): keep them as inventory
            # with an empty switch_ip. Switches and routers
            # themselves are map nodes, not hosts.
            infra_ips = set(rt.config.switches) | set(rt.config.routers)
            own_macs = _switch_macs()
            arp_hosts = {
                mac: ip for mac, ip in p.arp.items()
                if mac not in own_macs and ip not in infra_ips
            }
            created = await asyncio.to_thread(
                rt.db.set_ips, arp_hosts, True
            )
            if created:
                log.info(
                    "ARP: %d devices known but not seen on any switch "
                    "port", created,
                )
        await resolve_names()
    if p.observe:
        # ARP vouches for an address, so a newcomer it knows joins the
        # map in the same poll it first appeared in
        p.new_macs += await asyncio.to_thread(rt.db.confirm_hosts_with_ip)
        if p.new_macs:
            log.info("New MACs confirmed: %d", len(p.new_macs))
        # copies confirmed before v0.8.5 held them back, now that this
        # poll's sightings and addresses are written
        await hide_confirmed_copies(set(p.own))
        # an ARP answer never makes a router's interface a new device
        p.new_macs = [mac for mac in p.new_macs if mac not in p.own]


async def settle_unconfirmed(p: MapPass) -> None:
    """Off the map, what the database has not accepted, now that this
    scan's sightings are in it; addresses ARP no longer confirms released."""
    p.unconfirmed = await asyncio.to_thread(rt.db.unconfirmed_macs)
    if p.unconfirmed and p.observe:
        log.info(
            "MACs awaiting confirmation (%d poll(s) each): %d",
            p.confirm_scans, len(p.unconfirmed),
        )
    p.db_rows = await asyncio.to_thread(rt.db.hosts_by_mac)
    if rt.config.filter_suspect_macs:
        # off the map whatever the database holds: a switch past its
        # budget is drawn from a reading nobody wrote down, and its
        # copies are in no table of unconfirmed addresses. Nor in
        # an offline group, behind a trunk or in the list beside
        # the map: _assemble_hosts leaves out what is in this set.
        p.unconfirmed |= p.suspect_macs | _marked_copies(p.db_rows)
    else:
        p.unconfirmed -= p.suspect_macs  # draw the damage instead
    p.hosts = [h for h in p.hosts if h["mac"] not in p.unconfirmed]
    rt.fdb_macs = {h["mac"] for h in p.hosts}
    if p.observe:
        await _release_unconfirmed_ips(p.db_rows)


def place_router_nodes(p: MapPass) -> None:
    """Each router one node, and its other MACs drawn as that node."""
    # One node per router of `routers:` (v0.8.11): one of its MACs, or
    # the LLDP bridge it is, and every other MAC of its drawn as that
    # node — on the same port, elsewhere, or only in the database
    p.placement = place_routers(
        p.identities, p.hosts, p.bridges,
        p.topo_info.get("other_devices", []) + p.topo_info.get("unidentified", []),
        _remembered_router_macs(
            p.own, p.db_rows, {sw.ip for sw in p.collected if sw.reachable},
            rt.fdb_macs | p.unconfirmed,
        ),
    )
    _merge_router_macs(p.placement, p.hosts, p.bridges)


def assemble_hosts(p: MapPass) -> None:
    """The devices of the map, the offline groups, the devices without a
    place, the groups behind trunks — with what the database knows."""
    # a record on an empty node has no port to be drawn at
    p.hosts, p.offline_groups, p.unlocated, p.trunk_groups = _assemble_hosts(
        p.hosts, p.db_rows, p.pseudo_switches,
        {sw.ip for sw in p.collected if sw.reachable},
        p.unconfirmed,
        {
            **p.placement.merged,
            **{h["mac"]: h["merged_into"] for h in p.hosts if h.get("merged_into")},
        },
        _nodes_on_port(p.bridges, p.pseudo_switches),
        p.uplink_only,
        p.topo_info.get("trunk_names", {}),
    )
    # A group that sits on a trunk was never seen there as devices:
    # its location is inherited guesswork, and the card should not
    # claim otherwise
    trunk_names = p.topo_info.get("trunk_names", {})
    for group in p.offline_groups:
        group["approximate"] = group["port"] in trunk_names.get(
            group["switch"], []
        )
    _merge_db_fields(p.hosts, p.db_rows)
    _merge_db_fields(p.unlocated, p.db_rows)


async def write_down(p: MapPass) -> None:
    """LLDP, the bridges and the full readings, into the database."""
    # LLDP: remember the neighbours and the bridges nobody polls, so
    # a card can say since when a device has been behind that port.
    # Only what a switch read in full this scan said: "last seen" of a
    # neighbour out of a reading is the same copy-as-sighting as a
    # device's would be.
    p.now = time.time()
    fresh = [sw for sw in p.collected if sw.map_state == STATE_FRESH]
    fresh_ips = {sw.ip for sw in fresh}
    if p.observe:
        await asyncio.to_thread(rt.db.upsert_lldp, _lldp_rows(fresh), p.now)
        await asyncio.to_thread(
            rt.db.upsert_bridges,
            [
                {
                    "chassis_id": b["chassis_id"],
                    "sys_name": b["name"],
                    "sys_desc": b["sys_desc"],
                    "mgmt_ip": b["mgmt_ip"],
                    "switch_ip": b["switch"],
                    "port": b["port"],
                    "capabilities": ",".join(b["capabilities"]),
                }
                for b in p.bridges if b["switch"] in fresh_ips
            ],
            p.now,
        )
        for ip, polled_at, data in p.readings:
            await asyncio.to_thread(
                rt.db.save_switch_reading, ip, polled_at, READING_FORMAT,
                __version__, data,
            )


async def date_bridges(p: MapPass) -> None:
    """Since when each bridge has been seen and whether it is known, from
    the rows just written."""
    p.bridge_rows = await asyncio.to_thread(rt.db.bridges)
    for bridge in p.bridges:
        row = p.bridge_rows.get(bridge["chassis_id"], {})
        bridge["first_seen"] = row.get("first_seen", p.now)
        bridge["last_seen"] = row.get("last_seen", p.now)
        bridge["known"] = _is_known_bridge(bridge)
    _merge_bridge_identity(p.bridges, p.hosts, p.db_rows)


async def caption_routers(p: MapPass) -> None:
    """Each router's node: its address, its interfaces, and a note or a pin
    of its other MACs moved to it."""
    # the address the operator reaches a router at, for the caption
    p.router_ips = _router_addresses(p.arp, p.identities)
    for host in p.hosts:
        if host["mac"] in p.router_ips:
            host["router_ip"] = p.router_ips[host["mac"]]
    for bridge in p.bridges:
        if bridge["chassis_id"] in p.router_ips:
            bridge["router_ip"] = p.router_ips[bridge["chassis_id"]]
    _mark_router_nodes(p.placement, p.hosts, p.bridges, p.db_rows)
    if p.observe:
        await _router_nodes_take_over(p.placement)


def find_uplink_suspects(p: MapPass) -> None:
    """Ports that look like a way out of the network, said in the log."""
    # Ports that look like a way out but are not configured as one.
    # Addresses are only known once the DB has been merged in, so
    # this happens here rather than inside build_topology.
    rt.uplink_suspects.clear()
    rt.uplink_suspects.update(
        suspect_uplink_ports(
            [sw for sw in p.collected if sw.reachable],
            [
                (h["switch"], h["port"], h.get("ip", ""))
                for h in p.hosts if h.get("switch") and h.get("port")
            ],
            _uplink_ports(),
            infrastructure=_infrastructure(),
            infrastructure_macs=set(p.router_ips) | {
                mac for mac, ip in p.arp.items() if ip in _infrastructure()
            },
        )
    )
    for (sw_ip, port), why in (
        sorted(rt.uplink_suspects.items()) if p.observe else ()
    ):
        log.info(
            "%s port %s looks like a way out of the network (%s). Add "
            "\"%s:%s\" to uplink_ports and what is behind it becomes "
            "one \"External network\" node instead of a crowd of "
            "devices, and raises no bridge alarms.",
            sw_ip, port, why["text"], sw_ip, port,
        )


def publish(p: MapPass) -> None:
    """The spanning-tree report, and the map published."""
    p.stp_report = _stp_report(p.collected, p.topo_info.get("trunk_names", {}))
    rt.state.update(
        p.switches, p.links, p.hosts, p.pseudo_switches, p.vlan_names, p.unlocated,
        p.offline_groups, p.bridges, p.stp_report,
        p.topo_info.get("external_networks", []),
        p.trunk_groups,
        scanned_at=None if p.observe else max(
            (sw.polled_at for sw in p.collected if sw.reachable), default=None
        ),
    )


async def report_switches(p: MapPass) -> None:
    """The switches, to the alarms: answered or not, all silent at once,
    past their budget."""
    # A switch that ran out of budget is not reported either way.
    # "Missed an SNMP poll" is a statement about the switch; this
    # one is a statement about us, and switch_down must not be
    # raised on the strength of it — nor cleared, which would be
    # just as much of an invention.
    # And it goes by the answer in this scan, never by what is drawn:
    # a switch on the map from its last reading may be silent now.
    await rt.alarm_engine.on_scan(
        {
            sw.ip: bool(sw.answered) for sw in p.collected
            if not sw.over_budget and sw.answered is not None
        },
        {sw.ip: sw.sys_name or sw.ip for sw in p.collected},
    )
    # Every single switch silent at once is not ten faults, it is
    # one. The day `community: public` went back into the config,
    # the map emptied and the log said "does not respond to SNMP"
    # ten times over — true, unhelpful, and identical to what a
    # power cut would have printed.
    p.answered = [sw for sw in p.collected if sw.answered]
    if p.collected and not p.answered and not p.over_budget:
        log.error(
            "All %d configured switch(es) are unreachable at once. One "
            "common cause is likelier than %d simultaneous faults: "
            "snmp.community (SNMPv2c does not answer a wrong one at "
            "all — silence is what a mismatch looks like), or the "
            "network of this machine. `python -m moonlan.diag --walk "
            "<switch> 1.3.6.1.2.1.1.5` says which.",
            len(p.collected), len(p.collected),
        )
    # A switch that keeps answering and keeps not finishing. It is
    # on the map, from a reading that may be hours old, and until
    # now the only way to learn that was to read the journal.
    # Every switch over budget goes in, not only those past the
    # threshold: an alarm that is already up has its text brought up to
    # date on every scan — after a restart, on the first (v0.8.8).
    stale_switches = {
        sw.ip: {
            "scans": sw.over_budget_scans,
            "polled_at": sw.polled_at if sw.map_state != STATE_NEVER else 0.0,
            "source": "saved" if sw.ip in rt.readings_from_database else "run",
            "started_at": STARTED_AT,
            "answered": sw.answered,
        }
        for sw in p.collected
        if sw.over_budget
    }
    await rt.alarm_engine.on_stale_switches(
        stale_switches,
        {sw.ip: sw.sys_name or sw.ip for sw in p.collected},
        threshold=max(rt.config.stale_switch_scans, 1),
        # how each switch came out of this scan: the words a standing
        # switch_stale clears with depend on it (v0.8.9)
        states={
            sw.ip: {"state": sw.map_state, "answered": sw.answered}
            for sw in p.collected
        },
    )
    if p.over_budget:
        log.warning(
            "Scan finished without %d of %d switch(es): %s did not "
            "answer in full inside the budget. The map below is "
            "everything else, and their last readings are kept as "
            "they were.",
            len(p.over_budget), len(p.collected),
            ", ".join(p.over_budget),
        )


async def report_network(p: MapPass) -> None:
    """The bridges, the spanning tree, the damaged copies and the new MACs,
    to the alarms."""
    await rt.alarm_engine.clear_suppressed(
        _suppressed_bridges(p.bridges, p.bridge_rows)
    )
    await rt.alarm_engine.on_bridges(p.bridges, _known_bridge_ids())
    await rt.alarm_engine.on_stp(p.stp_report)
    await rt.alarm_engine.on_corruption(
        {f"{sw_ip}:{port}": found
         for (sw_ip, port), found in p.suspects.items()},
        inbound_errors=_subject_inbound_errors,
        window_minutes=_copies_window() / 60,
        maker=lambda mac: rt.vendors.lookup(mac).name or "",
    )
    if p.new_macs and rt.first_scan_done:
        rows = await asyncio.to_thread(rt.db.hosts_by_mac)
        await rt.alarm_engine.on_new_macs(
            p.new_macs,
            {mac: _new_device_text(mac, rows[mac])
             for mac in p.new_macs if mac in rows},
        )


async def finish_scan(p: MapPass) -> None:
    """The first scan done and said; old positions forgotten, notes
    reconciled with the map."""
    rt.first_scan_done = True
    log.info(
        "Scan finished: %d switches, %d links, %d hosts",
        len(p.switches), len(p.links), len(p.hosts),
    )
    await _purge_layout_once()
    # A scan where no switch answered says nothing about where the
    # nodes are: it would count every note as one scan without its
    # node, and an outage of half an hour would empty the map of
    # them
    if p.answered:
        await _reconcile_notes()
