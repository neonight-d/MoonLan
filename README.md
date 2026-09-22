[Русская версия → README_RU.md](README_RU.md)

# MoonLan

**MoonLan** is a web service for Linux that automatically builds the physical
topology of a local network from data collected from switches via SNMP and
displays it in a browser.

An open-source alternative to LanTopoLog. MIT license.

![Network map with port statistics](docs/img/map.png)

*Automatically discovered topology: LACP trunks, unmanaged switches, offline device groups and live port counters.*

![Alarms panel](docs/img/alarms.png)

*Alarm panel: port errors, discards and host outages with one-click access to the switch port table.*

## Features (v0.7)

- SNMP v2c polling of switches: device name, ports, speeds, statuses.
  Each switch has a time budget for its whole poll
  (`snmp.host_budget_seconds`) and may carry SNMP settings of its
  own, so one slow agent delays itself rather than the whole map —
  and is marked as late rather than reported as unreachable, which
  it is not. An OID that answers nothing but timeouts is left alone
  for a while and then tried again, out loud in the log both times.
- MAC address tables (BRIDGE-MIB and Q-BRIDGE-MIB) from every switch,
  including entries on trunk bridge-ports missing from
  `dot1dBasePortIfIndex` (e.g. D-Link LACP trunks).
- Accurate topology inference: a switch-to-switch link is drawn only when
  it is direct (FDB set-intersection criterion) — no false links between
  the rays of a star. A switch is recognized in neighbors' FDB by its full
  MAC set (bridge MAC, interface MACs, management-IP MAC), with a fallback
  exclusion rule for one-way visibility. Link cards show ports of both ends.
  Inside a branch the order is taken from LLDP first, where the two
  devices name each other, and from the MAC tables where they do not —
  a device reachable *through* a port is not the same statement as a
  device *on the cable*, and a garland of switches behind one port used
  to be drawn as a bunch hanging off it. A link the rule of "behind,
  not beside" forbids is withdrawn, with both ends, both ports and the
  reason in the journal. Where nothing can establish the order, the
  lines are dashed and say so: a guess must not look like a measured
  cable.
- Rings among polled switches are resolved or explained. A ring with a
  port the spanning tree holds in discarding is real and is drawn as
  it is. A ring with no blocked port is an error of inference, and its
  weakest link goes — a forwarding-table guess before a statement by
  one device before a statement by both. A ring nothing can account for
  keeps all its links, marked: erasing an arbitrary cable would be
  worse than admitting it cannot be explained.
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
- Honest placement: a device visible only through trunks is drawn on
  the trunk it was seen through, marked approximate, and the card says
  which trunk and why. Where a trunk carries more than a handful of
  them they are collected under one "Beyond the trunk · N" node past
  the cable instead of a row of dots on it — the node claims only what
  is known, that these addresses come through this port, and never
  that a switch is there. A device seen only on *uplinks* is not drawn
  at all — a MAC on an uplink says the device is on the far side of
  that cable, not behind the switch that reported it, so it goes to
  "Not on map" instead of being hung off the one port it cannot be
  behind.
- Stable inventory: a host whose MAC left the switch tables stays on
  the map at its last known port for `host_grace_hours` (default 24),
  greyed out and marked "last seen …", instead of blinking with every
  MAC-table timeout. Devices ARP knows but no switch port shows — a
  subnet behind a router, for instance — are listed under "Not on map",
  searchable and pingable. `python -m moonlan.diag --hosts` reports how
  complete the inventory is and which subnets are missing from it.
  A device behind a switch that ran out of its poll budget keeps its
  place on the map, drawn from the last reading that did arrive and
  labelled with when that was — but nothing about it is recorded as a
  sighting: "last seen" stops moving, and a new address is never
  confirmed by repeats of one reading of it.
- The interface says when it has stopped hearing from the service: the
  last picture stays on screen and the header says how old it is,
  instead of a map that quietly never changes again.
- A layout that does not rearrange itself. Node positions live on the
  server, not in one browser: a map of a network is a shared object,
  and two people looking at it have to see the same picture. Drag a
  node and it is pinned where it was dropped and saved, marked so the
  map can answer "did MoonLan arrange this or did I"; release it from
  its card or with a right-click. "Save layout" records where
  everything is and makes the picture reproducible. A node the saved
  layout does not cover is drawn dashed and counted in the header, so
  the drift is visible rather than found when the map is printed. A
  node that vanished from the network keeps its place — a device
  switched off for the night was not taken away — and a position is
  forgotten only when it is both older than `layout_keep_days` and has
  no node on the map. `python -m moonlan.diag --layout` reports all of
  it.
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
- LLDP (LLDP-MIB): every switch's neighbours — chassis id, port, name,
  model, management address and capabilities. Where two polled switches
  announce each other, the link and both its ports come from LLDP and
  the link card says so; every link carries a source (`lldp`, `fdb` or
  `both`), because "the two devices told us" and "we worked it out from
  the MAC tables" are not the same claim. See "LLDP" below.
