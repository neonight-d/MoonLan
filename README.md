[Читать по-русски → README_RU.md](README_RU.md)

# MoonLan

**MoonLan** is a web service for Linux that automatically builds the physical
topology of a local network from data collected from switches via SNMP and
displays it in a browser.

An open-source alternative to LanTopoLog. MIT license.

## Features (v0.5.8)

- SNMP v2c polling of switches: device name, ports, speeds, statuses.
- MAC address tables (BRIDGE-MIB and Q-BRIDGE-MIB) from every switch,
  including entries on trunk bridge-ports missing from
  `dot1dBasePortIfIndex` (e.g. D-Link LACP trunks).
- Accurate topology inference: a switch-to-switch link is drawn only when
  it is direct (FDB set-intersection criterion) — no false links between
  the rays of a star. A switch is recognized in neighbors' FDB by its full
  MAC set (bridge MAC, interface MACs, management-IP MAC), with a fallback
  exclusion rule for one-way visibility. Link cards show ports of both ends.
- Link stability: FDB entries are merged over the last 3 polls, so links
  do not flicker when MAC table entries age out.
- LACP (IEEE8023-LAG-MIB): an aggregate is drawn as a single thick line
  labeled "LACP N×speed" with the member ports listed in the link card.
- VLAN (Q-BRIDGE-MIB): each host's port PVID and VLAN names are shown
  in the host card and the device list.
- Unmanaged switch detection: when many hosts are visible behind one port,
  they are grouped under a "Switch without SNMP" node
  (`unmanaged_threshold` in the configuration).
- Host IP addresses from routers' ARP tables (`routers` section),
  host names via reverse DNS. An IP belongs to exactly one MAC, so a
  device replaced or re-addressed by DHCP does not linger as a second
  host record.
- Stable inventory: a host whose MAC left the switch tables stays on
  the map at its last known port for `host_grace_hours` (default 24),
  greyed out and marked "last seen …", instead of blinking with every
  MAC-table timeout. Devices ARP knows but no switch port shows — a
  subnet behind a router, for instance — are listed under "Not on map",
  searchable and pingable. `python -m moonlan.diag --hosts` reports how
  complete the inventory is and which subnets are missing from it.
- A readable map at any size: offline devices sharing a port hang off
  one "Offline · N" node instead of surrounding every switch with a
  cloud of grey dots (`offline_group_threshold`); the devices stay
  visible behind the group node, and its card lists them with when
  each was last seen. The caption of the selected node gets a rounded
  backdrop so it stays readable over edges and neighbours, and the
  force layout can be stopped with the "Freeze layout" button when the
  map is where you want it.
- Continuous ping monitoring of all hosts and switches: green/grey status
  indicator, time of the last reply. Pings follow IP ownership: a host
  whose MAC left every switch table and whose address ARP no longer
  confirms releases that address (`ip_confirm_hours`) instead of
  borrowing the liveness of whatever device holds it now.
- Only real addresses reach the map: MAC-table rows whose OID suffix
  does not match the table, along with multicast, broadcast and
  all-zero addresses, are rejected and counted per switch instead of
  becoming phantom devices. Randomized (locally administered) MACs are
  marked as such — that is what leaves a trail of one-off devices.
  `python -m moonlan.diag --fdb <switch>` shows the verdict on every
  row, `--host <ip|mac>` explains one device in a sentence.
- A new MAC waits before it becomes a device: it has to appear in
  `new_host_confirm_scans` polls (default 2), or be vouched for by ARP
  with an IP, which is what happens to every real newcomer. Addresses
  a failing cable invents live for a poll or two and now never reach
  the map, the device counter or the journal.
- Frame corruption is named: an unconfirmed, IP-less address a few bits
  away from a real one on the same port is a damaged copy of it, not a
  device. Such addresses are held off the map, listed on the port with
  a ⚠ mark, and once enough of them pile up the port raises
  `port_frame_corruption` — critical when the port's error counters
  agree. See "Frame corruption: how to read the alarm" below.
- Devices behind switches MoonLan does not poll stay on the map: a MAC
  visible only on trunk ports is drawn on the trunk with the best claim
  to it, marked "approximate" with a dashed edge, instead of
  disappearing into "Not on map" (`place_trunk_only_hosts`).
- Port traffic and error monitoring: a light counters poll (ifHC* octets
  with a 32-bit fallback, errors, discards) turns deltas into Mbit/s and
  errors/min per port. The "Ports" panel of a switch shows live rates;
  map edges show the current trunk load ("2×1 Gbit/s · ↓34 ↑12 Mbit/s",
  summed over LAG members). Counter resets after a switch reboot are
  detected and do not produce rate spikes.
