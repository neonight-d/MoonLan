# What MoonLan does

Everything MoonLan does, as of v0.7.5 — the list the README used to carry. The README keeps the summary; each item here links to the page that explains it.

![Alarms panel](img/alarms.png)

*Alarm panel: port errors, discards and host outages with one-click access to the switch port table.*

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
- Sign-in with three roles — viewer, user, administrator — and TOTP as
  a required second factor for administrators; recovery codes; a lock
  after ten wrong passwords that ends by itself. Until the first
  administrator is created on the server, the map works as before,
  open to everyone, and says so in the header. The first administrator
  and getting back in are done with `python -m moonlan.users` on the
  server, with the service running. What a role may not do is shown
  greyed out with the role it needs, never hidden. Plain HTTP is said
  to be plain HTTP: see [Users and sign-in](SIGN-IN.md).
- What a person placed is kept, the rest is laid out again. Pinned
  positions live on the server, not in one browser: a map of a network
  is a shared object, and two people looking at it have to see the
  same picture. Nothing else is stored as a position — where the
  physics engine happened to leave an unpinned node goes stale as soon
  as somebody moves what it hangs off. On every load the map is laid
  out from the pinned nodes outwards: what hangs off a pinned node
  starts next to it, then the next level, each at a spot derived from
  its id, so two pages start from one picture (and with the layout
  frozen show exactly one). The groups and switches round a pinned
  node — not the hosts, which fold round their switch whatever they
  start from — are remembered as offsets from it when it is placed, and
  start there, on the side a person left them, even after the node has
  been moved. A page that has been open for a week follows what other
  pages pin, release or reset at its next refresh.
- Placing a node is a separate act from looking at one. Outside
  **Arrange** mode a dragged node goes back to the layout engine; in
  it, a drag places a node and keeps it there, and the right button
  lets it go again. `P` does the same from the keyboard. A pinned node
  can be dragged in either mode and keeps its pin at the new spot —
  pinning says "the physics engine does not get to move this", not
  "nobody does" — and the header counts how much of the map is
  somebody's decision rather than the engine's. Placing and releasing
  are entries in the journal, one per action. "Remember the places
  around it" in the menu of a pinned node records how the groups round
  it stand now, without moving the node.
- Double-click a switch (or any group node) to select the devices on
  it, Ctrl+click to add and remove, drag the selection to move it all
  at once.
- A right-click menu on every node, acting on the whole selection.
  Ping and Traceroute run from the MoonLan machine, against the address
  MoonLan itself knows for that node, and the result opens in a panel
  beside what the continuous monitoring says; a selection or a group
  is pinged at once, into a table that fills as the answers come in.
  The web interface, SSH, remote desktop (a `.rdp` file), copy IP and
  MAC, and links of your own from `config.yaml`. An item that cannot
  run is greyed out with the reason — "IP unknown", "no traceroute on
  the server" — rather than missing. See
  [The node menu](CONFIGURATION.md#the-node-menu).
- A node that vanished from the network keeps its place — a device
  switched off for the night was not taken away — and a position is
  forgotten only when it is both older than `layout_keep_days` and has
  no node on the map. "Reset layout" asks once and then lays the map
  out afresh, without a reload. `python -m moonlan.diag --layout`
  reports all of it.
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
  agree. See [Frame corruption](DIAGNOSTICS.md#frame-corruption-how-to-read-the-alarm).
- Devices behind switches MoonLan does not poll stay on the map: a MAC
  visible only on trunk ports is drawn on the trunk with the best claim
  to it, marked "approximate" with a dashed edge, instead of
  disappearing into "Not on map" (`place_trunk_only_hosts`).
- LLDP (LLDP-MIB): every switch's neighbours — chassis id, port, name,
  model, management address and capabilities. Where two polled switches
  announce each other, the link and both its ports come from LLDP and
  the link card says so; every link carries a source (`lldp`, `fdb` or
  `both`), because "the two devices told us" and "we worked it out from
  the MAC tables" are not the same claim. See [LLDP](NETWORK.md#lldp).
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
  See [STP status](NETWORK.md#stp-status).
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
  as reporting nothing — never as "no loops". See [Loop
  Detection](NETWORK.md#loop-detection).
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

Version history: [CHANGELOG.md](../CHANGELOG.md)
([русская версия](../CHANGELOG_RU.md)).
