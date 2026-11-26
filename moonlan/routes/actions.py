"""The node menu and what it starts (v0.8.13).

/api/node-menu — what the context menu of a node offers, the links of
config.yaml among them — /api/actions, which starts a ping or a
traceroute and says how it goes, and PATCH /api/host/{mac}, whether a
host is monitored. Moved out of server.py word for word
(docs/ARCHITECTURE.md); the state is read as rt.X when a route runs.
Nothing here imports moonlan.server.
"""

from __future__ import annotations

import asyncio
import logging
import time

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .. import menu, probes, signin
from ..runtime import rt
from .common import _refuse

log = logging.getLogger("moonlan")

router = APIRouter()


class HostPatch(BaseModel):
    monitored: bool


@router.patch("/api/host/{mac}")
async def api_patch_host(mac: str, body: HostPatch):
    mac = mac.lower()
    ok = await asyncio.to_thread(rt.db.set_monitored, mac, body.monitored)
    if not ok:
        return JSONResponse({"error": "unknown host"}, status_code=404)
    # who asked for alarms about this device, or stopped them
    await asyncio.to_thread(
        rt.db.add_event, time.time(),
        "monitor_on" if body.monitored else "monitor_off", mac, "",
        signin.actor(),
    )
    return {"mac": mac, "monitored": body.monitored}


def _node_directory() -> dict[str, dict]:
    """Every node on the map with what the node menu needs to know.

    The page names a node by its id and nothing else. The address a
    ping goes to, the MAC a link is filled with, all come from here —
    MoonLan's own topology and database — and never from the request.
    `switch` is the address of the switch the node hangs off (a
    switch's own, for a switch), `port` the port there.
    """
    topo = rt.state.as_dict()
    db_hosts = rt.db.hosts_by_mac()
    nodes: dict[str, dict] = {}

    def entry(kind, ip="", mac="", name="", switch="", port="", row=None):
        row = row or {}
        return {
            "kind": kind, "ip": ip or "", "mac": mac or "",
            "name": name or "", "switch": switch or "", "port": port or "",
            # what the continuous ping knows, shown beside a one-off
            # ping as the second source it is
            "ping_up": bool(row["ping_up"]) if "ping_up" in row else None,
            "last_ping_ok": row.get("last_ping_ok", 0) or 0,
        }

    for sw in topo["switches"]:
        nodes["sw:" + sw["ip"]] = entry(
            "switch", sw["ip"], sw.get("mac"), sw.get("name"), sw["ip"],
            row=rt.switch_ping.get(sw["ip"]),
        )
    for host in topo["hosts"]:
        if host.get("merged_into"):
            continue  # drawn as its bridge node
        row = db_hosts.get(host["mac"], {})
        nodes["host:" + host["mac"]] = entry(
            "host",
            row.get("ip") or host.get("router_ip"),
            host["mac"],
            row.get("name") or (host.get("lldp") or {}).get("sys_name"),
            host.get("switch"), host.get("port"), row,
        )
    for bridge in topo.get("bridges") or ():
        row = db_hosts.get(bridge.get("chassis_id", ""), {})
        nodes[bridge["id"]] = entry(
            "switch",
            bridge.get("router_ip") or row.get("ip") or bridge.get("mgmt_ip"),
            bridge.get("chassis_id"), bridge.get("name"),
            bridge.get("switch"), bridge.get("port"), row,
        )
    for key in ("pseudo_switches", "trunk_groups", "offline_groups",
                "external_networks"):
        for group in topo.get(key) or ():
            nodes[group["id"]] = entry(
                "group", switch=group.get("switch"), port=group.get("port"),
            )
    return nodes


def _tool_state() -> dict:
    """What the menu can run, for greying out what it cannot."""
    trace = rt.tools.get("traceroute")
    return {
        "ping": bool(rt.tools.get("ping")),
        "traceroute": trace[0] if trace else None,
        "simulated": rt.config.demo,
        "max_targets": rt.config.context_menu.max_targets,
    }