- Stateful alarms: host_down (3 missed pings, only for hosts marked
  "Monitor" — the journal still records everything), switch_down
  (2 failed SNMP polls, critical), port_errors, port_discards and
  port_util (threshold plus hysteresis), port_hosts_down (critical:
  several devices of one port went silent at once — one alarm instead
  of a burst), lag_degraded (a LAG member went down), new_mac. The
  "Alarms" panel lists active and recently cleared alarms; the header
  badge shows the active count. Every raise/clear is also written to
  the journal.
- Errors and discards are told apart: damaged frames (a cable, duplex
  or transceiver fault) raise port_errors — a warning, and only when
  they are also a meaningful share of the port's frames — while
  discards, which are usually normal filtering, raise the quiet
  port_discards (info, syslog only). The "Ports" panel colours both
  columns against their thresholds and explains them in tooltips;
  `python -m moonlan.diag --port <switch> --watch N` shows the raw
  counters and the very rates the alarm engine works with.
- Honest LAG capacity: the edge label counts only active members —
  a degraded 2×1 Gbit/s aggregate shows "LACP 1×1 Gbit/s (1/2 members)",
  and the link card lists each member with its state.
- Notifications: email (SMTP), Telegram (Bot API) and Syslog (UDP) with
  per-alarm-type routing (`alarm_notify`) and an anti-spam cooldown.
  `python -m moonlan.notify --test` checks every enabled channel.
- Alarm hygiene: flap damping (an oscillating subject is muted after
  3 raises in 2 hours with a single FLAPPING notice and a FLAP mark in
  the panel), manual clear buttons, and a stale-alarm janitor that
  auto-clears alarms whose subject disappeared from the network data.
- Event journal: new MAC addresses, hosts going down and coming back,
  alarm raises/clears. Data is stored in SQLite and survives restarts.
- Two-panel web UI: device list with search (name, IP or MAC) on the left,
  interactive auto-refreshing network map on the right. English and Russian
  interface languages.
- Demo mode with a virtual network — explore the UI without real switches.
- SNMP diagnostic tool: `python -m moonlan.diag <ip>`.

## Roadmap

| Version | Functionality |
|---------|---------------|
| v0.1    | SNMP polling, MAC tables, basic topology, web UI |
| v0.2    | Manual map editing, context menus, layout export/import *(postponed)* |
| v0.3 ✓  | Ping monitoring, journal of new MAC addresses, last-reply time, host IPs and names (ARP/DNS) |
| v0.4 ✓  | Accurate link inference, LACP, VLAN, unmanaged switches |
| v0.5 ✓  | Alerts and notifications: email, Telegram, Syslog; traffic thresholds; port error counters (ifInErrors etc.) |
| v0.6    | Spanning Tree monitoring, topology change notifications |
| v0.7    | Export to PDF and Draw.io, MAC address info import |
| v0.8    | Windows computer inventory (WMI/WinRM) |

## Requirements

- Linux, Python 3.10+
- Switches with SNMP v2c enabled (read-only community)

## Installation

```bash
git clone https://github.com/neonight-d/MoonLan.git
cd MoonLan
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
# edit config.yaml: set switch addresses and the SNMP community
```

## Configuration (config.yaml)

