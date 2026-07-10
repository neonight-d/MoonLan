[Читать по-русски → README_RU.md](README_RU.md)

# MoonLan

**MoonLan** is a web service for Linux that automatically builds the physical
topology of a local network from data collected from switches via SNMP and
displays it in a browser.

An open-source alternative to LanTopoLog. MIT license.

## Features (v0.5)

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
  host names via reverse DNS.
- Continuous ping monitoring of all hosts and switches: green/grey status
  indicator, time of the last reply.
- Port traffic and error monitoring: a light counters poll (ifHC* octets
  with a 32-bit fallback, errors, discards) turns deltas into Mbit/s and
  errors/min per port. The "Ports" panel of a switch shows live rates;
  map edges show the current trunk load ("2×1 Gbit/s · ↓34 ↑12 Mbit/s",
  summed over LAG members). Counter resets after a switch reboot are
  detected and do not produce rate spikes.
- Stateful alarms: host_down (3 missed pings, only for hosts marked
  "Monitor" — the journal still records everything), switch_down
  (2 failed SNMP polls, critical), port_errors and port_util (threshold
  with two-cycle hysteresis), port_hosts_down (critical: several hosts
  of one port went silent at once — one alarm instead of a burst),
  lag_degraded (a LAG member went down), new_mac. The "Alarms" panel
  lists active and recently cleared alarms; the header badge shows the
  active count. Every raise/clear is also written to the journal.
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

thresholds:
  errors_per_minute: 10          # port_errors alarm threshold
  port_utilization_percent: 90   # port_util: % of the link speed
                                 # (for a LAG — of the total speed)
  mass_down_hosts: 3             # port_hosts_down: hosts of one port
                                 # gone silent in one ping cycle

notifications:
  cooldown_seconds: 300      # anti-spam per (alarm type, subject)
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
| GET    | `/api/topology`   | Current topology: nodes, links (ports, LACP, current load), hosts (IP, name, ping, VLAN), `pseudo_switches`, `vlan_names` |
| GET    | `/api/switch/{ip}/ports` | Port table of a switch: status, speed, PVID, LAG, In/Out Mbit/s, errors and discards per minute, known devices |
| GET    | `/api/alarms?active=1\|0&limit=50` | Active or recently cleared alarms |
| PATCH  | `/api/host/{mac}` | Set the host's monitoring flag: `{"monitored": true\|false}` |
| POST   | `/api/alarms/{id}/clear` | Manually clear one active alarm |
| POST   | `/api/scan`       | Start a new switch poll |
| GET    | `/api/search?q=…` | Search by name, IP or MAC |
| GET    | `/api/journal?limit=100` | Event journal, newest first |
| GET    | `/api/status`     | Service status and last poll time |
