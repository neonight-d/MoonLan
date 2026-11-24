# MoonLan Architecture

## Overview

~~~text
SNMP / ARP / system probes
          │
          ▼
   collection + parsing
          │
          ▼
 topology inference + validation
          │
          ▼
      TopologyState
       │         │
       ▼         ▼
    SQLite     FastAPI
       │         │
       └────┬────┘
            ▼
        web interface
~~~

## Main modules

### Collection

moonlan/snmp_collector.py owns SNMP interaction and converts vendor-specific responses into structured switch data. The collector uses one SNMP engine per process and per-switch locks. Different switches can be polled concurrently while overlapping operations against the same switch are serialized.

#### The rules of a walk

A walk is a run of GETNEXT requests, and `_walk` holds the agent to RFC 3416 §4.2.2 rather than trusting it (v0.8.8):

- **The OID grows.** Each answer must carry an OID strictly past the one before it — the start of the walk (or of a resumed pass) for the first row, then the previous row, no-such rows included. Tuples of integers compare the way OIDs are ordered. An answer that is not past it ends the walk at once, fault `non_increasing`: the next GETNEXT would ask for the same OID, and an agent that answered it once with itself answers it so forever. pysnmp's `walk_cmd` does not check this and stops only on its own `Null` or `endOfMibView`; an agent of RouterOS 6.x that answers `lldpRemManAddrIfSubtype` with itself and `noSuchObject` turned that into hundreds of requests a second until the poll budget ended the whole poll.
- **A run of no-such is the end.** A row carrying `noSuchObject` / `noSuchInstance` is skipped, so one damaged row does not cost the table; `WALK_MAX_NO_SUCH` (100) in a row ends the walk, fault `no_such_run`. A row with a value resets the count. `endOfMibView` ends a walk as it always did.
- **What came, stays.** The rows read before a fault are kept (`truncated` when there are any), the walk is not resumed — the agent would answer the same — and it is not a dead-OID strike: it costs one request now, not a timeout. One WARNING per host and OID in the life of the process, one INFO when it walks normally again; `/api/skipped-oids` lists the faults.
- **The budget is the only clock.** There is no time limit per walk: a legitimately slow table — the MAC table of a big switch — would be cut short silently and its hosts would leave the map. Every walk of a full poll is timed instead, and a poll the budget cancels says which walk it was cut in (`cut_at` in `/api/polling`); the walk the cancellation lands in keeps its record "running" for the server to read. Only `collect()`'s own walks are timed, so a counters cycle that takes the host the moment a cancelled poll lets go of it cannot overwrite that record.

### Topology

moonlan/topology.py converts observations into links, hosts, pseudo-switches, bridges, groups, and placement decisions. Evidence has different strength: explicit LLDP agreement, validated direct FDB relationship, one-way/FDB inference, and approximate/trunk placement. The UI keeps that distinction visible.

### Monitoring

Counters, ping, STP, loop detection, and alarms run on separate cadences where possible. A slow topology poll should not unnecessarily block lightweight monitoring.

### Persistence

SQLite stores inventory, layout, alarms, and journal information. WAL mode is used for this workload so reads and writes can overlap safely.

### API

moonlan/server.py is the FastAPI boundary. It combines the latest topology state with fresh database fields when serving the UI.

The /api/health endpoint is intentionally cheap, answers without sign-in, and says only that the process is up and which version it is. /api/status contains deeper operational state and needs a viewer.

### Sign-in and rights

- moonlan/access.py is the rights table: every route of the app ("METHOD /path/template") and the least role it needs — public, signed_in, viewer, user, admin, accounts. A route missing from it is for administrators only; tests/test_access.py lists every route of the app and fails on any the table does not name, and tests/test_docs.py checks the table in docs/API.md against it.
- moonlan/signin.py is an ASGI middleware in front of every request. It finds the route the way the router does, looks the session cookie up in the database on every request — so an account changed from the console takes effect at once — and answers 401 or 403 before the handler runs or the body is read. It also holds the sign-in, one's own password and TOTP, and the accounts API. Who is asking reaches the handlers through a ContextVar, which is how the journal names the person.
- moonlan/auth.py has the primitives: scrypt passwords, RFC 6238 TOTP, recovery codes, tokens. Standard library only.
- moonlan/passkeys.py is the WebAuthn relying party: listen.public_url's host and origin, the one-time challenges, and the checks of what a key answers, done by python-fido2 — the one dependency outside the standard library that signing in needs, and an optional one: without it the service runs with keys off. signin.py keeps the parts that are about accounts: whose key it is, the signature counter, the throttle, the journal.
- moonlan/https.py is the public address, the certificate MoonLan serves itself (checked by run.py before uvicorn starts), the redirect port, and the list of trusted proxies that run.py hands to uvicorn — nobody's X-Forwarded-* headers are believed unless their address is on it.
- moonlan/users.py is `python -m moonlan.users`, the accounts from the server's shell. It writes to the same database; the service sees the change at its next request.
- While no enabled administrator exists, the table is not applied (except to account management): the service answers as it did before sign-in existed.