- Switches nobody polls, by name: an LLDP neighbour with the `bridge`
  capability behind one of your ports becomes a named node with its
  model and every management address it announced, clickable. Where it
  is the only device behind that port it replaces the anonymous
  "switch without SNMP" and inherits its devices; where several answer
  on one port an unmanaged box is on the cable, so that node stays and
  the bridges are drawn behind it. A neighbour that announces no
  capabilities is not treated as a bridge at all. A new
  `unmanaged_bridge_detected` alarm fires when one turns up behind a
  port that is not a trunk; `known_bridges` suppresses it for the ones
  that belong there, and `uplink_ports` marks the ports that leave the
  network — a provider handover — so what is behind them is collected
  under one "External network" node and raises nothing. A device
  MoonLan polls itself — anything in `switches:` or `routers:`, by any
  of its addresses or MACs — never raises it either: a switch that
  answers every scan is not "a switch nobody polls". A port that
  looks like a way out — a public address behind it, or a neighbour
  managed from a subnet nothing else shows — says so in its card and
  in `diag --topology`, so the setting is discoverable rather than
  something to be found in the example config.
- One device, one node: a bridge whose MAC is also in the MAC table of
  its own port (they usually are) is drawn once, with its IP, its name
  and its ping state, instead of once as a named bridge and once as a
  bare MAC beside itself.
- Honest spanning-tree status: a switch with STP disabled still answers
  every `dot1dStp*` object — priority 0, cost 0, itself as the root —
  and MoonLan refuses to draw a root out of that. The "STP" panel gives
  the network verdict first (not running / fragmented into N roots /
  one tree with root X), then the raw per-switch numbers; blocking
  ports are dashed red edges labelled BLOCKING and the root bridge is
  outlined. Alarms `stp_root_changed`, `stp_topology_change` and
  `stp_fragmented` fire only for switches whose tree actually operates.
  See "STP status" below.
- Loop detection from the vendors' private MIBs: profiles keyed by
  the `sysObjectID` each model actually answers with (built in for the
  D-Link DGS-1210-26 Rev.F1, DES-1210-28/ME and DES-3526; more can be
  added in `loop_detection.profiles`), polled on the counters cycle
  rather than on the ten-minute scan. A branch counts as identified
  only when it answers with data — in SNMPv2c a missing object comes
  back inside a *successful* reply, so "the agent answered" identifies
  nothing. `loop_detected` is critical and carries the
  raw status value the agent returned; `loop_detection_disabled` notes
  a switch that stopped watching. A model that reports nothing is shown
  as reporting nothing — never as "no loops". See "Loop Detection"
  below.
- Port flapping: `ifLastChange` is read alongside `ifOperStatus`, so a
  link that bounces several times between two counter polls is counted
  rather than missed. `port_flapping` fires at
  `thresholds.flaps_per_window` transitions inside
  `thresholds.flap_window_minutes`, and the ports panel marks the port
  with the count and the time of the last transition.
- Port traffic and error monitoring: a light counters poll (ifHC* octets
  with a per-direction 32-bit fallback, errors, discards) turns deltas
  into Mbit/s and errors/min per port. A counter the switch did not
  answer for reads "—", never 0.0, and raises no alarm either way. A
  walk that stops partway through a table is resumed from where it
  stopped, and the ports it still missed are fetched one at a time. A
  switch whose poll fails costs only its own data: the others are
  collected and shown regardless. A measured rate is never thrown away
  for being old: it is shown dimmed with its age, and only past
  `stale_rate_hide_minutes` does the cell empty — "—" means the port
  has never been measured, which is a different fact and a different
  investigation. The "Ports" panel of a switch shows live rates;
  map edges show the current trunk load ("2×1 Gbit/s · ↓34 ↑12 Mbit/s",
  summed over LAG members). Counter resets after a switch reboot are
  detected and do not produce rate spikes.
- Stateful alarms: host_down (3 missed pings, only for hosts marked
  "Monitor" — the journal still records everything), switch_down
  (2 failed SNMP polls, critical), switch_stale (a switch that answers
  but has not finished a full poll for several scans — its data has
  stopped being refreshed, which is neither up nor down), port_errors,
  port_discards and
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
- A failed poll says so: if the topology build raises, the previous map
  stays on screen and the header turns red with "Last scan failed" and
  the error text, instead of the empty "no data yet" that a service
  which has only just started shows.
- Event journal: new MAC addresses, hosts going down and coming back,
  alarm raises/clears. Data is stored in SQLite and survives restarts.
- Two-panel web UI: device list with search (name, IP or MAC) on the left,
  interactive auto-refreshing network map on the right. English and Russian
  interface languages.
- Demo mode with a virtual network — explore the UI without real switches.
- SNMP diagnostic tool: `python -m moonlan.diag <ip>`.

Version history: [CHANGELOG.md](CHANGELOG.md)
([русская версия](CHANGELOG_RU.md)).

## Roadmap

