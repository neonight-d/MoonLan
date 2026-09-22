# Changelog

All notable changes to MoonLan, newest first. Dates are release dates;
every version was verified on the production network it was written
for before the next one started.

Русская версия — [CHANGELOG_RU.md](CHANGELOG_RU.md).

## v0.7.1 — 2026-09-22

Dragging a node is not a decision.

v0.7 pinned a node on every drag, and I argued for it in the spec:
"moving a box on the screen is a statement". It is a convincing
sentence about the wrong activity. People drag nodes to look — pull the
cloud of hosts aside, lift a switch out of the tangle, see what is
behind it — and turning each of those into a commitment meant the map
set like concrete without anyone ever choosing it. No tag was put on
v0.7, which was right: a version that gets in the way of using the map
is not a release, however carefully its server half was built.

Placing a node is a different act from looking at one, and it now has a
mode of its own. In **Arrange** mode a drag places a node and the right
button releases it; outside, a drag is just a drag. The mode shows on
the canvas as well as the button — a mode you can forget you are in is
a mode that edits the map by accident — and it is deliberately not
remembered between sessions. `P` does the same from the keyboard. A
pinned node stays draggable in either mode: pinning says "the physics
engine does not get to move this", not "nobody does". The confirmation
dialog in front of releasing a node is gone; pinning cost one gesture
and undoing it cost a dialog.

A full reset did nothing visible until the page was reloaded. vis keeps
x and y on the node it has already built, and nothing in the update
path can take them away again — `setOptions` assigns a coordinate only
when one is given. The nodes went on standing where they were, the
physics restarted from the same points, and the reset looked inert. The
node set is now rebuilt for that one case, while the thirty-second
refresh keeps updating in place so the camera survives.

A node with no saved position had nowhere in particular to be, so it
appeared wherever vis felt like putting it — the other half of the same
complaint. It now starts next to the thing it is plugged into, taken
from the edges that were just drawn, with an offset derived from its id
so the same device lands in the same place for everyone.

And there is no "save layout" button any more. A button that has to be
pressed for the picture to survive is a button somebody will forget,
and then the map they spent ten minutes arranging is gone. A node with
no saved position gets one as soon as the layout settles; a node that
already has one is never touched by it, or every open browser would
rewrite the shared picture on every refresh. The header counter changes
with it: counting nodes the saved picture did not contain made sense
while saving was a decision, and now it counts pinned nodes instead —
how much of this map is somebody's choice rather than the engine's.

Double-clicking a switch or a group node selects the devices on it,
Ctrl+click adds and removes, and dragging the selection moves it all at
once. The right button opens a menu built from data rather than markup,
acting on the whole selection: place, release, open the card, open the
ports. The next version fills that menu with diagnostic actions and the
operator's own commands, and it should not have to be rewritten to do
it.

## v0.7 — 2026-09-22

A layout that does not rearrange itself.

The map has never remembered where anything is. "Freeze layout" turns
the physics engine off and nothing more; inside one session the nodes
stay put only because vis.Network is updated rather than rebuilt. Press
F5 and the map lays itself out again, and the administrator in the next
room has it arranged differently. The export to PDF and Draw.io that
stands next in the roadmap was waiting on this: a diagram in a cabinet
is useful because it matches what somebody remembers, and a PDF with a
different picture every time does not do that job.

Positions now live in the database, not in one browser. A map of a
network is a shared object: two people looking at one network have to
see one picture, or "the switch at the bottom left" stops meaning
anything. The language and the freeze toggle are personal and stay
where they are; coordinates are not. The keys were already there and
already stable — the node ids the topology builds out of what a device
IS.

Dragging a node is a statement: the person doing it knows where that
switch stands better than a force-directed layout does. So a dragged
node is pinned where it was dropped and saved, with nothing to press.
It is marked, because the map has to answer "did MoonLan arrange this
or did I" without anyone guessing, and it can be released from its card
or with a right-click. "Save layout" records where everything is, not
only what was pinned, and does not un-place what was put by hand.

