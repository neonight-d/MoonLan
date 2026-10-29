# Diagnostics

```bash
python -m moonlan.diag <switch_ip> [--community public] [--timeout 2]
```

Prints everything MoonLan sees on the device via SNMP: interfaces,
bridge-port mapping, FDB distribution, LAG-MIB support, visibility of
the other configured switches. Read-only; does not touch the database.

Some reports ask the running service for what only it knows — poll
times, paused OIDs, which nodes are on the map. With sign-in on, the
service answers diag with a token it writes at every start next to its
database (`<db_path>.console`, mode 0600) and treats it as a viewer
that only reads: run diag as the user MoonLan runs as, in its
directory.

## Diagnosing one device

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
rows are always shown — they carry no usable port. For a MAC, `--host`
also names its maker.

## Who made a device

```bash
python -m moonlan.diag --oui 10:ff:e0:00:00:01     # any case, any separators
```

The maker by the IEEE register, which register named it (MA-L, MA-M,
MA-S or IAB) and how long a prefix matched. When a 28- or 36-bit block
matched, it also prints the MA-L block it was carved from — that row
reads "IEEE Registration Authority", which is what a lookup by the
first three bytes alone would have said. A random or hand-set address
(the second bit of the first byte) and a group address are not looked
up, and the line says which bit decided it; an address the register
does not list shows the three prefixes that were tried.