| Version | Functionality |
|---------|---------------|
| v0.1    | SNMP polling, MAC tables, basic topology, web UI |
| v0.2 ~  | Manual map editing, context menus, layout export/import *(layout and manual placement done in v0.7; manual LINKS deliberately not)* |
| v0.3 ✓  | Ping monitoring, journal of new MAC addresses, last-reply time, host IPs and names (ARP/DNS) |
| v0.4 ✓  | Accurate link inference, LACP, VLAN, unmanaged switches |
| v0.5 ✓  | Alerts and notifications: email, Telegram, Syslog; traffic thresholds; port error counters (ifInErrors etc.) |
| v0.6 ✓  | LLDP neighbours and link verification, unmanaged bridge detection, honest STP status, port flapping |
| v0.6.1 ✓| Fixes from the production network: multi-bridge ports, capability-less neighbours, LLDP port matching |
| v0.6.2 ✓| One node per device, LLDP names on the map, external uplink ports, a usable ports panel |
| v0.6.3 ✓| Readable links, honest external-network demo, safe development against a live service |
| v0.6.4 ✓| Honest counters, remembered offline locations, network edge and hint fixes |
| v0.6.5 ✓| Resuming a truncated walk, honest partial answers, per-port counter fallback |
| v0.6.6 ✓| One bad OID no longer stops the counters; offline groups behind their bridge |
| v0.6.7 ✓| Loop Detection from the vendors' private MIBs, honest unsupported state |
| v0.6.8 ✓| Identify the model before reading it; diagnostics that can be shared |
| v0.6.9 ✓| A device seen on an uplink is not behind it; host placement under test |
| v0.6.10 ✓| Devices seen through a trunk are grouped beyond it, not on it |
| v0.6.11 ✓| One root is one root; a panel header stays put |
| v0.6.12 ✓| A slow agent delays itself, not the whole map; per-switch SNMP settings |
| v0.6.13 ✓| A dash means never measured; an unknown root is not a root |
| v0.6.14 ✓| LLDP builds the tree; rings are resolved or explained |
| v0.6.15 ✓| A reading nobody took is not an observation; the map says when it stopped |
| v0.7 ✓  | A layout that does not rearrange itself: positions on the server, manual placement, drift made visible |
| v0.7.1  | Export to PDF and Draw.io, MAC address info import |
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
  timeout: 5               # seconds to wait for a reply
  retries: 2               # re-sends of a single request
  retries_on_break: 2      # times a walk that stops mid-table is
                           # picked back up from where it stopped
  host_budget_seconds: 120 # the whole poll of one switch, start to
                           # finish. `timeout` bounds one request;
                           # this bounds the sum of them
  dead_oid_strikes: 3      # walks with no rows AND a timeout, in a
                           # row, after which that OID is left alone
                           # on that host (0 — never)
  dead_oid_cooldown_scans: 30  # for this many scans, then tried again

switches:                  # IP addresses of managed switches. An entry
  - 192.168.1.2            # is an address, or a mapping with `ip:` and
  - ip: 192.168.1.3        # any snmp: key, which then applies to that
    timeout: 2             # switch alone; everything else is inherited
    retries: 1
    host_budget_seconds: 90

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
known_bridges: []            # bridges that are supposed to be behind an
                             # access port (chassis id or management IP):
                             # found and named, but never alarmed on.
                             # Only for devices MoonLan does NOT poll —
                             # anything in switches: or routers: is
                             # already known and needs no entry here
uplink_ports: []             # ports that leave the network, as
                             # "<switch ip>:<port name>": what is behind
                             # them is one "External network" node and
                             # raises no bridge alarms. A port that
                             # looks like one says so in its card.

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
  stp_changes_per_cycle: 3       # stp_topology_change: topology changes
                                 # per scan on an operating switch
  flaps_per_window: 4            # port_flapping: link state transitions…
  flap_window_minutes: 10        # …inside this window

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
  unmanaged_bridge_detected: [telegram, syslog]
  stp_root_changed: [telegram, syslog]
  stp_topology_change: [telegram, syslog]
  stp_fragmented: [telegram, syslog]
  port_flapping: [telegram, syslog]
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

### When one switch is not like the others

A network is never made of one kind of hardware, and the settings
above are one compromise for all of it. Four keys exist for the device
that does not fit.

**`snmp.host_budget_seconds`** (default 120) bounds the whole poll of
one switch, start to finish; `timeout` bounds one request. A poll is a
dozen walks of a dozen requests each, so an agent that answers
everything slowly stays inside every single timeout and still takes
eight minutes — and the scan waits for the last switch. A switch that
runs past its budget is left out of that scan; the rest of the network
gets its map on time.

It is **not** reported as unreachable, because it is not: it answers,
only too slowly. No `switch_down` is raised for it and none is cleared.
Its last complete reading stays on the map, its card says when that
reading was taken, and the header says how many switches ran out of
time.

**Per-switch settings.** An entry in `switches:` may be a mapping with
`ip:` and any of `community`, `timeout`, `retries`,
`retries_on_break`, `host_budget_seconds`. Those apply to that switch
alone; anything not written there is inherited from the `snmp:`
section. The old plain list of addresses keeps working exactly as it
did.

Reach for this when one device is unlike the rest. A box that answers
slowly usually wants a *shorter* timeout and one retry, not a longer
one: the requests that cost the time are the ones it will never answer,
and the sooner they are given up on the better. A corner of the network
set up years apart from the rest may want its own community.

```yaml
switches:
  - 192.168.1.2                  # as before: everything from snmp:
  - ip: 192.168.1.3
    timeout: 2                   # a slow box: better to give up fast
    retries: 1
    host_budget_seconds: 90
  - ip: 192.168.1.4
    community: OtherString
```

`python -m moonlan.diag --config` prints the settings every switch is
actually polled with and marks the ones it was given of its own.

