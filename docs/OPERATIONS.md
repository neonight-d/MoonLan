# MoonLan Operations & Troubleshooting

## Startup

Check the startup log first. It reports the configuration path, database path, enabled features, and configuration problems.

Set the configuration explicitly when needed:

~~~bash
export MOONLAN_CONFIG=/absolute/path/to/config.yaml
~~~

## Health checks

Simple liveness, without sign-in:

~~~bash
curl -fsS http://127.0.0.1:8080/api/health
~~~

Detailed state — needs a viewer once sign-in is on; see
[HEALTHCHECK.md](HEALTHCHECK.md) for reading it from the server itself:

~~~bash
curl -s http://127.0.0.1:8080/api/status
~~~

## Users and access

Accounts are managed on the server, with the service running. Run the
command where MoonLan runs, with the same `config.yaml` (or
`MOONLAN_CONFIG`): it prints which database it works on. Full story:
[SIGN-IN.md](SIGN-IN.md).

The first administrator — sign-in switches on at the next request of
every open map, no restart:

~~~bash
python -m moonlan.users add <name> --role admin
~~~

Its second factor, bound here so the secret never crosses the network:

~~~bash
python -m moonlan.users totp <name>
~~~

Getting somebody back in:

~~~bash
python -m moonlan.users list                  # roles, state, TOTP, locks, sessions
python -m moonlan.users unlock <name>         # after ten wrong passwords
python -m moonlan.users passwd <name> --temporary
python -m moonlan.users reset-totp <name>     # a lost phone and no recovery codes
~~~

Passwords are asked for, never taken from the command line. The last
enabled administrator cannot be deleted, disabled or demoted.
`diag --config` shows administrators without TOTP and accounts locked
right now. Run `diag` as the user MoonLan runs as: it asks the service
with a token only that user can read.

## Empty map

1. Check /api/health.
2. Check /api/status (signed in, or with the console token).
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