`--config` shows the register itself: the folder (`oui.path`), each of
the four files with its rows and date, a file missing, and a register
older than 180 days. To fetch or refresh it, see
[OPERATIONS.md](OPERATIONS.md#the-maker-of-a-device-ieee-register).

## Updating the configuration

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

Then what the configuration makes of HTTPS and keys: the mode (HTTPS
served by MoonLan, at a proxy, or plain HTTP), the public address and
the host keys are bound to, the trusted proxies, the certificate —
subject, names, the end date, whether it names the public host, whether
its key is readable by others — HSTS, the redirect port, the installed
`fido2` and `cryptography`, and whether keys are on and, if not, why.
The sign-in part names the accounts that have keys and any
administrator with neither TOTP nor a key.

The last table it prints is the SNMP settings every switch is actually
polled with, marking the ones that switch was given of its own rather
than inheriting from `snmp:`.

## What is not being asked for

```bash
python -m moonlan.diag --skipped
```

Lists the (host, OID) pairs MoonLan has stopped polling because they
answered nothing but timeouts, and how many scans are left before each
is tried again — see `snmp.dead_oid_strikes` in
[CONFIGURATION.md](CONFIGURATION.md#when-one-switch-is-not-like-the-others). The pause lives in the
running service, so this asks it over the API rather than guessing; it
needs MoonLan to be up, at the `listen.host` / `listen.port` from the
same `config.yaml`.

## How complete is the inventory

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
- who made the devices in the MAC and ARP tables: the makers, most
  first, and how many addresses are random or hand-set, or not in the
  IEEE register;
- database totals: hosts, how many are missing from the current MAC
  tables (and how many of those are still inside the grace window),
  never located, without an IP, without a name.

A subnet whose devices are all "in ARP only" is behind a router (or
behind a switch MoonLan does not poll): its traffic never crosses a
polled switch port, so an L2 map cannot place those devices — MoonLan
lists them under "Not on map". Since `routers:` accepts a list, adding
every L3 device that holds an ARP table (each VLAN gateway, each
router) is what makes the inventory more complete.

## Errors vs discards

```bash
python -m moonlan.diag --port 10.0.0.21               # every active port
python -m moonlan.diag --port 10.0.0.21 --iface Gi0/1 # one port
python -m moonlan.diag --port 10.0.0.21 --watch 3     # 3 measurements, 60 s apart
```

## "—" is not zero

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

## Frame corruption: how to read the alarm

```
WARNING port_frame_corruption: 10.0.0.44:1/5 — 7 distorted copies of
20:7b:d5:1a:31:8d in the MAC table (20:7b:d5:1a:31:87, 20:7b:d5:1a:31:8f,
…) — all of one address: its network adapter is the likelier fault; try
the device through another adapter; inbound errors in 10 min: 340
```

A switch learns the source address of every frame it forwards. When the
frames themselves arrive damaged — a failing transmitter, cable or patch
cord, a dying transceiver, interference on a long run — the switch
faithfully learns the damaged addresses too. Next to the real
`20:7b:d5:1a:31:8d` the MAC table grows `20:7b:d5:1a:31:9d` (one bit
away), `20:7b:d5:1a:31:87`, `80:7b:d5:1a:31:8d`. Each of them lives for
a poll or two and never answers ARP.

MoonLan reads the pattern for what it is: an address the database has
not accepted as a device and that has no IP, within
`corruption_hamming_bits` (8) of one it had accepted **on the same
switch** — on any port of it, in this poll or within `host_grace_hours`
of it. Those addresses are kept off the map however many polls they
are seen in, the port they are on carries a ⚠ mark in the "Ports"
panel listing them, and once `corruption_macs_threshold` (5) of them
accumulate inside `notifications.flap_window_seconds`, the port raises
the alarm. If the port's error counters are alarming too — or were
recently — the standing alarm is upgraded to **critical** and says so:
two independent symptoms of one physical fault.

The alarm and the ⚠ mark say which fault is likelier
([MAC copies and errors on a port](NETWORK.md#mac-copies-and-errors-on-a-port)):

- **copies of one address** — that device's network adapter. Connect
  the device through another adapter, or another device to the port;
- **copies of several addresses** — what they share: the cable, the
  patch cord, the port, or an unmanaged switch behind it.

The alarm in the page carries the port's inbound errors over one scan
interval, when the counters know them.

Before v0.8.5 a copy seen in two polls in a row was confirmed as a
device. Those records are found by what they are — confirmed by
sightings alone, never an IP, an ARP entry, a ping reply or a name, at
most ten sightings, nobody watching them or labelling them, near an
address with an IP on the same switch, and at least
`corruption_macs_threshold` of them around that one address — and
taken off the map at the first start, with one journal entry for the
batch ("Damaged MAC copies removed · 20:7b:d5:1a:31:8d: 104 addresses on
… 1/5"). The next daily cleanup deletes them.

To see the evidence:

```bash
python -m moonlan.diag --fdb 10.0.0.44            # whole MAC table
python -m moonlan.diag --fdb 10.0.0.44 --iface 1/5  # one port
python -m moonlan.diag --port 10.0.0.44 --iface 1/5 # its error counters
```

The last section of `--fdb` groups the corrupted addresses by port: the
real MAC each group is a distortion of, every copy with its Hamming
distance and whether anything ever gave it an IP.

If the copies stop appearing, the alarm clears itself once the flap
window passes without a new one.

Two devices with neighbouring factory MACs on one switch are not a
false positive as long as they answer ARP: an IP confirms an address
and takes it out of the candidates for good. A device that never gets
an IP and sits within the threshold of another one on its switch is
held back as a copy — it shows in the ⚠ list of its port with the
address it is taken for a copy of. `filter_suspect_macs: false` draws
such addresses anyway, a smaller `corruption_hamming_bits` narrows the
match, and `--fdb` always shows what was matched against what.

## Sharing a diagnostic report

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

## Walking an arbitrary MIB

```bash
python -m moonlan.diag --walk 10.0.0.10 1.3.6.1.4.1.171 --limit 200
```

Dumps any OID subtree: raw OID, SNMP type, the value as text and, where
it is not text, as hex. It exists for exploring private MIBs before
writing anything against them — D-Link lives under `1.3.6.1.4.1.171`
and HPE under `1.3.6.1.4.1.11` — and it is how the loop-detection
profiles were found. It is also the first step in adding a new one:
see [Adding a model](NETWORK.md#adding-a-model). The default 500-row ceiling keeps a stray
subtree from being downloaded whole.