**`snmp.dead_oid_strikes`** (default 3) and
**`snmp.dead_oid_cooldown_scans`** (default 30). An agent that does
not implement a table is supposed to answer `noSuchObject`, which
costs one round trip. Some go quiet instead, and the walk pays the
whole retry budget to learn nothing — every cycle, forever. After
`dead_oid_strikes` walks in a row that returned no rows **and** ended
in a timeout, MoonLan stops asking that host for that OID for
`dead_oid_cooldown_scans` scans, then tries again: firmware gets
updated.

Only that one outcome counts. A partial answer is what
`retries_on_break` is for, and an honest `noSuchObject` is cheap to
keep asking for. Both the pause and the resumption are logged, and the
skipped walk reports "no answer" rather than an empty table — data
missing because MoonLan stopped asking must never be mistaken for data
the device denies having. Set `dead_oid_strikes: 0` to switch the rule
off.

```bash
python -m moonlan.diag --skipped
```

asks the running service what is on pause right now and for how many
more scans.

### A dash, a stale value and a switch that stopped being read

Three settings exist because three different absences used to look
alike.

**`stale_rate_hide_minutes`** (default 30). A measured port rate is
never thrown away for being old. Until v0.6.13 anything older than
three counters intervals was dropped from the answer and the panel drew
"—" — the same "—" it draws for a counter the agent does not implement.
Those are opposite diagnoses: one says "this switch has no such
counter, stop looking", the other says "nobody has measured this
lately". Now a rate older than three intervals is dimmed and says when
it was taken; past this many minutes the cell empties, because a
half-hour-old speed is a memory — and even then hovering it says when
the port was last measured. A bare "—" with nothing behind it means the
port has never been measured at all.

**`stale_switch_scans`** (default 5). A switch may answer and never
finish answering: its poll budget runs out every scan, its data stops
being refreshed, and on the map it goes on looking alive from its last
complete reading. One deployment had a switch that was not read in full
once in six hours — thirty scans, thirty budget failures — and nothing
said so outside the journal. After this many consecutive scans its card
counts them and dates the reading, the switch list says the same on
hover, and a `switch_stale` alarm is raised (warning, syslog by
default). Never `switch_down`: sending somebody to look for a dead
device that is answering wastes the trip. It clears the moment one full
poll finishes.

The usual cure is not a bigger budget but a shorter timeout for that
device — see the per-switch settings above. `diag --config` prints how
long each switch's last complete poll actually took, which is the
number to set a budget from.

**The counters cycle and the poll budget.** A scan holds a switch for
as long as its budget allows, and a counters cycle that finds it held
waits briefly and then skips it. So a `host_budget_seconds` at or above
twice `counters_interval_seconds` means that switch misses a cycle
after every scan and its rates visibly age. MoonLan says so at startup
and in `diag --config`, per device. It is not forbidden — a genuinely
slow agent may need the budget — but it should be a decision rather
than a surprise.

**`layout_keep_days`** (default 90). How long a saved node position
outlives the node itself. A device switched off for the night was not
taken away: coming back, it belongs where it was, so nothing is
forgotten for being absent. A position goes only when it is BOTH older
than this and has no node on the current map — and that housekeeping
runs once, at the first scan after a restart, because before that scan
there is no map to compare against and every position would look
orphaned.

### When SNMP says nothing at all

SNMPv2c does not answer a wrong community string. Not with an error —
it does not answer. From outside, that silence is shaped exactly like
an agent that does not implement the object you asked for, and MoonLan
used to say precisely that.

So before deciding, it asks: `sysDescr`, which every agent must
implement, and a ping.

- the host answers ping and says nothing to `sysDescr` → the community
  string or a disabled agent, and that is named first;
- silent on both → not reachable from here at all, a network question
  before it is an SNMP one;
- `sysDescr` answers and the OID you asked about does not → the old
  verdict, which is true here and can be said firmly.

The same sentence goes into the service log when a switch stops
answering. And when every configured switch goes silent at once, that
is reported as one fact rather than N: one common cause is likelier
than N simultaneous faults, and `snmp.community` is the first place to
look.

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

#### A second instance beside a running service

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

The last table it prints is the SNMP settings every switch is actually
polled with, marking the ones that switch was given of its own rather
than inheriting from `snmp:`.

#### What is not being asked for

```bash
python -m moonlan.diag --skipped
```