A node the saved picture does not contain is drawn with a dashed
outline and counted in the header — the same discipline as "N switches
ran out of time": a difference between what was recorded and what is
there now has to be visible, not discovered when the map is printed.

A node that vanished from the network keeps its place. A device
switched off for the night was not taken away, so nothing is forgotten
for being absent; only age forgets a position, and only for a node the
map no longer has. That housekeeping runs at the first scan after a
restart rather than at startup proper — before that scan there is no
map to compare against, and a restart would throw the whole layout
away. Saving and clearing the layout go into the journal: somebody
changed the picture everybody else is looking at.

`diag --layout` reports what is stored, what was placed by hand, what
has no saved position and what has no node.

Deliberately not in this version: manual links and manual nodes. A link
drawn by hand is indistinguishable on the map from one that was
inferred, and there is nothing to check it against — it will not go
stale, will not disappear when a cable moves, and will go on asserting
something that stopped being true years ago. That is the opposite of
the line this project has held since v0.6.4. If it is ever wanted, a
hand-drawn object has to look different, be dated and be checkable, and
that is its own piece of work.

## v0.6.15 — 2026-09-21

A reading nobody took is not an observation. Two defects, neither of
them about topology, both found while writing up earlier work.

A switch that runs out of its poll budget keeps the last table that did
arrive, so the branch behind it stays on the map. That is v0.6.12 and
it stands. What did not stand is that nothing downstream could tell the
copy from a fresh reading: `upsert_hosts` set `last_seen = now` and
`seen_count += 1` for every device behind it, so a device unplugged an
hour ago reported "last seen: just now"; a MAC present in that one
reading accumulated confirmations from repeats of itself and was
announced in the journal as a new device, with no second observation
ever happening; and `FdbStability` re-stamped every entry as fresh, so
a three-poll smoothing never expired and quietly became permanent
memory.

The same sin as v0.6.4's "no answer is not zero" and v0.6.13's "a dash
is not expired", from the other side: not an absence dressed as a
value, but a copy dressed as an observation. Such hosts are now marked
and dated. They stay on the map at their port — cables do not move
every ten minutes — but they are kept out of the inventory write and
out of the set that projects confirmations, and the smoothing countdown
runs without being refreshed while the saved table goes on drawing the
links behind it. The host card says which switch, when the reading was
taken, and that "last seen" above is the last time the device really
was found. `diag --hosts` honours the poll budget too, and says whose
MAC table is missing from its comparisons.

And the map could stop being refreshed without saying so. Three
periodic requests — the map, the ports panel, the scan progress — none
of them caught anything, so when the service went away the promise
rejected unhandled every thirty seconds and the picture simply stopped
changing, which looks exactly like a quiet network. All three now
survive it: the last good picture stays on screen, the header says in
amber that the data is no longer arriving and how old it is, recovery
clears it silently, and the console gets one line for the loss and one
for the return instead of one every thirty seconds.

## v0.6.14 — 2026-09-21

LLDP builds the tree, it does not decorate it. And one branch of the
live network turned out to need something else entirely.

Behind port 1/28 of mb1 hang three boxes: two RouterOS bridges and an
Edge-Core. LanTopoLog draws them as a garland. MoonLan drew all three
straight onto mb1 and laid the one LLDP edge it had on top, closing a
ring that does not exist.

The order of the two passes was wrong. A MAC table says a device is
*reachable through* a port; LLDP says a device is *on the cable*. The
second is the stronger statement, and it was arriving after the weaker
one had drawn the picture — `merge_lldp_links` could refine ports and
add a missing pair, but never remove the link that pair contradicted.
`lldp_link_candidates` now runs before `infer_tree`, which takes the
pairs: a member another member reports on a port of its own that is not
its uplink is *behind* that member, leaves the contest for "nearest",
and is hung off its host afterwards.

The same rule, stated once and applied to the finished link set,
withdraws a MAC-table link that LLDP forbids — but only where the link
it prefers is present, and only where the switch in question is the far
end. Each removal is a WARNING and a journal entry: the tree is not
rearranged silently.