```yaml
listen:
  host: 0.0.0.0            # web UI address and port
  port: 8080

snmp:
  community: public        # SNMP v2c community (read-only)
  timeout: 2
  retries: 1

switches:                  # IP addresses of managed switches
  - 192.168.1.2
  - 192.168.1.3

routers:                   # devices with an ARP table (routers,
  - 192.168.1.1            # L3 switches) — the source of host IPs

scan_interval_minutes: 10  # SNMP polling period (0 — manual only)
ping_interval_seconds: 60  # ping monitoring period
counters_interval_seconds: 60  # port counters polling period
db_path: moonlan.db        # SQLite file (hosts, journal, alarms)
unmanaged_threshold: 3     # more hosts than this behind a port — draw
                           # a "switch without SNMP" node (0 — disable)
monitored_by_default: false  # true = host_down alarms for every host,
                             # not only for those marked "Monitor"
host_grace_hours: 24       # how long a host stays on the map after its
                           # MAC left the switch tables
host_retention_days: 30    # then it is deleted from the database
offline_group_threshold: 2   # offline devices on one port hang off
                             # a single "Offline · N" node
ip_confirm_hours: 6          # a stale host whose IP ARP has not confirmed
                             # for this long releases the address
new_host_confirm_scans: 2    # polls a new MAC must appear in before it
                             # becomes a device (an IP from ARP is enough)
filter_suspect_macs: true    # keep damaged copies of a real address off
                             # the map (false — draw them)
place_trunk_only_hosts: true # a MAC seen only on trunks is drawn there,
                             # marked "approximate"

thresholds:
  errors_per_minute: 5           # port_errors: damaged frames only
  error_ratio_percent: 0.01      # and at least this share of all frames
  discards_per_minute: 500       # port_discards (info, syslog only)
  port_alarm_cycles: 3           # cycles before an alarm is raised/cleared
  port_utilization_percent: 90   # port_util: % of the link speed
                                 # (for a LAG — of the total speed)
  mass_down_hosts: 3             # port_hosts_down: devices of one port
                                 # gone silent in one ping cycle
  corruption_hamming_bits: 8     # how far a MAC may sit from a real one
                                 # on the same port and still be its copy
  corruption_macs_threshold: 5   # copies on one port within the flap
                                 # window -> port_frame_corruption

notifications:
  cooldown_seconds: 300      # anti-spam per (alarm type, subject)
  flap_count: 3              # this many raises of one subject within
  flap_window_seconds: 7200  # this window mute its notifications
  flap_quiet_seconds: 3600   # until it stays quiet for this long
  email:
    enabled: false
    smtp_host: smtp.example.com
    smtp_port: 587
    starttls: true
    username: ""
    password: ""
    mail_from: moonlan@example.com
    mail_to: [admin@example.com]
  telegram:
    enabled: false
    bot_token: ""            # Bot API token from @BotFather
    chat_ids: []
  syslog:
    enabled: false
    host: 127.0.0.1
    port: 514

alarm_notify:                # which alarm types go to which channels
  host_down: [email, telegram, syslog]
  switch_down: [email, telegram, syslog]
  port_errors: [syslog]
  port_discards: [syslog]
  port_util: [telegram, syslog]
  new_mac: [syslog]
```

All new sections are optional — an old config without them keeps
working (monitoring is on, notifications are off).

> **Warning.** The real `config.yaml` contains the SNMP community and
> notification credentials (SMTP password, bot token). Keep it out of
> version control — it is listed in `.gitignore`.

Check the notification channels after configuring them:

```bash
python -m moonlan.notify --test
```

It sends a test message to every enabled channel and prints a
per-channel result.

## Running

```bash
python run.py
```

Open `http://server_address:8080` in a browser.

### Running as a service (systemd)

An example unit lives in [docs/deploy/moonlan.service](docs/deploy/moonlan.service).
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

### Demo mode (no real switches)

```bash
MOONLAN_DEMO=1 python run.py
```

The service generates a virtual network — a star of five switches with
LACP, VLANs, an unmanaged switch and a couple dozen hosts. The demo
also exercises the monitoring: live traffic curves on ports, one port
with growing errors (a port_errors alarm within a couple of minutes),
a host_down that raises and clears, new devices on a rescan. Instead
of sending anything, notifications are logged as `NOTIFY (demo): …`.

### Diagnostics

```bash
python -m moonlan.diag <switch_ip> [--community public] [--timeout 2]
```

Prints everything MoonLan sees on the device via SNMP: interfaces,
bridge-port mapping, FDB distribution, LAG-MIB support, visibility of
the other configured switches. Read-only; does not touch the database.

#### Diagnosing one device

```bash
python -m moonlan.diag --host 10.0.0.51        # or a MAC address
python -m moonlan.diag --fdb 10.0.0.44 --iface 1/3
```

`--host` gathers everything MoonLan knows about one device: its stored
record with every timestamp, whether each switch has the MAC in its
table right now (port, VLAN, the raw OID of the entry) or plainly does
not, what every router's ARP says in both directions, a ping issued on
the spot, and a one-line verdict — for example, that no switch reports
the MAC, it was last seen on a port eight hours ago, and the address
still answers because ARP now maps it to a different device.

`--fdb` dumps the switch's MAC table row by row: raw OID, the length of
its suffix, the address it yields, the bridge-port and ifIndex, and
whether the row was accepted or rejected and why. It is the way to
settle whether devices on some port are real: rows whose OID suffix
does not match the table (a walk that left it, or an agent with its own
indexing) used to become phantom addresses, and they are now listed as
rejected. `--iface` narrows the listing to one port, while rejected
rows are always shown — they carry no usable port.

#### Updating the configuration

```bash
python -m moonlan.diag --config
```

Prints every setting with its effective value and whether it came from
your `config.yaml` or from a default, followed by two lists: keys the
file has that MoonLan does not know (a typo, or a setting removed in a
later version) and keys the file lacks, whose defaults now apply.
Passwords, tokens and the SNMP community are shown as `***`.