Lists the (host, OID) pairs MoonLan has stopped polling because they
answered nothing but timeouts, and how many scans are left before each
is tried again — see `dead_oid_strikes` above. The pause lives in the
running service, so this asks it over the API rather than guessing; it
needs MoonLan to be up, at the `listen.host` / `listen.port` from the
same `config.yaml`.

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
python -m moonlan.diag --port 10.0.0.21               # every active port
python -m moonlan.diag --port 10.0.0.21 --iface Gi0/1 # one port
python -m moonlan.diag --port 10.0.0.21 --watch 3     # 3 measurements, 60 s apart
```

#### "—" is not zero

A dash in the ports panel means the switch returned nothing for that
counter. A zero means it answered, and the answer was zero. The two
look alike and mean opposite things, so they are never conflated:
an unknown value raises no alarm and clears none, and the column
header carries a ⚠ naming the OID that went unanswered.

This is not hypothetical. The D-Link DGS-1210 Rev.F1 implements
`ifHCInOctets` and returns nothing at all for `ifHCOutOctets` — MoonLan
falls back to the 32-bit `ifOutOctets` for that direction alone, which
is why each counter column is walked and judged on its own. A walk that
fails logs one WARNING per OID per cycle with the error text, and
`python -m moonlan.diag --port <switch>` opens with a per-column report:
how many rows each returned, or that it returned none and why.

If a switch's own web interface shows errors where SNMP reports zeros,
that is a hole in its firmware rather than a healthy port — the numbers
above tell you which of the two you are looking at. The same goes for
the packet counters: a column that answers for every port with zero
while the octet counters are in the terabytes is implemented and never
written to, and MoonLan reads the 32-bit counters instead so the error
ratio has a frame count to work with.

**Before concluding that a counter is missing, raise the timeout.** A
timeout set too low does not show up later as a timeout — it shows up
as a switch that returns nothing, which is indistinguishable from one
that does not implement the OID. `snmp.timeout: 2` with one retry gave
empty and truncated tables on this network, and produced exactly that
wrong conclusion about a DGS-1210 that answers `ifHCOutOctets`
perfectly well at five seconds. The defaults are 5 and 2 for that
reason.

A switch with many interfaces may also stop answering partway through
a table — the ports that vanish are the ones at the end of it. Such a
walk is picked back up from the last OID that did arrive
(`snmp.retries_on_break`), and any ports still missing afterwards are
fetched one at a time from the 32-bit counter. A column that ended
short says so rather than reporting silence:

```
in_octets  1.3.6.1.2.1.31.1.1.1.6  24 row(s), then stopped answering
                                   at 1.3.6.1.2.1.31.1.1.1.6.24 (…),
                                   4 port(s) filled in from 1.3.6.1.2.1.2.2.1.10
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

### LLDP

```bash
python -m moonlan.diag --topology   # neighbours, sources, mismatches
```

MAC tables say which addresses are reachable through a port. They never
say what the device on the other end is. LLDP says both, which is why
v0.6 reads it:

- **Links become statements.** Where two polled switches announce each
  other, the link and the ports on both ends come from LLDP, not from
  inference. Every link carries `source`: `lldp`, `fdb` or `both`, and
  the link card names it. This matters most in networks with one-way
  visibility, where an access switch never sees the core in its MAC
  table and the uplink port could only ever be a guess.
- **Bridges get names.** A neighbour with the `bridge` capability whose
  chassis id belongs to no polled switch is a switch nobody polls. It
  becomes a node with its name, model and management address instead of
  an anonymous "switch without SNMP" — or instead of nothing at all,
  where too few devices sat behind it to trip `unmanaged_threshold`.

Several cautions are built in, all of them learned the hard way:

**Only a device that says "bridge" is one.** A neighbour that
announces `router`, `wlanAccessPoint` or `telephone` has identified
itself perfectly well; it is named as what it says it is, in its card
and in the ports panel, and it gets no bridge node — it is already on
the map as a host, and now with its own name on it. Devices with the
router capability are drawn as diamonds so infrastructure stands out
from a cloud of workstation dots.

**A missing capability proves nothing — and buys nothing.** LLDP's
System Name, Description and Capabilities are optional TLVs, and some
devices ship with them disabled (D-Link DES-3526 does). A neighbour
that sends none of them is shown as an unidentified LLDP device: it
appears on its port, and on its own host card when it is a device the
map already draws, but it gets **no bridge node and raises no alarm**.
The absence of the `bridge` flag is not evidence that the device is a
switch either — on this network it is a hundred and thirty IP cameras
and desk phones, and v0.6.0 drew every one of them as a bridge.

The exception is about the switch, not the neighbour: an agent that
fills `lldpRemSysCapEnabled` for nobody at all (the HPE 1820) says
nothing about any particular neighbour by leaving it empty. There a
device announcing both a system name and a management address is taken
as a bridge and marked as such in its card — and its alarm is raised
at severity **info**, because that is an inference rather than the
device's own claim.

**Port labels, where they are labels.** `lldpLocPortDesc` holds what an
operator typed into the switch on some agents ("Library", "403 audit")
and a copy of `ifDescr` on others. A value that repeats the port's own
ifName or ifDescr, or that is one firmware template with the port
number substituted, is dropped; what survives is shown in italics next
to the port with a tooltip saying it was set on the switch. Those
labels are somebody else's data and may be years out of date.

**LLDP frame forwarding.** Some switches can be told to re-transmit
foreign LLDP frames (`LLDP Forward Message` on D-Link DES-1210), and
the neighbour then shows up on a port it is not attached to. Two things
give that away: the same chassis on more than one **logical** port
(both members of a LACP bundle are one port, not two), or a neighbour
whose MAC sits in the switch's own forwarding table behind a different
port. Such a port is marked in the ports panel and none of its data is
used to draw links. If you see the mark, turn the setting off: it is
not doing anything useful for you either.

**Several devices behind one port is not forwarding.** It is an
unmanaged switch on the cable with several talkers behind it — the
normal shape of an access port. All of them are found and shown, and
the bridges among them get their nodes; what cannot be done is drawing
a link, because which of them is on the cable is not knowable. The
port card says so.

