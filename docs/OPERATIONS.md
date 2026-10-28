# MoonLan Operations & Troubleshooting

## Installing

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

`fido2` (signing in with a key, since v0.7.6) brings `cryptography`,
which pip installs as a ready-made wheel on x86-64 and ARM64 Linux with
glibc or musl. Where no wheel fits — an old pip, an unusual CPU or
libc — pip tries to build it and needs Rust and the OpenSSL headers.
Either upgrade pip first (`.venv/bin/pip install --upgrade pip`), or
take the distribution's package and let the virtual environment see it:

```bash
sudo apt install python3-cryptography     # Debian, Ubuntu
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements.txt
```

Without `fido2` MoonLan runs as before: signing in with a key is off,
the log says why at startup, and so does the sign-in form. Passwords
and TOTP do not need it.

## Running

```bash
python run.py
```

Open `http://server_address:8080` in a browser.

## Running as a service (systemd)

Or in a container, with docker compose: [DOCKER.md](DOCKER.md).

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

## The maker of a device (IEEE register)

MoonLan names who made a device from the first bytes of its MAC address,
by the register IEEE publishes. The register is not in the repository —
it is several megabytes and changes every week. Fetch it:

```bash
.venv/bin/python -m moonlan.oui update
```

It downloads four CSV files (MA-L, MA-M, MA-S, IAB) into `oui/` next to
the database (`oui.path` to put them elsewhere), checks that each one
is a complete register, and only then replaces the old files: a
download cut short leaves the register as it was. MoonLan reads it at
startup, so restart the service afterwards. Without it the map works
as before and devices show "registry not loaded".

IEEE turns away some clients it takes for robots (HTTP 403 or 418), and
a machine may have no way out to the internet at all. Then download the
four files in a browser and copy them into the folder the command
printed, under these names:

| File | Register | URL |
|---|---|---|
| `oui.csv` | MA-L | https://standards-oui.ieee.org/oui/oui.csv |
| `mam.csv` | MA-M | https://standards-oui.ieee.org/oui28/mam.csv |
| `oui36.csv` | MA-S | https://standards-oui.ieee.org/oui36/oui36.csv |
| `iab.csv` | IAB | https://standards-oui.ieee.org/iab/iab.csv |

`python -m moonlan.diag --config` says what MoonLan reads: the folder,
rows per register and the date of the files. A register older than
180 days is a line in the log at startup.

Monthly, with systemd — the update as the service's user, then a
restart of the map:

```ini
# /etc/systemd/system/moonlan-oui.service
[Unit]
Description=Update the IEEE register for MoonLan

[Service]
Type=oneshot
User=<user>                # the same as in moonlan.service
WorkingDirectory=/opt/moonlan
ExecStart=/opt/moonlan/.venv/bin/python -m moonlan.oui update
ExecStartPost=+/usr/bin/systemctl try-restart moonlan.service
```

```ini
# /etc/systemd/system/moonlan-oui.timer
[Unit]
Description=Update the IEEE register for MoonLan monthly

[Timer]
OnCalendar=monthly
RandomizedDelaySec=1d
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now moonlan-oui.timer
```

A failed update leaves the old register and the service untouched:
`ExecStartPost` runs only after a successful `ExecStart`.

## Names and notes from a file

A node's name and note (v0.8.0) are written in its card. For many at
once — an inventory kept in a spreadsheet, or moving notes to another
installation — there is a CSV, imported by MAC address.

```
mac;label;note
10:FF:E0:44:02:5E;Accounting;Desk 105
088AF15E3910;;Wi-Fi in the library
```

- **The first line** names the columns: `mac;label;note`, or in
  Russian `mac;подпись;заметка`, in any case. `note` may be left out.
- **The separator** is `;` — what Excel saves in a Russian Windows —
  or `,` or a tab, whichever the first line uses.
- **The encoding** is UTF-8, with a BOM or without; a file that is not
  UTF-8 is read as cp1251, which is how Excel saves CSV in a Russian
  Windows.
- **Quotes** as Excel writes them (RFC 4180); a note in quotes may run
  over several lines.
- **A MAC** in any common writing: `aa:bb:cc:dd:ee:ff`,
  `AA-BB-CC-DD-EE-FF`, `aabb.ccdd.eeff`, `AABBCCDDEEFF`.
- A name is at most 40 characters on one line, a note at most 2000; a
  longer one makes the line an error, not a cut.

A MAC goes on a device first, then on the switch whose bridge MAC it
is, then on the LLDP bridge whose chassis id it is. A MAC nobody has is
kept for the device that has it, under "Device not found yet" in
"Notes without a node", and is on its node as soon as it appears.

