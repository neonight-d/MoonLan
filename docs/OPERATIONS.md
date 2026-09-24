# MoonLan Operations & Troubleshooting

## Startup

Check the startup log first. It reports the configuration path, database path, enabled features, and configuration problems.

Set the configuration explicitly when needed:

~~~bash
export MOONLAN_CONFIG=/absolute/path/to/config.yaml
~~~

## Health checks

Simple liveness:

~~~bash
curl -fsS http://127.0.0.1:8000/api/health
~~~

Detailed state:

~~~bash
curl -s http://127.0.0.1:8000/api/status
~~~

## Empty map

1. Check /api/health.
2. Check /api/status.
3. Run python -m moonlan.diag --config.
4. Verify configured switch addresses.
5. Verify SNMP reachability and community strings.
6. Use diag --walk or a switch-specific diagnostic.

Do not immediately increase every timeout. First distinguish authentication, reachability, unsupported OID, and genuinely slow-agent problems.

## Slow switch

Use the per-device poll budget. A late switch should be marked over-budget, keep its last complete reading, allow other switches to update, and not have that saved reading counted as a new observation.

Inspect timing with /api/status and python -m moonlan.diag --config.

## Incorrect topology

Run:

~~~bash
python -m moonlan.diag --topology
~~~

Check LLDP vs FDB evidence, dropped links, unresolved rings, trunk direction, approximate placement, and stale/over-budget sources.

## Port dashes

A dash can mean a counter was not measured or is too old to display. It is not automatically zero.

~~~bash
python -m moonlan.diag --port <switch> <port>
python -m moonlan.diag --port <switch> <port> --watch
~~~

## Loop alarms

~~~bash
python -m moonlan.diag --loop
~~~

Check the model profile and raw status value before changing the network.

## Suspicious MAC addresses

~~~bash
python -m moonlan.diag --fdb <switch>
python -m moonlan.diag --host <ip-or-mac>
~~~

Invalid, multicast, broadcast, or malformed FDB rows should not become normal devices.

## Sharing diagnostics

Before sharing diagnostics outside the trusted network, anonymize them and review the output:

~~~bash
python -m moonlan.diag --anonymize ...
~~~

## Database

The SQLite database contains operational network information. Back it up before migrations or destructive maintenance.

Do not delete the database as a first troubleshooting step: it removes inventory history, alarms, journal entries, and saved layout.