**Which port a neighbour is really on.** `lldpRemLocalPortNum` is not
an ifIndex, and `lldpLocPortId` is not always usable: the HPE 1820
answers it with one system MAC on all 26 ports, which put all seven of
its neighbours on port 1. MoonLan resolves the local port in order of
evidence — the local port table, then the switch's own MAC table, then
the port number read as an ifIndex — throws away any key that points
at more than one port, and records which of the three it used
(`port_matched_by`, printed by `diag --topology`). A link resting on
the weakest of them does not override the MAC tables.

Where LLDP and the MAC tables disagree, LLDP wins and the disagreement
is recorded: `diag --topology` prints an "LLDP vs FDB mismatches"
section, and the same lines go to the log at INFO. Nothing is quietly
"fixed".

### STP status

```bash
python -m moonlan.diag --stp        # raw dot1dStp* next to the verdict
```

MoonLan may tell you STP is not running on a switch that other tools
show as a root bridge with a priority and a cost. It is not being
coy — the other tools are reading placeholders.

A switch with spanning tree **disabled** still answers every
`dot1dStp*` object. It reports priority 0, root cost 0 and, most
misleadingly, itself as the designated root. Read at face value, five
such switches become five root bridges of five trees. That is exactly
the false diagnosis this project started from.

A switch is taken as part of a tree on the first of these that holds:

1. **it accepted somebody else's root** — the designated root is not
   one of its own addresses and the cost to it is above zero. Only a
   bridge that processes BPDUs answers that way; one with its tree off
   names itself, at cost zero, always;
2. **a neighbour confirms it as the root** — another polled switch,
   already known to be operating by (1), follows this switch's own
   address. "I am the root" reads identically from the real root and
   from a switch with STP disabled, and from inside one device they
   cannot be told apart. From outside they can, and MoonLan polls the
   whole network;
3. **the tree demonstrably converged**, which is the historical test
   below. It is last because history is what a tree switched on an
   hour ago does not have: `dot1dStpTimeSinceTopologyChange` equals
   the uptime and `dot1dStpTopChanges` is zero, so a freshly converged
   tree used to read as one that never converged.

`diag --stp` prints which of the three decided, and so does the
tooltip on the state cell in the panel. For test 3, root, cost and
root port are used only for a switch that passes all three of:

1. at least one port has `dot1dStpPortEnable` = enabled(1);
2. at least one port is in a state other than disabled(1);
3. the tree demonstrably converged at some point — either
   `dot1dStpTopChanges` > 0, or `dot1dStpTimeSinceTopologyChange` is
   meaningfully younger than `sysUpTime` (a change happened after
   boot).

Anything else reads **not operating**, and the reported root, cost and
root port are shown as dashes rather than as the zeros the switch
offers. The reason is kept and printed by `diag --stp`, so the verdict
can be checked rather than believed.

The network verdict on top of the panel is one of three:

- **STP is not running in this network** — no switch passes the tests;
- **one spanning tree, root X** — every operating switch agrees;
- **fragmented: N roots** — operating switches follow different roots,
  which happens when segments are isolated from each other's BPDUs.
  Held for two scans before `stp_fragmented` is raised: a converging
  tree passes through disagreement on its way to agreement.

Fragmentation is **not necessarily a fault**. BPDUs are untagged and
are handled in the VLAN of the port they arrive on, so two segments
whose trunk ports sit in different VLANs form two trees entirely by
design and never meet — which is exactly what was happening on the
network this was written for, where trunk ports 27 and 28 were in
VLAN 500 and VLAN 600 and in no VLAN each other could reach. The
panel lists each root with the VLANs of its followers' trunk ports
beside it, and the alarm text says the same. Check the VLAN
membership of the trunks before treating it as damage.

Roots are compared by MAC, never by the text of the Bridge ID: see
"What BRIDGE-MIB really says" below for why two switches can spell
one root two different ways.

A partial tree is a normal state here, not an error. Bridges that
answer no SNMP (the MikroTik boxes behind the access ports) take part
in spanning tree without appearing in any of these numbers, and the map
says so through the LLDP bridge nodes instead.

`stp_root_changed`, `stp_topology_change` and `stp_fragmented` are
raised for operating switches only. A switch that stops operating drops
its remembered root, so coming back does not read as a root change.

#### What BRIDGE-MIB really says

Four things on this hardware mean something other than what they look
like. All four were found the hard way; they are written down so the
next person adding a switch does not find them again.

**The priority in the Bridge ID can be in the wrong byte.** A Bridge
ID is eight bytes: two of identifier, six of MAC. 802.1t splits the
first two into a 4-bit priority (hence always a multiple of 4096) and
a 12-bit system id extension. An Edge-Core ES3528M answers
`00 10 34 0a 33 bc ca f0` where an HPE 1820 answers
`10 00 34 0a 33 bc ca f0` for the same root — the priority is in the
low byte, and read honestly that is 16 rather than 4096. MoonLan
detects the shifted encoding, corrects it, logs a warning naming the
host, and marks the value in the panel. It does not correct it
silently: the number will differ from the one the switch's own web
interface prints, and the operator has to know why.

**`dot1dStpProtocolSpecification` is not the protocol version.**
RouterOS answers 3 (`ieee8021d`) with RSTP running, and it is not
alone. MoonLan reads the version from `dot1dStpVersion`
(`1.3.6.1.2.1.17.2.16.0`) and prints the specification only in
`diag --stp`, with a note beside it.

