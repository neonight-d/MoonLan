# MoonLan Operations & Troubleshooting

## Running

```bash
python run.py
```

Open `http://server_address:8080` in a browser.

## Running as a service (systemd)

An example unit lives in [docs/deploy/moonlan.service](deploy/moonlan.service).
Adjust `User=`, `WorkingDirectory=` and the venv path, then:

```bash
sudo cp docs/deploy/moonlan.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now moonlan
journalctl -u moonlan -f        # follow the service log
```

The unit sets `Restart=on-failure` and `LimitNOFILE=65535` (a safety
net; the service itself reuses one SNMP engine per process, so file
descriptors do not accumulate).

Resource usage is logged at INFO every 10 minutes — check for leaks
with:

```bash
journalctl -u moonlan | grep "open fds"
```

(your user must be in the `systemd-journal` group to read the journal
of a system service). The same numbers are exposed as `open_fds` and
`rss_kb` in `/api/status`.

## Demo mode (no real switches)

```bash
MOONLAN_DEMO=1 python run.py
```

The service generates a virtual network — a star of five switches with
LACP, VLANs, an unmanaged switch and a couple dozen hosts. The demo
also exercises the monitoring: live traffic curves on ports, one port
with growing errors (a port_errors alarm within a couple of minutes),
a host_down that raises and clears, new devices on a rescan. It also
covers everything v0.6 added: a switch the MAC tables cannot place at
all and LLDP can (a link with source `lldp`), a named MikroTik bridge
behind an access port with its `unmanaged_bridge_detected` alarm, a
port carrying forwarded LLDP frames, a neighbour with its optional
TLVs switched off, a spanning tree with a root and a blocking port
next to a switch whose STP is off but still reports itself as root,
and a port that flaps on every cycle. Loop detection is in it too: on
the second counters cycle the box behind access-sw-1 Gi0/14 gets both
ends of a patch cord — a `loop_detected` alarm, a red edge and a red
switch outline — while one switch reports loop detection switched off
globally and another is a model no profile covers. Instead of sending
anything, notifications are logged as `NOTIFY (demo): …`.

### A second instance beside a running service

```bash
MOONLAN_CONFIG=/tmp/dev.yaml MOONLAN_DEMO=1 python run.py
```

`MOONLAN_CONFIG` points at another `config.yaml`, so a run started for
a quick look takes its own `db_path` and its own port instead of the
production ones. Without it, everything started in the project
directory loads the production config — and a demo started that way
writes demo hosts into the real inventory. The startup log prints the
absolute path of both the config file and the database, so it is
always clear which instance is talking to what. The database runs in
WAL mode, so a `diag` run or a test alongside the service does not
hand it `database is locked`.

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
