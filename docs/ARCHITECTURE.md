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
- moonlan/users.py is `python -m moonlan.users`, the accounts from the server's shell. It writes to the same database; the service sees the change at its next request.
- While no enabled administrator exists, the table is not applied (except to account management): the service answers as it did before sign-in existed.

### Web UI

The web layer is static HTML/CSS/JavaScript. The browser refreshes API data and renders the topology with vis-network. Server-side content hashing gives UI assets versioned URLs while static responses remain revalidated.

## How it works, step by step

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

## Extension rule

When adding a feature, identify whether it is a new observation, a new inference, a persisted fact, an operator action, or presentation-only behavior. Keep those layers separate to reduce accidental coupling.
