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

- **A repeat is a loop.** An answer that repeats — the OID asked for (the start of the walk or of a resumed pass for the first row, then the previous row), or any OID this walk has already had, from any pass and the no-such rows included — ends the walk at once, fault `repeated_oid`: from there the agent would hand out the same answers again, forever. pysnmp's `walk_cmd` does not check this and stops only on its own `Null` or `endOfMibView`; an agent of RouterOS 6.x that answers `lldpRemManAddrIfSubtype` with itself and `noSuchObject` turned that into hundreds of requests a second until the poll budget ended the whole poll. The OIDs seen are kept for one walk, one tuple of integers per row — acceptable for a MAC table of tens of thousands.
- **The order may be broken; the table is read to its end.** An answer that goes back to an OID before the previous one but not seen yet, inside the subtree, is the agent's own order, not a loop — RFC 3416 asks for the order, and an access switch handed out its Q-BRIDGE table VLAN by VLAN, out of numeric order, the rows after the step back found nowhere else. Such a table ends by itself; it is read on, `out_of_order` counts the steps back, and one INFO per host and OID says so. The repeat is checked first, the subtree boundary after it: an OID outside the subtree is the end of the table, as it always was.
- **A run of no-such is the end.** A row carrying `noSuchObject` / `noSuchInstance` is skipped, so one damaged row does not cost the table; `WALK_MAX_NO_SUCH` (100) in a row ends the walk, fault `no_such_run`. A row with a value resets the count. `endOfMibView` ends a walk as it always did.
- **What came, stays.** The rows read before a fault are kept (`truncated` when there are any), the walk is not resumed — the agent would answer the same — and it is not a dead-OID strike: it costs one request now, not a timeout. One WARNING per host and OID in the life of the process, one INFO when it walks normally again; `/api/skipped-oids` lists the faults.
- **The budget is the only clock.** There is no time limit per walk: a legitimately slow table — the MAC table of a big switch — would be cut short silently and its hosts would leave the map. Every walk of a full poll is timed instead, and a poll the budget cancels says which walk it was cut in (`cut_at` in `/api/polling`); the walk the cancellation lands in keeps its record "running" for the server to read. Only `collect()`'s own walks are timed, so a counters cycle that takes the host the moment a cancelled poll lets go of it cannot overwrite that record.

#### A router's identity (v0.8.11)

For each router of `routers:` the collector reads, with its ARP table, `ifPhysAddress`, `ifName` (or `ifDescr` where it is empty), `ifType` and `ipAdEntIfIndex` — all through `_walk`, with every rule above. moonlan/routers.py turns them into a `RouterIdentity`: each of the router's MACs with the names of the interfaces that have it (the one it is called by first: a bond before its members, a port before a VLAN), each of its own IPs with the MAC of its interface, and the MAC of the interface the `routers:` address is on. A router that answers no `ifPhysAddress` has an empty identity, and nothing below changes for it. The identity lives for one scan; the service does not store it.

The server uses it in four places, all in `_build_map`. A router's own MACs join the MACs ARP vouches for (`with_ip`): never copy candidates, never held back, confirmed at once without `new_mac` (`upsert_hosts(vouched=…)`), and what the copy rules did to them before is undone (`Database.vouch_for`). `routers.place()` picks one node per router — an LLDP bridge that is the router, or the router's MAC on the map, the interface of its `routers:` address first — and every other MAC of the router is merged into it (`merged_into`), whether it is on that port, on another one or only in the database. An LLDP neighbour is the router when its chassis ID is one of the router's MACs or its addresses are the router's; on the node's port its data goes onto the node. A note or a pinned position on a merged MAC moves to the node once, when the node has none (journal user `@moonlan`).

### Topology

moonlan/topology.py converts observations into links, hosts, pseudo-switches, bridges, groups, and placement decisions. Evidence has different strength: explicit LLDP agreement, validated direct FDB relationship, one-way/FDB inference, and approximate/trunk placement. The UI keeps that distinction visible.

LLDP arrives on a physical port, and the devices behind an aggregate are on the aggregate. `detect_bridges()` puts a neighbour heard on a member on the aggregate's port (`aggregate_port()`, both kinds: IEEE8023-LAG-MIB and the synthetic D-Link trunk), keeping the member in `member_port`, so everything matched by (switch, port) — a bridge taking over a pseudo-switch, a bridge and its own MAC as one node, the LLDP data of a non-bridge on its host, `uplink_ports` — sees one port (v0.8.11). The ports panel reads the LLDP table itself and keeps showing a neighbour on its member's row. `normalized_fdb()` resolves both kinds of aggregate the same way.