Worth running after every upgrade: a `config.yaml` written for an older
version keeps overriding settings whose meaning has changed — for
instance `errors_per_minute: 10` used to count discards as errors and
now applies to damaged frames alone, where the default is 5. The
service logs the same summary on startup, at WARNING level when the
file contains keys it does not recognise.

#### How complete is the inventory

```bash
python -m moonlan.diag --hosts
```

Polls the MAC tables of every switch and the ARP tables of every
router, then compares them with the database:

- how many MAC addresses each switch sees and how many are unique;
- how many entries each ARP source returns;
- the split into devices that are both on a port and in ARP, on a port
  only (no IP known), and **in ARP only** — those sit on no port of any
  polled switch;
- known IP addresses grouped by /24, each with the number of devices
  and how many of them are on a switch port;
- database totals: hosts, how many are missing from the current MAC
  tables (and how many of those are still inside the grace window),
  never located, without an IP, without a name.

A subnet whose devices are all "in ARP only" is behind a router (or
behind a switch MoonLan does not poll): its traffic never crosses a
polled switch port, so an L2 map cannot place those devices — MoonLan
lists them under "Not on map". Since `routers:` accepts a list, adding
every L3 device that holds an ARP table (each VLAN gateway, each
router) is what makes the inventory more complete.

#### Errors vs discards

```bash
python -m moonlan.diag --port 10.0.0.21               # noisy ports
python -m moonlan.diag --port 10.0.0.21 --iface Gi0/1 # one port
python -m moonlan.diag --port 10.0.0.21 --watch 3     # 3 measurements, 60 s apart
```

The first measurement prints the raw counters (`ifInErrors`,
`ifOutErrors`, `ifInDiscards`, `ifOutDiscards`, `ifHCInOctets`,
`ifHCOutOctets`, `ifHCInUcastPkts`, `ifHCOutUcastPkts`, `ifOperStatus`,
`ifHighSpeed`); with `--watch` every later one adds the deltas and the
rates — exactly the numbers the alarm engine and the "Ports" panel work
with. Without `--iface` only ports with a non-zero error or discard
counter are listed, worst first.

**Errors** are damaged frames: a bad cable or patch cord, a duplex
mismatch, a dying SFP or transceiver, interference on a long run. They
are genuinely rare on healthy hardware, which is why `port_errors` is a
warning at 5 per minute — and, where the switch reports packet
counters, only when the errors are more than `error_ratio_percent` of
all frames the port carries.

**Discards** are frames the switch dropped on purpose or for lack of
room: VLAN filtering (a frame arrives for a VLAN the port is not in),
storm control, ACLs, a buffer filled by a burst. Hundreds a minute on a
busy access port are normal and mean nothing is broken, so they raise
`port_discards` (info, syslog only, 500 per minute by default) and
never reach Telegram or email.

So: errors → look at the physical layer (`--watch` to see whether they
keep growing, then the cable, the port, the transceiver, the duplex
setting). Discards → look at the configuration and the traffic profile
(VLANs on the port, storm control, whether the load has outgrown the
link speed).

#### Frame corruption: how to read the alarm

```
WARNING port_frame_corruption: 10.0.0.44:1/3 — 7 distorted copies of
20:7b:d5:1a:31:8d in the MAC table (20:7b:d5:1a:31:9d, 20:7b:d5:7a:34:07,
…) — check the cable, the patch cord and the port
```

A switch learns the source address of every frame it forwards. When the
frames themselves arrive damaged — a failing cable or patch cord, a
dying transceiver, interference on a long run — the switch faithfully
learns the damaged addresses too. Next to the real `20:7b:d5:1a:31:8d`
the MAC table grows `20:7b:d5:1a:31:9d` (one bit away),
`20:7b:d5:7a:34:07`, `20:77:b5:7c:37:87`. Each of them lives for a poll
or two, never answers ARP, and used to settle on the map as a device.

MoonLan reads the pattern for what it is: an unconfirmed, IP-less
address within `corruption_hamming_bits` (8) of a confirmed one **on
the same port**. Those addresses are kept off the map, the port carries
a ⚠ mark in the "Ports" panel listing them, and once
`corruption_macs_threshold` (5) of them accumulate inside
`notifications.flap_window_seconds`, the port raises the alarm. If the
port's error counters are alarming too — or were recently — the
standing alarm is upgraded to **critical** and says so: two independent
symptoms of one physical fault.

To see the evidence:

```bash
python -m moonlan.diag --fdb 10.0.0.44            # whole MAC table
python -m moonlan.diag --fdb 10.0.0.44 --iface 1/3  # one port
```

