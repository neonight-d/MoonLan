# LLDP, spanning tree and loop detection

What MoonLan reads from the protocols the switches speak about each other, and how to read its verdicts.

## LLDP

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

## STP status

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

### What BRIDGE-MIB really says

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

## Loop Detection

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

### The rule: normal is known, everything else is a loop

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

### Alarms

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

### Adding a model

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
