[Русская версия → README_RU.md](README_RU.md)

[![Tests](https://github.com/neonight-d/MoonLan/actions/workflows/tests.yml/badge.svg)](https://github.com/neonight-d/MoonLan/actions/workflows/tests.yml)
[![License](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB.svg)](https://www.python.org/)

# 🌙 MoonLan

**MoonLan** is a self-hosted Linux network-mapping and monitoring service that discovers physical LAN topology from switch data collected over SNMP and presents it in a live web interface. An open-source alternative to LanTopoLog.

> **Design principle:** show what the network actually told us, and clearly label what is inferred, stale, approximate, or unknown.

![MoonLan network map](docs/img/map.png)

---

## ✨ What it does

| Area | Capabilities |
|---|---|
| 🔭 Discovery | SNMP v2c, FDB/MAC tables, LLDP, LACP, VLAN/PVID, ARP/DNS |
| 🗺️ Topology | Direct-link inference, trunk awareness, STP-aware rings, unmanaged bridges |
| 📈 Monitoring | Ping, traffic rates, errors, discards, flapping, loop detection |
| 🚨 Alarms | Stateful alarms with Email, Telegram, Syslog notifications |
| 🧭 Operations | Shared layout, pinned nodes, search, journal, port/STP/alarms panels |
| 🔐 Access | Sign-in with three roles, TOTP for administrators, accounts from the server's shell |
| 🧪 Diagnostics | FDB, hosts, ports, STP, loop detection, topology, arbitrary MIB walks |
| 🛡️ Safety | Per-host poll budgets, stale-data labels, command argument validation |
| 🌐 UI | English/Russian interface, demo mode, responsive dark network map |

The whole list, item by item: **[docs/FEATURES.md](docs/FEATURES.md)**.

---

## 🚀 Quick start

Linux, Python 3.11+, switches with SNMP v2c (a read-only community).

### Install

~~~bash
git clone https://github.com/neonight-d/MoonLan.git
cd MoonLan

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
~~~

### Configure

~~~bash
cp config.example.yaml config.yaml
# set the switch addresses and the SNMP community
~~~

Every key, with its default: **[docs/CONFIGURATION.md](docs/CONFIGURATION.md)**.

### Run

~~~bash
python run.py
~~~

Open `http://<server>:8080/`.

For a safe first run, a virtual network:

~~~bash
MOONLAN_DEMO=1 python run.py
~~~

Running as a systemd service, the demo, a second instance beside a
running one: **[docs/OPERATIONS.md](docs/OPERATIONS.md)**.

---

## 🔐 Users and sign-in

Until the first administrator exists, the map is open to everyone who
can reach it — as before v0.7.4 — and the header says so. The first
administrator is created on the server, not in the browser:

~~~bash
python -m moonlan.users add <name> --role admin
~~~

Sign-in switches on at once, without a restart. Three roles: a
**viewer** looks; a **user** may also ping, rescan, pin and clear
alarms; an **administrator** may also reset the layout and manage
accounts, and must use TOTP. Over plain HTTP the password and the
session cross the network in clear text — HTTPS comes in v0.7.6.

Roles, TOTP, getting back in, what HTTP leaves open:
**[docs/SIGN-IN.md](docs/SIGN-IN.md)**.

---

## 🧪 Tests

MoonLan uses Python's standard **unittest** runner:

~~~bash
python -m unittest discover -s tests -v
~~~

CI runs the suite on Python 3.11 and 3.12 for pushes to **main** and pull requests.

When fixing topology, polling, persistence, or vendor-specific behavior, add a regression test for the failure mode. The suite intentionally protects subtle rules such as stale readings, FDB validation, topology inference, layout persistence, command safety, rights, and partial SNMP failures.

---

## 🏗️ Architecture

~~~text
SNMP switches / routers
          │
          ▼
   SnmpCollector + probes
          │
          ▼
 ┌─────────────────────────┐
 │ topology inference      │
 │ LLDP / FDB / LACP       │
 │ STP / VLAN / ARP        │
 └────────────┬────────────┘
              │
              ▼
        TopologyState
          │       │
          ▼       ▼
       SQLite   FastAPI ◄── sign-in, rights table
          │       │
          └───┬───┘
              ▼
        Web interface
~~~

The collector, topology engine, persistence layer, API, and UI are intentionally separated. A slow or partially responding switch should not make the rest of the network disappear.

See **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** for the data flow, the rights table and optimization boundaries.

---

## 🔌 REST API

| Method | Endpoint | Purpose | Role |
|---|---|---|---|
| GET | **/api/health** | Liveness: status and version, nothing else | none |
| POST | **/api/auth/login**, **/api/auth/totp** | Sign in: password, then the code | none |
| GET | **/api/status** | Scan, service, and resource state | viewer |
| GET | **/api/topology** | Current topology and live device state | viewer |
| GET | **/api/switch/{ip}/ports** | Port status, rates, errors, LLDP, LAG, loop state | viewer |
| GET | **/api/stp** | STP state and network verdict | viewer |
| GET | **/api/alarms** | Active or historical alarms | viewer |
| POST | **/api/alarms/{id}/clear** | Clear an active alarm | user |
| GET | **/api/journal** | Event history, with who did what | viewer |
| GET | **/api/search?q=...** | Device search | viewer |
| GET/PATCH/DELETE | **/api/layout** | Shared layout management | viewer / user / admin |
| GET | **/api/node-menu?id=...** | Resolve context-menu actions | viewer |
| POST | **/api/actions** | Start ping or traceroute jobs | user |
| POST | **/api/scan** | Poll the network now | user |
| … | **/api/users** | Accounts | admin |

The role applies once sign-in is on. Every route, its role and its
answers: **[docs/API.md](docs/API.md)**.

---

## 🧭 Project structure

~~~text
MoonLan/
├── run.py
├── config.example.yaml
├── requirements.txt
├── moonlan/
│   ├── snmp_collector.py   # SNMP polling and device data
│   ├── topology.py         # topology inference and map state
│   ├── counters.py         # traffic/error counters and rates
│   ├── corruption.py       # damaged copies of a real MAC on one port
│   ├── lldp.py             # LLDP processing
│   ├── stp.py              # STP/RSTP processing
│   ├── loopdetect.py       # vendor loop-detection profiles
│   ├── alarms.py           # stateful alarm engine
│   ├── notify.py           # notifications
│   ├── db.py               # SQLite persistence
│   ├── pinger.py           # continuous ping monitoring
│   ├── probes.py           # safe on-demand ping/traceroute
│   ├── menu.py             # context-menu URL validation
│   ├── auth.py             # scrypt passwords, TOTP, recovery codes
│   ├── access.py           # the rights table: every route and its role
│   ├── signin.py           # sessions, the guard on every request, accounts API
│   ├── users.py            # python -m moonlan.users
│   ├── anonymize.py        # rewriting a diag report so it can be shared
│   ├── diag.py             # diagnostics CLI
│   ├── demo.py             # the demo network
│   └── server.py           # FastAPI application
├── tests/
├── web/                    # HTML/CSS/JS, English and Russian
└── docs/
~~~

---

## 📚 Documentation

- **What MoonLan does** — [docs/FEATURES.md](docs/FEATURES.md)
- **Configuration** — [docs/CONFIGURATION.md](docs/CONFIGURATION.md)
- **Users and sign-in** — [docs/SIGN-IN.md](docs/SIGN-IN.md)
- **Operations & troubleshooting** — [docs/OPERATIONS.md](docs/OPERATIONS.md)
- **Diagnostics** — [docs/DIAGNOSTICS.md](docs/DIAGNOSTICS.md)
- **LLDP, STP and loop detection** — [docs/NETWORK.md](docs/NETWORK.md)
- **Health checks** — [docs/HEALTHCHECK.md](docs/HEALTHCHECK.md)
- **Architecture** — [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- **API reference** — [docs/API.md](docs/API.md)
- **Roadmap** (in Russian) — [docs/ROADMAP.md](docs/ROADMAP.md)
- **Contributing** — [CONTRIBUTING.md](CONTRIBUTING.md)
- **Security policy** — [SECURITY.md](SECURITY.md)
- **Changelog** — [CHANGELOG.md](CHANGELOG.md) ([in Russian](CHANGELOG_RU.md))

---

## 🛡️ Security

MoonLan is intended for trusted/self-hosted network environments.

- Treat SNMP v2c communities as secrets.
- Do not expose the service directly to an untrusted network.
- Create an administrator to put the map behind a sign-in; until then it is open to everyone who can reach it.
- Over plain HTTP, passwords and session cookies cross the network in clear text (HTTPS: v0.7.6).
- On-demand actions accept MoonLan node IDs, not arbitrary addresses, and run without a shell.
- Configurable context-menu URLs are scheme-validated and values are URL-encoded.
- Read **[SECURITY.md](SECURITY.md)** before exposing MoonLan beyond a lab or trusted LAN.

---

## 🤝 Contributing

Small, focused changes are preferred over broad rewrites.

1. Reproduce the issue.
2. Add a regression test.
3. Preserve existing public APIs/class names unless a breaking change is intentional.
4. Run the full test suite.
5. Update documentation and changelog when behavior changes.
6. Use a clear Conventional Commit-style message.

See **[CONTRIBUTING.md](CONTRIBUTING.md)**.

---

## 🗺️ Roadmap

| Version | |
|---|---|
| v0.7.5 ✓ | One row of controls; documentation from the community |
| v0.7.6 | HTTPS (TLS of its own or behind a reverse proxy) and passkeys |
| v0.8 | Export to PDF and Draw.io, MAC address info import |
| v0.9 | Windows computer inventory (WMI/WinRM) |

Every version so far: [CHANGELOG.md](CHANGELOG.md).

---

## 📜 License

MIT — see **[LICENSE](LICENSE)**.

### Why MoonLan?

Network maps are useful only when they distinguish **observation from inference**. MoonLan deliberately keeps that distinction visible: a measured cable, an FDB-derived relationship, an approximate trunk placement, and a stale reading should never look like the same thing.

## Acknowledgments

AI-Assisted Development: Built with [Claude Code](https://github.com/anthropics/claude-code) by [Anthropic](https://www.anthropic.com/)