def _menu_links(node_id: str, facts: dict) -> tuple[list[dict], list[dict]]:
    """(built-in links, the operator's links) for one node, filled in.

    A link that lacks a value is still listed, with the field it lacks:
    the page shows it greyed out with the reason, the way "no answer"
    is never shown as zero.
    """
    kind = facts["kind"]
    builtin: list[dict] = []
    if kind in ("switch", "host"):
        if node_id.startswith("sw:"):
            scheme = rt.config.web_scheme(facts["ip"])
        elif kind == "switch":
            scheme = rt.config.context_menu.web_scheme
        else:
            scheme = "http"
        for key, template in (("web", scheme + "://{ip}"),
                              ("ssh", "ssh://{ip}")):
            url, missing = menu.expand(template, facts)
            builtin.append({"key": key, "url": url, "missing": missing})
    custom: list[dict] = []
    for link in rt.config.context_menu.links:
        if not link.applies(kind):
            continue
        url, missing = menu.expand(link.url, facts)
        custom.append({"label": link.label, "url": url, "missing": missing})
    return builtin, custom


@router.get("/api/node-menu")
async def api_node_menu(id: str = Query(...)):
    """What the right-click menu of one node can offer."""
    nodes = await asyncio.to_thread(_node_directory)
    facts = nodes.get(id)
    if facts is None:
        return JSONResponse(
            {"error": "unknown_node", "node": id}, status_code=404
        )
    builtin, custom = _menu_links(id, facts)
    return {
        "node": {"id": id, **facts},
        "links": builtin,
        "custom": custom,
        "tools": _tool_state(),
    }


class ActionBody(BaseModel):
    action: str
    nodes: list[str]


@router.get("/api/actions")
async def api_actions_state() -> dict:
    return {**_tool_state(), "running": rt.jobs.running(),
            "max_running": rt.config.context_menu.max_running}


@router.post("/api/actions")
async def api_start_action(body: ActionBody, request: Request):
    """Starts ping or traceroute from this machine; returns the job.

    Node ids in, never addresses: an id MoonLan does not know is
    refused, and so is an address sent in place of one. Whatever the
    tool is run against was found by MoonLan itself.
    """
    if body.action not in probes.ACTIONS:
        return _refuse("unknown_action", 400, action=body.action)
    ids = list(dict.fromkeys(body.nodes))
    if not ids:
        return _refuse("no_targets", 400)
    limit = rt.config.context_menu.max_targets
    if len(ids) > limit:
        # refused, not trimmed: a silently shortened list reads as
        # "these are all of them"
        return _refuse("too_many_targets", 400, limit=limit, count=len(ids))
    if body.action == "traceroute" and len(ids) > 1:
        return _refuse("traceroute_one", 400)
    if body.action == "ping" and not rt.tools.get("ping"):
        return _refuse("tool_missing", 503, tool="ping")
    if body.action == "traceroute" and not rt.tools.get("traceroute"):
        return _refuse("tool_missing", 503, tool="traceroute")
    nodes = await asyncio.to_thread(_node_directory)
    unknown = [node_id for node_id in ids if node_id not in nodes]
    if unknown:
        return _refuse(
            "unknown_nodes", 404, nodes=unknown[:10],
            # the one mistake worth naming: an address is not a node id
            addresses=[n for n in unknown if probes.checked_address(n)],
        )
    targets = [
        probes.Target(
            node=node_id, kind=nodes[node_id]["kind"],
            name=nodes[node_id]["name"] or nodes[node_id]["ip"] or node_id,
            ip=nodes[node_id]["ip"],
            monitor={
                "ping_up": nodes[node_id]["ping_up"],
                "last_ping_ok": nodes[node_id]["last_ping_ok"],
            },
        )
        for node_id in ids
    ]
    try:
        job = rt.jobs.start(
            body.action, targets, rt.config.context_menu.max_running,
            owner=signin.actor(),
        )
    except probes.Busy:
        return _refuse("busy", 429, limit=rt.config.context_menu.max_running)
    client = request.client.host if request.client else "?"
    shown = ", ".join(
        f"{t.node} ({t.ip or 'no address'})" for t in targets[:5]
    ) + (f" and {len(targets) - 5} more" if len(targets) > 5 else "")
    log.info(
        "Menu: %s of %s requested from %s%s (job %s)",
        body.action, shown, client,
        f" by {signin.actor()}" if signin.actor() else "", job.id,
    )
    return job.as_dict()


@router.get("/api/actions/{job_id}")
async def api_action_state(job_id: str):
    """A ping's result is for whoever started it, and for an
    administrator. Somebody else's job answers as if it did not exist."""
    job = rt.jobs.get(job_id)
    who = signin.principal()
    if job is None or (
        who is not None and who.role != "admin" and job.owner != who.name
    ):
        return _refuse("unknown_job", 404)
    return job.as_dict()