### Monitoring

Counters, ping, STP, loop detection, and alarms run on separate cadences where possible. A slow topology poll should not unnecessarily block lightweight monitoring.

### Persistence

SQLite stores inventory, layout, alarms, and journal information. WAL mode is used for this workload so reads and writes can overlap safely.

### API

moonlan/server.py is the FastAPI boundary. It combines the latest topology state with fresh database fields when serving the UI.

The /api/health endpoint is intentionally cheap, answers without sign-in, and says only that the process is up and which version it is. /api/status contains deeper operational state and needs a viewer.

#### server.py, taken apart in stages (v0.8.12)

server.py grew to nearly 5000 lines: the state of the process, 46 routes, the scan and polling loops, the building of the map, the cleanups and the lines the log opens with. It is being taken apart one stage per version, and each stage is accepted on one criterion: everything works as before — any visible difference is a bug.

1. **v0.8.12:** moonlan/startup.py and moonlan/housekeeping.py — leaves with no state of their own.
2. moonlan/runtime.py: the state of the process in one object, `rt`; the tests move onto it.
3. moonlan/routes/: the routes, by module, through `rt`.
4. moonlan/poller.py: the polling loops and the building of the map, through `rt`.
5. `_build_map` split into steps, with a test of the order of its side effects.

Where things are now:

- **moonlan/startup.py** — what the start says, and the port that only redirects: `configure_logging`; `_log_config` with `_config_missing`, `_log_settings` and `_setting_problem`; `_log_public_url` and `_log_transport`; `_log_certificate` (`CERT_WARN_DAYS`), which returns the certificate it read for server.py to keep; `start_redirect_listener`.
- **moonlan/housekeeping.py** — the cleanups (`purge_invalid_macs`, `purge_old_hosts`, `purge_journal`, `hide_confirmed_copies`, `purge_copies`, `drop_unpinned_positions`), the end of the certificate (`check_certificate_end`, `CERT_ALARM_DAYS`, `CERT_CHECK_SECONDS`), the leak watch's `resource_usage`, and `RESOURCE_LOG_SECONDS` and `CLEANUP_SECONDS`.
- **moonlan/server.py** — the rest: the state of the process (the config, the database, the alarm engine, the certificate, the public address…), `lifespan`, the routes, the scan and polling loops and `_build_map`. The loops that run the cleanups (`periodic_cleanup`, `periodic_certificate_check`, `periodic_resource_log`) stay here until stage 4, and `purge_layout` until stage 3: it needs the helpers of the layout routes.

The border, for this stage and every later one:

- A module taken out of server.py **never imports moonlan.server** — not at the top, not inside a function, not by name. It imports only the modules below it: config, db, https, settings and so on.
- **What it needs is given to it**, as parameters named after the server.py globals they stand for (`config`, `db`, `alarm_engine`, `public_url`…). The moved code is the old code word for word, comments included; a function that set a global returns the value instead. Its log is `logging.getLogger("moonlan")`, not `__name__`, so the log lines stay `moonlan: …`.
- **server.py keeps a wrapper** of the same name and signature for every function that moved, and re-exports every constant. A wrapper reads the globals when it is called, so a test that patches `server.certificate` or replaces `server.db` works unchanged. The wrappers are temporary: stage 2 removes them.
- A move is a commit of its own, with nothing else in it, so `git diff --color-moved` and `git blame -C -C` show the code as moved, not rewritten.

tests/test_module_boundaries.py holds every module of the list `NEW_MODULES` to this: no import of moonlan.server, none loaded with it in a clean process, the "moonlan" logger, and every moved name still in server.py. A new stage adds its modules and names there.

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
   (`ipNetToMediaPhysAddress`), names via reverse DNS. Each router
   also reports its own interfaces — `ifPhysAddress`, `ifName`,
   `ipAddrTable` — and is drawn as one node whichever of its MACs the
   switches see (v0.8.11).
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
   capability that belong to no polled switch become named nodes. A
   neighbour heard on a member of an aggregate is placed on the
   aggregate, where the devices behind it are.
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