**`dot1dStpPortState = 2` (blocking) does not mean a blocked link.**
RouterOS reports blocking on ports with nothing plugged into them —
`ether3` and `ether4` of a four-port box. A port is treated as
blocking only when `ifOperStatus` for it is up; otherwise it is dark,
and there is no edge on the map to block.

**A switch can run a spanning tree and report none of it.** Four
D-Links on this network run RSTP — their CLI names the root, the root
port and the cost — and answer every BRIDGE-MIB object with a zero:
designated root `00 00 00 00 00 00 00 00`, priority 0, no topology
changes, and either an empty `dot1dStpPortTable` or one where every
port is disabled. MoonLan says exactly that rather than calling the
tree broken, and the root among them is still identified, because its
neighbours name it.

Read correctly on all of them: `dot1dBaseBridgeAddress`,
`dot1dBasePortIfIndex`, and the per-port `dot1dStpPortDesignatedRoot`
/ `DesignatedCost` / `DesignatedPort`. `dot1dTpFdbPort = 0` means "no
port" and is dropped, so nothing binds to a port zero that does not
exist.

### Sharing a diagnostic report

```bash
python -m moonlan.diag --topology --anonymize
```

Every `diag` report is a map of your network: addresses, MAC tables,
host names, the port labels somebody typed into the switches. That is
exactly what makes it useful in a bug report and exactly what you do
not want published — this project learned that by publishing nine of
them in its own history.

`--anonymize` works with any of the reports. It rewrites the output
through a substitution table that is **stable for the run**: one
address always becomes the same replacement, so the report still reads
as a report — the same switch is the same switch on every line, and a
MAC seen on two ports is still one device. Replacements come from the
documentation ranges so nobody mistakes one for a real device:
`198.51.100.0/24` (RFC 5737) for addresses, `00:00:5e:00:53:xx`
(RFC 7042) for MACs, `switch-N` and `host-NN` for names.

It covers every address and MAC in the text — including the ones
hiding inside an OID, where an ARP row carries an address and a MAC
table row carries a MAC as six decimal octets — and every name the
tool itself learned: switch `sysName`s, LLDP neighbour names, host
names from the database. What it cannot cover is a bare word somebody
typed into a port description, because nothing marks it as a name. The
output is short; skim it before you attach it.

`docs/diag_example/` holds one anonymised sample of each report,
generated this way and not edited afterwards.

### Loop Detection

```bash
python -m moonlan.diag --loop     # raw values, matched profile, verdict
```

There is no standard MIB for loopback detection. Every vendor keeps it
in its own branch under `1.3.6.1.4.1.<enterprise>`, with its own
structure and its own enumerations, so MoonLan reads it through
*profiles*: a profile says which `sysObjectID` a model answers with,
where its branch starts, the suffixes of the four scalars and the three
per-port columns, and which raw status value means "no loop".

Built in:

| Profile | Model | sysObjectID | Branch |
|---------|-------|-------------|--------|
| `dlink-1210` | DGS-1210-26 Rev.F1 | `…171.10.153.6.1` | `…171.11.153.1000.17` |
| `dlink-1210` | DES-1210-28/ME | `…171.10.75.15.2` | `…171.10.75.15.2.17` |
| `dlink-des3526` | DES-3526 | `…171.10.64.1` | `…171.11.64.1.2.12` |

The two 1210 models share one profile: different roots, identical
structure below them.

Note the third column against the fourth. A product's private branch
is **not** under its own sysObjectID — two of these three cross from
the `.10` subtree into `.11`, and they do it by different arithmetic.
The first version of this table assumed the obvious rule and got two
of four D-Links wrong. There is nothing to derive here; a new model is
added by walking it.

**HPE OfficeConnect 1820 reports nothing.** A full walk of
`1.3.6.1.4.1.11` on one returns versions, serial numbers and PoE — no
detection interval, no per-port loop state. Loop Protection runs on the
device and is simply not exposed over SNMP. That is a property of the
model, not a polling failure, and MoonLan says so: its card reads
"the model does not report it over SNMP", the Loop column is dashes,
and no alarm is raised. What it never says is "no loops" — an answer
nobody gave is not an answer.

#### The rule: normal is known, everything else is a loop

Only the "no loop" value has ever been observed on this hardware: `1`
on the 1210 family, the string `None` on the DES-3526. What these
agents report *during* a real loop was never seen, and nobody is going
to short two ports of a working network to find out. So the rule is
inverted from the obvious one: a status that is not the known-normal
value raises `loop_detected`, and the raw value goes into the alarm
text. The first real loop then documents itself instead of passing in
silence.

A port whose status did not arrive is **unknown**, not normal: the Loop
column shows "no data" and nothing is raised for it in either
direction. A port with LBD switched off on the switch shows a dash.

#### Alarms

- `loop_detected` (**critical**, subject `<switch ip>:<port>`): the
  switch reports a loop. The message carries the switch name, the port,
  the raw status value, when it started and the configured recovery
  time. Cleared when the status returns to normal — the switch releases
  the port itself — with how long the loop held.
- `loop_detection_disabled` (**info**, syslog by default): a switch that
  answers the branch reports loop detection switched off globally. With
  no spanning tree running, this is the only loop protection there is,
  and its silent disappearance is worth a line.

