"""LLDP and the bridges (v0.8.14).

This poll's LLDP neighbours for the database, the addresses of kit that
is ours, the bridges known by configuration or because we poll them,
the identity of the device a bridge node is, the node standing on a
switch's port. Moved out of server.py word for word
(docs/ARCHITECTURE.md); the state is read as rt.X when they run.
Nothing here imports moonlan.server.
"""

from __future__ import annotations

from .. import demo
from ..derived import _uplink_ports
from ..runtime import rt
from ..snmp_collector import SwitchData
from ..topology import port_name


def _lldp_rows(collected: list[SwitchData]) -> list[dict]:
    """This poll's LLDP neighbours, flattened for the database."""
    rows: list[dict] = []
    for sw in collected:
        if not sw.reachable:
            continue
        for neighbor in sw.lldp_neighbors:
            rows.append({
                "switch_ip": sw.ip,
                "local_port": (
                    port_name(sw, neighbor.local_ifindex)
                    if neighbor.local_ifindex is not None
                    else f"lldpLocPortNum {neighbor.local_port_num}"
                ),
                "chassis_id": neighbor.chassis_id,
                "port_id": neighbor.port_id,
                "port_desc": neighbor.port_desc,
                "sys_name": neighbor.sys_name,
                "sys_desc": neighbor.sys_desc,
                "capabilities": ",".join(sorted(neighbor.cap_enabled)),
                "mgmt_ip": neighbor.mgmt_ip,
            })
    return rows


def _infrastructure() -> set[str]:
    """Addresses of kit that is ours: the switches and the routers.

    In demo mode these come from the demo network, not from the
    config.yaml that happens to be in the working directory.
    """
    if rt.config.demo:
        return set(rt.switch_data) | set(demo.ROUTERS)
    return set(rt.config.switches) | set(rt.config.routers)


def _merge_bridge_identity(
    bridges: list[dict], hosts: list[dict], db_rows: dict[str, dict]
) -> None:
    """Gives a bridge node the identity of the device it IS.

    Two ways the same box shows up twice. Its chassis MAC is in the FDB
    of the port LLDP found it on — the topology builder already marked
    that host as merged, and here the bridge picks up its IP, its DNS
    name and its ping state, so the node answers "is it up?" like any
    other. And its management address may sit in the inventory under a
    different MAC (a router announces one per VLAN interface): a host
    holding such an address on the bridge's own port is the same
    device, so its node goes too and the card says which addresses
    came from where.
    """
    ip_owner = {
        row["ip"]: mac for mac, row in db_rows.items() if row["ip"]
    }
    for bridge in bridges:
        row = db_rows.get(bridge["chassis_id"], {})
        bridge["ip"] = row.get("ip", "") or bridge.get("mgmt_ip", "")
        bridge["name"] = bridge.get("name") or row.get("name", "")
        bridge["dns_name"] = row.get("name", "")
        bridge["ping_up"] = bool(row.get("ping_up", 0))
        bridge["last_ping_ok"] = row.get("last_ping_ok", 0)
        if row.get("first_seen"):
            bridge["first_seen"] = min(
                bridge.get("first_seen", row["first_seen"]), row["first_seen"]
            )
        # the same box under another MAC, on this very port
        also: list[str] = []
        for address in bridge.get("mgmt_ips", []):
            mac = ip_owner.get(address)
            if not mac or mac == bridge["chassis_id"]:
                continue
            twin = db_rows.get(mac, {})
            if (twin.get("switch_ip"), twin.get("port")) != (
                bridge["switch"], bridge["port"]
            ):
                continue  # same address, but not behind this cable
            also.append(address)
            for host in hosts:
                if host["mac"] == mac:
                    host["merged_into"] = bridge["id"]
        bridge["also_ips"] = also