A ring among polled switches is now either real or an invention. Real
means the spanning tree is holding one of its ports in discarding, and
that ring is drawn as it is. A ring with no blocked port loses its
weakest link; where several are equally weak, the one "behind, not
beside" forbids; where nothing tells them apart, nothing is removed and
every link is marked, because erasing an arbitrary cable is worse than
admitting the ring cannot be explained.

Then the live network said the diagnosis was half right. LLDP in that
branch is unusable: those RouterOS bridges forward LLDP frames, so
every port of theirs carries extra talkers and yields no link — and
mutual agreement does not rescue it, since forwarding is symmetric and
mb1 and the Edge-Core two hops apart name each other as confidently as
two devices sharing a cable. What was actually missing was direction.
`uplink_of` finds the way out of a branch by looking for switches
outside it, and this branch sees nothing outside itself, so all three
came back with an unknown uplink — which disarmed the very test that
orders a branch. Inside a branch there is a second answer, already used
to draw the far end of every link: the port a member sees its parent
on. With it, the MAC tables order this branch on their own, and the map
now draws what LanTopoLog draws.

The fallback star is still there for branches nothing can order, and it
is now dashed and dimmed, with both the edge tooltip and the link card
saying what is unknown about it. The switch card lists every line drawn
from that switch — neighbour, port, source, speed, whether STP is
blocking it.

`diag --topology` finally honours the per-host budget v0.6.12 gave the
service, runs the same passes in the same order, prints each link's
speed and both port states, and lists what the inference withdrew and
which rings are left standing.

## v0.6.13 — 2026-09-18

A dash means never measured, not quietly expired. Four absences that
looked alike, and one that pointed the wrong way.

The ports panel lost whole columns of counters and got them back a
minute later. Nothing was wrong with the switches: `current()` dropped
every rate older than three counters intervals, the port fell out of
the answer, and the panel drew "—" — the same "—" it draws for a column
the agent does not implement. Opposite diagnoses. A measurement is now
reported however old it is, with its age: shown dimmed past three
intervals, withheld past `stale_rate_hide_minutes` (30), where a rate
really has become a memory — and even then the empty cell says when the
port was last measured. Only a port never measured at all gets a bare
dash.

Half of why those cycles were being missed: the counters cycle gave up
the instant it found a host's lock taken by the scan, and a scan holds
a host for as long as its poll budget allows. Any budget of two
intervals or more therefore guaranteed a missed cycle after every scan
— the operator had set 180 against an interval of 60 and made himself
four permanently ageing switches. It now waits up to
min(counters_interval / 2, 20) seconds, and a budget at or above twice
the interval is reported at startup and in `diag --config`, per device.

`10.3.7.10` was not polled in full once in six hours: thirty scans,
thirty budget failures, while the map showed it alive from its last
complete reading. That is a third state beside "answering" and "down".
After `stale_switch_scans` (5) consecutive scans the card counts them
and dates the reading, and a `switch_stale` alarm is raised — warning,
syslog, cleared by one full poll. Never `switch_down`: sending somebody
to look for a dead device that is answering wastes the trip. Each poll
now records how long it took, and `diag --config` prints it — a budget
set by eye is a budget set wrong.

The STP panel called mb0 the root while the map drew it as an ordinary
switch. Both read the same fields off the same objects, at different
moments: `judge_network`, which is the only test that can recognise a
root reporting zeros about itself, ran after `build_topology` had
already decided the node was not operating. It now runs first.

`community: public` went back into the config by accident, every switch
went dark, and `diag --walk` said "the subtree is empty, or the agent
does not implement it". Both halves were wrong — SNMPv2c does not
answer a wrong community at all, and that silence is shaped exactly
like an unimplemented object. MoonLan now asks `sysDescr` and pings
before deciding, and names the community first when the host answers
ICMP and nothing else. All switches silent at once is reported as one
fact rather than N.