The last section groups the corrupted addresses by port: the real MAC
each group is a distortion of, every copy with its Hamming distance and
whether anything ever gave it an IP.

What to do: replace the patch cord first, then the cable run, then move
the device to another port. If the copies stop appearing, the alarm
clears itself once the flap window passes without a new one.

Two devices with neighbouring factory MACs on one port are not a false
positive: they answer ARP, which confirms them and takes them out of
the candidate set. A device that never gets an IP and sits near another
one can be flagged — `filter_suspect_macs: false` draws such addresses
anyway, and `--fdb` always shows what was matched against what.

## How it works

1. MoonLan polls every switch from `config.yaml` via SNMP: `sysName`,
   `sysDescr`, the interface table (IF-MIB) and the MAC forwarding
   table (BRIDGE-MIB / Q-BRIDGE-MIB). FDB entries on bridge-ports
   missing from `dot1dBasePortIfIndex` (trunks on some D-Link models)
   are kept on synthetic ports instead of being dropped.
2. Physical member ports of LACP aggregates (IEEE8023-LAG-MIB) are
   mapped to the logical aggregate port and treated as one port.
3. A link between switches A and B is drawn only when it is direct:
   the ports through which A and B see each other must not both see
   any third switch (the intersection of foreign MAC sets is empty).
   This prevents false ray-to-ray links in a star, where every ray sees
   all the others through the core. A switch is recognized by any MAC
   from its full set (bridge MAC, interface MACs, management-IP MAC);
   one-way visibility is resolved by an exclusion rule.
4. MAC addresses on the remaining ports are end devices shown on the
   map; each host gets the PVID (untagged VLAN) of its port. If more
   than `unmanaged_threshold` hosts are visible behind one port, they
   are grouped under a "Switch without SNMP" node.
5. Host IPs are taken from the ARP tables of the `routers` devices
   (`ipNetToMediaPhysAddress`), names via reverse DNS.
6. All hosts with an IP and all switches are pinged regularly; status
   and last-reply time are visible in the list, on the map and in the
   device card.
7. A separate light loop polls port counters (octets, errors,
   discards) and converts deltas into per-port rates. The alarm engine
   evaluates the rules after every ping/scan/counters cycle, stores
   alarms in SQLite, mirrors transitions into the journal and routes
   notifications to email/Telegram/Syslog with a cooldown.
8. Hosts, the event journal and alarms are stored in SQLite
   (`moonlan.db`), so `first_seen` and history survive restarts.
9. The result is available through the REST API (`/api/topology`)
   and in the web UI.

## Project structure

```
MoonLan/
├── run.py                  # entry point
├── config.example.yaml     # configuration example
├── requirements.txt
├── moonlan/
│   ├── config.py           # configuration loading
│   ├── snmp_collector.py   # SNMP polling of switches (FDB, ARP, LACP, VLAN)
│   ├── topology.py         # topology inference
│   ├── counters.py         # port traffic/error counters and rates
│   ├── corruption.py       # damaged copies of a real MAC on one port
│   ├── alarms.py           # stateful alarm engine
│   ├── notify.py           # email/Telegram/Syslog notifications
│   ├── db.py               # SQLite: hosts, event journal, alarms
│   ├── pinger.py           # ping monitoring (system ping)
│   ├── diag.py             # SNMP diagnostic tool
│   ├── demo.py             # demo network generator
│   └── server.py           # FastAPI application and REST API
├── web/                    # web UI (HTML/CSS/JS, ru/en)
└── docs/
```

## API

| Method | Path              | Description |
|--------|-------------------|-------------|
| GET    | `/api/topology`   | Current topology: nodes, links (ports, LACP, current load), hosts (IP, name, ping, VLAN, `stale`), `unlocated` (known devices on no port), `pseudo_switches`, `vlan_names` |
| GET    | `/api/switch/{ip}/ports` | Port table of a switch: status, speed, PVID, LAG, In/Out Mbit/s, errors and discards per minute, known devices |
| GET    | `/api/alarms?active=1\|0&limit=50` | Active or recently cleared alarms |
| PATCH  | `/api/host/{mac}` | Set the host's monitoring flag: `{"monitored": true\|false}` |
| POST   | `/api/alarms/{id}/clear` | Manually clear one active alarm |
| POST   | `/api/scan`       | Start a new switch poll |
| GET    | `/api/search?q=…` | Search by name, IP or MAC |
| GET    | `/api/journal?limit=100` | Event journal, newest first |
| GET    | `/api/status`     | Service status and last poll time |
