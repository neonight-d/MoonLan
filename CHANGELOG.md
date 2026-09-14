# Changelog

All notable changes to MoonLan, newest first. Dates are release dates;
every version was verified on the production network it was written
for before the next one started.

Русская версия — [CHANGELOG_RU.md](CHANGELOG_RU.md).

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