And the panel showed two spanning trees, the second one rooted at the
string "unknown" and holding three RouterOS bridges, with
`stp_fragmented` raising and clearing over it. A walk of
1.3.6.1.2.1.17.2 on an RB941 returns the protocol, the priority and the
whole port table — and none of the scalars in between, so the bridge
names no root while its uptime is six weeks and its
TimeSinceTopologyChange is zero, which the historical heuristic read as
"converged long ago". A bridge in a tree knows its root; where there is
none, the heuristic no longer gets to guess. And the absence of a root
is no longer allowed to become a grouping key: switches with no root
are listed under the table rather than counted as an island.

## v0.6.12 — 2026-09-17

One slow agent delays itself, not the whole map. Four MikroTik boxes
joined `switches:` and the map stopped updating. They were not silent:
they answered, taking 272 and 468 seconds each, and the scan waited for
the last one — then the next scan returned immediately because that one
was still running.

`return_exceptions=True` (v0.6.6) had covered a switch that raises, not
one that is slow. Every request stayed inside `snmp.timeout`; nothing
bounded their sum. `snmp.host_budget_seconds` (default 120) now does.
A switch that runs past it is left out of that scan and the rest of the
network gets its map on time.

It is **not** reported as unreachable, because it is not: it answers,
only too slowly. No `switch_down` is raised for it and none is cleared
— we stopped asking, which is a fact about MoonLan and not about the
switch. Its last complete reading stays on the map, its card dates it,
and the header says how many switches ran out of time.

A `switches:` entry may now be a mapping with `ip:` and any of
`community`, `timeout`, `retries`, `retries_on_break`,
`host_budget_seconds`, applying to that switch alone; everything else
is inherited from `snmp:`. The plain list of addresses every existing
config.yaml uses keeps working untouched. A slow box usually wants a
*shorter* timeout and one retry, not a longer one — the requests that
cost the time are the ones it will never answer. `diag --config` prints
the settings each switch is actually polled with, marking the ones it
was given of its own.

`1.0.8802.1.1.2.1.4.2.1.3` — the LLDP management-address table — came
back from both RouterOS boxes as zero rows after the full retry budget,
every cycle. The other thirteen walks on the same device got through:
the agent does not implement the table and cannot say so, where an
agent that can answers `noSuchObject` in one round trip. After
`snmp.dead_oid_strikes` walks in a row that returned no rows **and**
ended in a timeout, that OID is left alone on that host for
`snmp.dead_oid_cooldown_scans` scans and then tried again. Only that
one outcome counts: a partial answer is what `retries_on_break` is for.
Both edges are logged and the skipped walk reports "no answer" rather
than an empty table — data missing because MoonLan stopped asking must
never be mistaken for data the device denies having. `diag --skipped`
asks the running service what is on pause and for how much longer.

Both RouterOS boxes also reported "4 of 4 neighbours sit on a local
port that could not be matched to an interface". `lldpRemLocalPortNum`
is 0 on every row there, so the remote table says nothing about the
local port — but that was not what broke it. `lldpRemChassisId`
announces subtype macAddress and then carries seventeen bytes of ASCII
instead of six octets, and a string is in nobody's forwarding table, so
the FDB fallback — which knows all four of these addresses — never got
the chance to place them. A MAC spelled out is still a MAC; all four
now land on their ports. `lldpRemLocalPortNum` is additionally tried as
a bridge-port number, between the forwarding table and the bare
ifIndex.

The header counts switches off while a scan runs ("Scanning: 6 of 8"),
`/api/status` carries the same fields, and a scan that gave up waiting
for somebody says so in amber with the names in the tooltip. Ten
minutes of a map that did not move, with nothing in the interface
saying so, is what sent the operator to journalctl.

## v0.6.11 — 2026-09-17

One root is one root, and a panel header stays put. RSTP went live on
the network this project was written for, and the STP panel answered
by showing three of its own defects.

A Bridge ID is a 4-bit priority and a 12-bit system id extension, not
one number — and some agents put the priority in the low byte, so one
root read as `4096/34:0a:...` from an HPE and `16/34:0a:...` from an
Edge-Core, and the network was reported as having two trees. Bridge
IDs are now parsed per 802.1t, the shifted encoding is detected,
corrected and logged rather than silently repaired, and roots are
compared by MAC.