On the map a port with a loop gets a red edge labelled LOOP and its
switch is outlined in red, the way an unreachable switch is.

#### Adding a model

`diag --loop` ends with the switches no profile covers and the
`sysObjectID` of each — that is the key a new profile needs:

```
Switches with no loop-detection profile — each line is what a
new profile in config.yaml needs to be keyed by:
  10.3.6.5        ES3528M                      sysObjectID 1.3.6.1.4.1.259.6.10.94
```

Walk the vendor's branch to find the objects:

```bash
python -m moonlan.diag --walk 10.3.6.5 1.3.6.1.4.1.259 --limit 800
```

then describe what you found in `loop_detection.profiles` in
`config.yaml` — the commented example there is the full shape. A
profile whose name repeats a built-in one replaces it, so a firmware
that moved its branch can be corrected without touching the code.

Profiles are matched by `sysObjectID` first. A switch whose
`sysObjectID` matches nothing is still probed against every known
branch — but a probe only counts when the branch answers with **data**:
the global scalar has to parse as one of the two values its enumeration
allows, and the port table has to return at least one row.

That bar exists because the obvious one does not work. In SNMPv2c an
agent answers a request for an object it does not implement with a
*successful* reply carrying `noSuchObject` — no error status, no error
indication. A probe that accepts "it replied" therefore succeeds on
every device that speaks SNMP at all, and v0.6.7 shipped exactly that:
an HPE, an Edge-Core and a DES-3526 were all claimed by the first
D-Link branch in the list and then reported as having loop protection
switched off. They had not been read at all.

`diag --loop` prints which of the two ways matched. `matched by probe`
on a model that is already in the table is the signal that the table
has drifted from the hardware again.

### Walking an arbitrary MIB

```bash
python -m moonlan.diag --walk 10.0.0.10 1.3.6.1.4.1.171 --limit 200
```

Dumps any OID subtree: raw OID, SNMP type, the value as text and, where
it is not text, as hex. It exists for exploring private MIBs before
writing anything against them — D-Link lives under `1.3.6.1.4.1.171`
and HPE under `1.3.6.1.4.1.11` — and it is how the loop-detection
profiles above were found. It is also the first step in adding a new
one: see "Loop Detection". The default 500-row ceiling keeps a stray
subtree from being downloaded whole.

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
8. LLDP is polled in the same cycle. Neighbours are resolved against
   the local port table (`lldpRemLocalPortNum` is not an ifIndex), ports
   carrying forwarded LLDP frames are excluded, links both devices
   announce override the inference, and neighbours with the `bridge`
   capability that belong to no polled switch become named nodes.
   BRIDGE-MIB `dot1dStp*` is read alongside and judged before use.
9. Hosts, the event journal and alarms are stored in SQLite
   (`moonlan.db`), so `first_seen` and history survive restarts.
10. The result is available through the REST API (`/api/topology`)
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
│   ├── lldp.py             # LLDP neighbours, capabilities, forwarding guard
│   ├── stp.py              # BRIDGE-MIB dot1dStp* and the "is it running" verdict
│   ├── loopdetect.py       # loop detection profiles for the vendors' private MIBs
│   ├── anonymize.py        # rewriting a diag report so it can be shared
│   ├── alarms.py           # stateful alarm engine
│   ├── notify.py           # email/Telegram/Syslog notifications
│   ├── db.py               # SQLite: hosts, event journal, alarms
│   ├── pinger.py           # ping monitoring (system ping)
│   ├── diag.py             # SNMP diagnostic tool
│   ├── demo.py             # demo network generator
│   └── server.py           # FastAPI application and REST API
├── web/                    # web UI (HTML/CSS/JS, ru/en)
├── tests/                  # python -m unittest discover -s tests
└── docs/
    └── diag_example/       # anonymised sample of each diag report
```

## API

| Method | Path              | Description |
|--------|-------------------|-------------|
| GET    | `/api/topology`   | Current topology: nodes, links (ports, LACP, current load), hosts (IP, name, ping, VLAN, `stale`), `unlocated` (known devices on no port), `pseudo_switches`, `bridges` (switches found by LLDP that nobody polls), `vlan_names`; every switch carries its `loop` state |
| GET    | `/api/switch/{ip}/ports` | Port table of a switch: status, speed, PVID, LAG, In/Out Mbit/s, errors and discards per minute, known devices, LLDP neighbours, link flaps, per-port loop state and the switch's `loop_detection` summary |
| GET    | `/api/stp`        | Spanning tree per switch plus the verdict for the network |
| GET    | `/api/alarms?active=1\|0&limit=50` | Active or recently cleared alarms |
| PATCH  | `/api/host/{mac}` | Set the host's monitoring flag: `{"monitored": true\|false}` |
| POST   | `/api/alarms/{id}/clear` | Manually clear one active alarm |
| POST   | `/api/scan`       | Start a new switch poll |
| GET    | `/api/search?q=…` | Search by name, IP or MAC |
| GET    | `/api/journal?limit=100` | Event journal, newest first |
| GET    | `/api/status`     | Service status and last poll time |

## Acknowledgments

AI-Assisted Development: Built with [Claude Code](https://github.com/anthropics/claude-code) by [Anthropic](https://www.anthropic.com/)