### Web UI

The web layer is static HTML/CSS/JavaScript. The browser refreshes API data and renders the topology with vis-network. Server-side content hashing gives UI assets versioned URLs while static responses remain revalidated.

## How it works, step by step

1. MoonLan polls every switch from `config.yaml` via SNMP: `sysName`,
   `sysDescr`, the interface table (IF-MIB) and the MAC forwarding
   table (BRIDGE-MIB / Q-BRIDGE-MIB). FDB entries on bridge-ports
   missing from `dot1dBasePortIfIndex` (trunks on some D-Link models)
   are kept on a synthetic port, `LAG <members>`, when the gaps in the
   mapping make that bridge-port an aggregate; any other unmapped
   bridge-port is dropped with its rows (v0.8.6).
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
9. Hosts, the event journal, alarms and the last full reading of
   every switch are stored in SQLite (`moonlan.db`), so `first_seen`,
   history and the map itself survive restarts.
10. The result is available through the REST API (`/api/topology`)
    and in the web UI.

## Performance boundaries

Keep different switch SNMP polls, independent ping targets, and independent counter polls parallel where their per-switch lock is free.

Keep operations against one switch serialized, database writes that preserve invariants ordered, and topology replacement as one logical state update.

Avoid creating a new SNMP engine for every scan, queueing indefinitely behind a slow switch, treating stale readings as fresh observations, rebuilding topology for an API request, or shelling out with user-controlled strings.

## Failure model

MoonLan prefers stale-but-labelled information to silently wrong information.

Examples:

- poll-budget expiry keeps the last complete reading but marks it as saved/stale;
- unsupported OID is not treated as zero;
- ambiguous links can remain visible with an explanation rather than being silently deleted;
- service/API failure keeps the last map and exposes the error state.

### Five states of a switch (v0.8.7)

Every switch of `switches:` is on the map whatever it answered. Where its
data comes from is its `state` (in `/api/topology` and `/api/polling`):

| `state` | When | On the map |
|---|---|---|
| `fresh` | a full poll in this scan | as polled |
| `over_budget` | answered, did not finish inside its budget | the last full reading, dated (v0.6.12) |
| `unreachable` | did not answer in this scan | the last full reading; the node says "not answering since HH:MM", its links and the devices behind it are muted |
| `restored` | not polled since the start yet | the reading saved in the database, "data from before the restart" |
| `never` | no full reading anywhere | an empty dashed node: its address and its note, no ports, no links; it stands on the port its management MAC is seen on, and that MAC is not drawn as a device |

The last three readings are readings, not observations: the devices behind
them are drawn (`from_saved`) but never recorded as seen — no `last_seen`,
no `seen_count`, no confirmation, no `new_mac` — the FDB smoothing is not
refreshed from them, and LLDP neighbours and bridges are remembered only
from switches read in full in this scan.

Alarms go by what happened in this scan, never by what is drawn: each
SwitchData carries `answered` (true, false, or null when not known), and
`switch_down` rises and clears by it. A budget that ran out after the
switch's first answer (`sysName`) is `answered: true`; one that ran out
before is `null`, and `switch_stale` does not claim the switch is
reachable then.

### Readings in the database

The last full reading of each switch is in `switch_readings` (ip,
polled_at, format, version, data), one row per switch, replaced after
every full poll. The data is JSON from explicit `to_dict()` /
`from_dict()` of SwitchData and the dataclasses inside it (ports, LLDP
neighbours, STP) — no pickle, and `format` says which shape it is.
Loop detection is not in it (the counters cycle refreshes it), nor
anything about the current scan.

At the start each configured switch gets its reading back as `restored`,
unless its `format` is not the current one or it is older than
`host_retention_days`; readings of switches no longer configured are
deleted. The map is drawn from what came back at once, dated by the
newest reading, through the same code as a scan with every write turned
off: no sighting, no journal event, no alarm raised or cleared. The first
full poll of each switch replaces `restored` with `fresh`.

## Extension rule

When adding a feature, identify whether it is a new observation, a new inference, a persisted fact, an operator action, or presentation-only behavior. Keep those layers separate to reduce accidental coupling.