Four D-Links running RSTP were called "not operating" because the
only test was historical — topology changes, or a change newer than
the uptime — and a tree switched on an hour ago has neither. Two tests
now come first: a bridge that accepted somebody else's root at a cost
above zero is participating, full stop; and a switch whose neighbours
follow its own address is the root, which is the one thing a switch
cannot establish about itself. `diag --stp` and the panel say which
test decided. A switch that answers BRIDGE-MIB with nothing at all —
zero Bridge ID, no topology changes, no port out of disabled — is now
reported as exactly that, instead of as a tree that failed to
converge.

Also: a port reported blocking with no cable in it is not a blocked
link (RouterOS reports blocking on every spare socket); the panel
skeleton from v0.6.2 is now a shared class, so the STP, alarms,
journal and details panels keep their header and close button in view
while their contents scroll; and `stp_fragmented` says what it may
well be — segments whose trunk ports sit in different VLANs form
separate trees by design, and the panel lists the VLANs beside each
root.

## v0.6.10 — 2026-09-14

Devices seen through a trunk are grouped beyond it, not on it. A
device visible only through a trunk is placed on that trunk and marked
approximate — the port is right, the place behind it is unknown — and
twenty of them drawn one dot at a time make the port look like twenty
computers plugged into the switch, mixed in with its real neighbours.
They now get a container: one "Beyond the trunk · N" node past the
cable, which expands into the list of what is out there. It is
deliberately not a "switch without SNMP": that node claims a switch is
there, and here nobody knows what is. Pseudo-switches are still never
created on a trunk — many MACs on a trunk is what a trunk is for. The
group hangs off whatever stands on that cable when something does, and
never off a switch MoonLan polls: if the devices were behind it they
would be on its downlink ports. Threshold: `trunk_group_threshold`,
default 3.

## v0.6.9 — 2026-09-14

A device seen on an uplink is not behind it. The switch that reports a
MAC address on the cable it came in on is telling you where the device
is **not**, and the placement rule had been reading it the other way:
the deepest switch in the tree won every claim it could make, so an
Edge-Core whose only live port was its own uplink collected twenty
devices belonging to the rest of the network. Direction now decides
first and depth second, the root's trunks count as pointing down
(nothing is above the root), and a device whose every sighting was on
an uplink is not placed at all — it goes to "Not on map", because
hanging it off somebody's uplink would be a statement about the one
place it cannot be. Host placement finally has tests: the rule had
been reordered three times without one, each fix breaking the case the
previous fix had made work.

## v0.6.8 — 2026-09-14