- **An empty cell changes nothing**; deleting is done in the card.
- **A note written by hand is never overwritten** by an import; the
  line is reported as skipped. A note from an earlier import is
  overwritten — and becomes a hand-written one once somebody edits it.
- **A MAC twice** in the file: the last line counts, with a remark.

**On the map** (administrators): Actions (⋯) → "Import notes (CSV)…".
The file is checked and nothing written until the preview — how many
would be written, how many of them are on the map and how many not
found yet, how many skipped, every error with its line — has been
answered with "Import". "Export notes (CSV)" (anyone) downloads the
notes of nodes that have a MAC in the same format, UTF-8 with a BOM and
`;`; notes of switches without SNMP, external networks and groups have
no MAC to be written by, and the export says how many stayed out. The
journal gets one entry per import.

**On the server**, through the running service — the same checks and
the same report:

```bash
python -m moonlan.notes import notes.csv --dry-run
python -m moonlan.notes import notes.csv
python -m moonlan.notes export notes.csv
```

Run it as the user MoonLan runs as, in its directory: with sign-in on,
it is let in by the token the service writes next to its database at
startup, the one `diag` uses, and the journal names it "the server
console". `docs/notes_example.csv` is a file to try it on the demo.

## Startup

Check the startup log first. It reports the configuration path, database path, enabled features, and configuration problems.

Set the configuration explicitly when needed:

~~~bash
export MOONLAN_CONFIG=/absolute/path/to/config.yaml
~~~

## Settings: from config.yaml to the page (v0.8.4)

What the map shows and how alarms rise — grouping thresholds, how long
devices stay, the polling intervals, the alarm thresholds, the alarm →
channel table, the shared recipients — is changed in the page now:
the Actions menu → Settings, by an administrator, without a restart.
The values are kept in the database. The list, with ranges:
[CONFIGURATION.md](CONFIGURATION.md#in-settings-since-v084).

After the upgrade, the first start moves every such value your
`config.yaml` has into the database — the journal says "Settings from
config.yaml" with each value — and from then on that part of the file
is not read. The log says which keys to remove, at every start until
they are gone:

~~~text
WARNING moonlan: In Settings now, so config.yaml's values are not used — remove them from config.yaml: offline_group_threshold, scan_interval_minutes, alarm_notify
~~~

`python -m moonlan.diag --config` lists the same keys, and every
setting with its value, where it comes from and who changed it last.
Remove them and restart: the values stay as they were. A value out of
range in the file is not moved — the log names it, and the default
applies until it is set in the page.

## Personal notifications

Every account — viewer, user or administrator — can add its own email
and Telegram for alarms in Settings → My notifications. Until it is
confirmed, an address gets nothing but its confirmation.

- **Email** is confirmed with a six-digit code sent through the SMTP
  server of `config.yaml` (fifteen minutes, five tries; one code a
  minute, five an hour). The email channel has to be set up:
  `notifications.email.enabled`, `smtp_host`, `mail_from`.
- **Telegram** is connected with a link to MoonLan's bot (or its QR
  code): the person presses Start, and the private chat it came from
  is the address. A group cannot be connected this way.
- **Which alarms**: what the shared table sends to that channel —
  following it when it changes — or one's own choice.
- Repeats and flapping are damped once, for everybody. An account that
  is disabled gets nothing; deleting it deletes its subscriptions;
  Users → ⋯ → "Turn notifications off" removes somebody's without
  deleting the account.

### A bot for MoonLan alone

To connect a chat, MoonLan reads the bot's updates (`getUpdates`)
while a link waits. Telegram gives a bot's updates to one reader: if
another program polls the same bot, or the bot has a webhook, Telegram
answers 409 Conflict, the log says so with Telegram's own words, and
the page tells the person that a link cannot work — use email. Give
MoonLan a bot of its own: @BotFather → /newbot, then its token in
`notifications.telegram.bot_token`. Sending to chats is not affected
either way; only connecting by a link is.

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
python -m moonlan.users list                  # roles, state, TOTP, keys, locks, sessions
python -m moonlan.users unlock <name>         # after ten wrong passwords
python -m moonlan.users passwd <name> --temporary
python -m moonlan.users reset-totp <name>     # a lost phone and no recovery codes
python -m moonlan.users passkeys <name>       # the keys, numbered
python -m moonlan.users remove-passkey <name> <number>
python -m moonlan.users reset-passkeys <name> # every key lost
~~~

Passwords are asked for, never taken from the command line. The last
enabled administrator cannot be deleted, disabled or demoted, and an
administrator's last second factor cannot be removed one by one.
`diag --config` shows administrators without a second factor, accounts
locked right now, and — in its HTTPS section — the certificate, the
proxies and whether keys can work ([HTTPS.md](HTTPS.md)). Run `diag` as the user MoonLan runs as: it asks the service
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
