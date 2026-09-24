# MoonLan API

The API is intended for local service use and currently has no authentication layer. Restrict access to a trusted network.

## Health

### GET /api/health

Lightweight liveness endpoint for service managers and reverse proxies.

~~~json
{
  "status": "ok",
  "version": "0.7.4",
  "scanning": false,
  "last_scan": 1720000000.0,
  "last_scan_ok": 1720000000.0,
  "last_error": "",
  "last_error_ts": 0.0
}
~~~

The response uses Cache-Control: no-store. It answers whether the web process is alive and intentionally does not require SNMP to be healthy.

## Operational status

### GET /api/status

Returns version, demo mode, device counts, scan progress, layout timestamps, and process resource hints.

## Topology

### GET /api/topology

Returns switches, links, hosts, unlocated devices, pseudo-switches, LLDP-discovered bridges, VLAN names, and current monitoring fields.

### GET /api/switch/{ip}/ports

Returns port status, speed, traffic rates, errors, discards, LAG membership, LLDP information, link changes, and loop-detection state.

## Monitoring

### GET /api/stp

Returns per-switch STP information and the network-level verdict.

### GET /api/alarms

Query parameters: active=1 for active alarms, active=0 for cleared/history, and limit=1..500.

### POST /api/alarms/{id}/clear

Manually clears an active alarm.

### GET /api/journal

Returns recent journal events. The limit parameter is bounded by the server.

## Inventory

### GET /api/search?q=...

Searches known devices by identifiers such as name, IP, and MAC.

### PATCH /api/host/{mac}

Changes whether a host is monitored for host-down alarms.

Example:

~~~json
{"monitored": true}
~~~

## Layout

### GET /api/layout

Returns saved positions, pinned state, neighbour offsets, reset timestamps, and missing/orphaned nodes.

### PUT /api/layout

Stores positions. The existing only_new behavior lets older clients avoid overwriting positions placed by another browser.

### PATCH /api/layout

Places/releases nodes and records neighbour offsets.

### DELETE /api/layout

Resets the shared layout.

## Node actions

### GET /api/node-menu?id=...

Resolves the actions available for a node.

### POST /api/actions

Starts a ping or traceroute job using MoonLan node IDs.

Example:

~~~json
{
  "action": "ping",
  "nodes": ["host:aa:bb:cc:00:00:01"]
}
~~~

The server rejects unknown IDs, arbitrary addresses, excessive target counts, unavailable tools, and unsupported actions.

### GET /api/actions/{id}

Returns the current state and results of an action job.

## Compatibility

Prefer additive response fields when extending an endpoint. Avoid changing the meaning or type of an existing field without a documented migration path.