Identify the model before believing what it reports. In SNMPv2c an
agent answers a request for an object it does not implement with a
*successful* reply carrying `noSuchObject`, so a probe that accepts
"the agent replied" succeeds on every device alive — three switches
were read off a branch belonging to another vendor and then reported
as having their loop protection switched off. A branch now counts as
identified only when it answers with data, the loop-detection profiles
are keyed by the sysObjectID each model actually answers with (a
D-Link product's private branch is not under its own identifier), and
"unknown" is no longer printed as "switched off". An aggregate ifIndex
that is not in the interface table is dropped instead of drawing a
trunk nobody has, a switch MoonLan polls no longer raises
"unmanaged bridge" about itself, and an empty `sysName` stops
captioning a node with its address twice.

Also: the diagnostic dumps left the repository. `diag --anonymize`
rewrites any report through documentation ranges (RFC 5737, RFC 7042)
with a substitution table that stays consistent for the run, and
`docs/diag_example/` holds one anonymised sample of each report.

## v0.6.7 — 2026-09-11

Loop Detection, read from the vendors' private MIBs — there is no
standard one. Profiles describe a model family: which sysObjectID it
answers with, where its branch starts, the suffixes of the scalars and
the per-port columns, and which raw status means "no loop". Built in
for the D-Link DGS-1210-26 Rev.F1, DES-1210-28/ME and DES-3526; more
can be added in `config.yaml` without touching code. Polled on the
counters cycle rather than the ten-minute scan, because a loop is an
incident. Only the "no loop" value has ever been observed on this
hardware, so the rule is inverted: anything else raises
`loop_detected` (critical) with the raw value in the text, and the
first real loop documents itself. A model that reports nothing is
shown as reporting nothing — never as "no loops".

## v0.6.6 — 2026-09-11

One impossible OID stopped the counters for the whole network. A
synthetic aggregate carries a negative ifIndex, pyasn1 refuses to
build a request for it, and the exception left through
`asyncio.gather` — every column on all five switches was empty for as
long as the service ran. Fixed in four layers, and offline groups now
hang off the bridge on their port instead of beside it.

## v0.6.5 — 2026-09-10

A walk that stops answering halfway is picked back up from the last
OID that arrived, up to `snmp.retries_on_break` times: on a switch
with 36 interfaces the gigabit uplinks at the end of the table went
missing in three polls running. A partial answer is reported as a
partial answer rather than "NO ANSWER", ports a resumed walk still
missed are fetched one GET at a time from the 32-bit counter, and a
packet column of zeros beside terabytes of octets is read as unfilled.
SNMP defaults raised to `timeout: 5`, `retries: 2` — a mean timeout
does not look like a timeout further down the line, it looks like a
switch that does not implement the OID.

## v0.6.4 — 2026-09-10

Honest counters. `bytes()` on a pysnmp integer allocates a zero buffer
as long as the number, which is how `diag --walk` asked for 1.49 GB on
an octet counter and was killed. A counter column the agent never
answered reads "—", not 0.0, and raises no alarm in either direction;
each direction falls back to its 32-bit counter on its own. A quiet
device stays at the port it lives on instead of moving to the core's
trunk, the external network hangs off the provider's bridge, and our
own router's port stops looking like a way out of the network.

## v0.6.3 — 2026-09-10

Readable links and addresses in the cards, the address an operator
actually uses under a router's name, `uplink_ports` announcing itself
from the port that looks like one, and a second instance that can be
run safely beside the live service (`MOONLAN_CONFIG`, SQLite in WAL
mode).

## v0.6.2 — 2026-09-10

One device, one node: a bridge whose MAC is also in the MAC table of
its own port is drawn once, with its name and address, instead of
twice. Routers are named and drawn as diamonds, port labels are shown
only where they are labels rather than a firmware template, the ports
panel keeps its header and port column in view, and `uplink_ports`
collects what lies past a provider handover under one node.

## v0.6.1 — 2026-09-09

Fixes from the production network, starting with the crash that left
it with no map at all when several bridges shared one port. A
neighbour is treated as a bridge only when it says it is one, a busy
access port is no longer called "LLDP forwarding", LLDP neighbours are
placed by evidence rather than by whichever key matched first, 38 rows
of one MikroTik collapse into one device, and a failed scan says so in
the header instead of showing an empty map.

## v0.6.0 — 2026-09-09

LLDP: neighbours, capabilities and management addresses, links
confirmed by LLDP with a source marker on every one (`fdb` / `lldp` /
`both`), and the bridges nobody polls named on the map with an
`unmanaged_bridge_detected` alarm when one appears behind an access
port.

Honest STP status, which is what this whole branch grew out of. A
switch with spanning tree disabled still answers every `dot1dStp*`
object — priority 0, cost 0, itself as the root — and reading those at
face value turns five disabled switches into five root bridges. Root,
cost and root port are now used only for a switch that demonstrably
runs a tree; everything else reads "not operating", with the reason
kept for `diag --stp`.

Also `port_flapping`, which catches a link that bounces between two
counter polls by reading `ifLastChange` alongside `ifOperStatus`, and
`diag --walk` for exploring private MIBs.

## v0.5.8 — 2026-09-07

MAC confirmation before a new address becomes a device, frame
corruption detection (a failing cable makes a switch learn addresses a
few bits from the real one), and placement of devices visible only
through trunks.

Earlier versions are summarised in the roadmap table of the README and
in [docs/ROADMAP.md](docs/ROADMAP.md).