def _suppressed_bridges(
    bridges: list[dict], bridge_rows: dict[str, dict]
) -> dict[str, str]:
    """chassis id -> why the configuration says it is not an alarm.

    Covers bridges seen in this scan and bridges only the database
    remembers: a device behind a port that has since been declared an
    uplink may not be in LLDP right now, and its alarm would otherwise
    outlive the setting that answers it. The same applies to a device
    that turned out to be one of our own polled switches — the alarm
    for it is closed by this path, with the reason in the journal,
    rather than waiting for a neighbour that is never going away.
    """
    uplinks = _uplink_ports()
    known = set(rt.config.known_bridges)
    ours = _polled_identities()
    located: dict[str, tuple[str, str, str]] = {
        row["chassis_id"]: (row["switch_ip"], row["port"], row["mgmt_ip"])
        for row in bridge_rows.values()
    }
    located.update({
        b["chassis_id"]: (b["switch"], b["port"], b.get("mgmt_ip", ""))
        for b in bridges
    })
    suppressed: dict[str, str] = {}
    for chassis_id, (switch_ip, port, mgmt_ip) in located.items():
        if (switch_ip, port) in uplinks:
            suppressed[chassis_id] = (
                f"{switch_ip} {port} is listed in uplink_ports"
            )
        elif chassis_id.lower() in known or (mgmt_ip or "").lower() in known:
            suppressed[chassis_id] = "listed in known_bridges"
        elif chassis_id.lower() in ours:
            suppressed[chassis_id] = ours[chassis_id.lower()]
        elif (mgmt_ip or "").lower() in ours:
            suppressed[chassis_id] = ours[mgmt_ip.lower()]
    return suppressed


def _polled_identities() -> dict[str, str]:
    """Every way a device we already poll can appear in someone's LLDP.

    A switch listed in `switches:` is polled by definition, and so a
    neighbour reporting it is not "a switch nobody polls". It used to
    be: an access switch sat in `switches:`, answered every scan, and still
    carried an unmanaged_bridge_detected alarm for 91 hours, because
    only `known_bridges` counted as known. Making the operator copy
    their own switch addresses into a second list to silence that is
    asking them to work around a bug.

    Returns identity -> why it is ours, lowercased: the configured
    switch and router addresses, and every MAC each polled switch is
    known by (bridge base MAC, interface MACs, its management-IP MAC
    from ARP) — a neighbour may name any of them as the chassis id.
    """
    ours: dict[str, str] = {}
    for ip in _infrastructure():
        ours[ip.lower()] = (
            "it is a router MoonLan polls" if ip in set(rt.config.routers)
            else "it is a switch MoonLan polls"
        )
    for ip, sw in rt.switch_data.items():
        why = f"it is {sw.sys_name or ip}, a switch MoonLan polls"
        for mac in sw.own_macs:
            ours[mac.lower()] = why
        if sw.bridge_mac:
            ours[sw.bridge_mac.lower()] = why
    return ours


def _known_bridge_ids() -> set[str]:
    """Chassis ids and addresses that must never raise a bridge alarm."""
    return set(rt.config.known_bridges) | set(_polled_identities())


def _is_known_bridge(bridge: dict) -> bool:
    """Known by configuration, or one of the devices we poll ourselves."""
    known = _known_bridge_ids()
    return (
        bridge["chassis_id"].lower() in known
        or bridge.get("mgmt_ip", "").lower() in known
    )


def _nodes_on_port(
    bridges: list[dict], pseudo_switches: list[dict]
) -> dict[tuple[str, str], dict]:
    """(switch, port) -> the node standing on that cable, if any.

    A pseudo-switch wins over a bridge where both are present: that is
    the case of several bridges behind one unmanaged box, and the live
    devices stay on the box, so the quiet ones must too.

    A device MoonLan polls itself is never offered as a parent. Groups
    hang off what is on the cable, and if devices were behind a switch
    we poll they would be on its downlink ports — believing otherwise
    is the whole of the v0.6.9 defect. Such a switch is drawn as its
    own node on the same port instead.
    """
    ours = set(_polled_identities())
    nodes: dict[tuple[str, str], dict] = {}
    for bridge in bridges:
        if bridge.get("trunk") or bridge.get("shares_port"):
            continue
        if (bridge["chassis_id"].lower() in ours
                or bridge.get("mgmt_ip", "").lower() in ours):
            continue
        nodes[(bridge["switch"], bridge["port"])] = {
            "id": bridge["id"], "name": bridge["name"],
        }
    for pseudo in pseudo_switches:
        nodes[(pseudo["switch"], pseudo["port"])] = {
            "id": pseudo["id"], "name": "",
        }
    return nodes
