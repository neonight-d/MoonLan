# Users and sign-in

Since v0.7.4 MoonLan can ask who is looking. This page is the whole
story; the API side is in [API.md](API.md), the security picture in
[SECURITY.md](../SECURITY.md).

Until the first administrator exists, MoonLan works exactly as before:
the map is open to everyone who can reach it, and the header says so,
with the command that ends it. Upgrading changes nothing by itself.

## The first administrator

It is created on the server, not in the browser. A setup page would
make an administrator of whoever opened the map first after the
install — in a school, not necessarily the teacher. Whoever has a
shell on the MoonLan machine is its administrator already.

```bash
cd /opt/moonlan                 # where MoonLan runs, with its config.yaml
.venv/bin/python -m moonlan.users add anton --role admin
```

The password is asked for twice, and never taken from the command line:
that would stay in the shell history and be visible in `ps`. At least
ten characters and not the name — no rules about capitals and symbols,
which make passwords shorter and more predictable, not stronger.

Sign-in switches on at once, without a restart: every open map asks to
sign in at its next refresh. It never switches off by accident — only
with no enabled administrator left, and the last one cannot be deleted,
disabled or demoted.

## Roles

| | Viewer | User | Administrator |
|---|---|---|---|
| Map, cards, ports, STP, journal, alarms — looking | ✓ | ✓ | ✓ |
| Search, copying IP/MAC, links (web, SSH, RDP, your own) | ✓ | ✓ | ✓ |
| Ping and traceroute from the server | — | ✓ | ✓ |
| Rescan network | — | ✓ | ✓ |
| Pin, release, arrange mode, "Remember the places around it" | — | ✓ | ✓ |
| Clear an alarm, mark a host monitored | — | ✓ | ✓ |
| Reset layout | — | — | ✓ |
| Users and roles | — | — | ✓ |

Links open on the viewer's own computer and the server does nothing,
so everyone has them. Even a ping is the server acting on somebody's
request, so a viewer has none of it. Reset layout wipes the shared
picture for everybody at once. What a role may not do is greyed out
with the role it needs — a viewer who does not see a button would
conclude the function does not exist; a click, `P` or a drag of a
pinned node says why. A ping result is seen by whoever started it and
by administrators.

The rights live in one table, `moonlan/access.py`, route by route; a
route missing from it is for administrators only, and a test fails on
any route of the app the table does not name. Changing requests must
come from the map's own page (`Origin`), and the session cookie is
`SameSite=Strict`.

## The second factor (TOTP)

Required for administrators: signed in with a password alone, an
administrator can do nothing but bind it. Optional for everyone else.
Standard TOTP — RFC 6238, HMAC-SHA1, six digits, thirty seconds — which
every authenticator app accepts, and so does an OATH hardware key. A
clock half a minute off still works; the same code never works twice.

Bind it **on the server**, where the secret never crosses the network:

```bash
.venv/bin/python -m moonlan.users totp anton
```

It prints the base32 secret, the `otpauth://` line and eight recovery
codes, and offers to check a code. Or **in the browser**: the name in
the header → "Turn on TOTP" shows a QR code, the secret and the
`otpauth://` line, and binds only once a code from it comes back with
the password — a mistake while scanning locks nobody out. Over plain
HTTP that secret crosses the network in clear text, which the page says.

To keep the secret on a hardware key rather than a phone, write the
`otpauth://` line to it:

```bash
ykman oath accounts uri "otpauth://totp/MoonLan:anton?secret=…&issuer=MoonLan&algorithm=SHA1&digits=6&period=30"
```

`ykman` and Yubico Authenticator see a key only when it is flashed with
a USB identifier they recognise — an RS-Key on an RP2350, say, with
firmware that presents itself as a compatible key. Otherwise use
whatever the key itself offers to load a TOTP account: the parameters
are the defaults every OATH implementation takes.

**Recovery codes**: eight, shown once when TOTP is bound (in the browser
or by `totp`); each works once instead of a code, for a lost phone or
key. Only their hashes are kept. The account page says how many are
left; binding again gives a new set.

## Getting back in

From the server's shell, with the service running (the database is in
WAL mode and the service reads accounts from it on every request):

| Command | What it does |
|---|---|
| `python -m moonlan.users list` | every account: role, state, TOTP, recovery codes left, last sign-in, sessions |
| `… passwd <name> [--temporary]` | a new password; `--temporary` asks for another at the next sign-in |
| `… role <name> viewer\|user\|admin` | change the role |
| `… disable <name>` / `enable <name>` | no sign-in until enabled again |
| `… reset-totp <name>` | remove the second factor and the recovery codes: a lost phone and no codes |
| `… totp <name>` | bind TOTP here and print the secret |
| `… unlock <name>` | lift a lock after wrong passwords |
| `… delete <name>` | delete the account (asks; `--yes` does not) |

A new password, a new role, disabling, deleting and resetting TOTP sign
the account out everywhere. Every action goes into the journal as done
from the console. Run the command where MoonLan runs, with the same
`config.yaml` (or `MOONLAN_CONFIG`): it prints which database it works
on.

In the browser, **Users** in the Actions menu (⋯) does the same for an
administrator: create an account with a temporary password (generated,
shown once; the person sets their own at the first sign-in), change a
role, disable and enable, a new temporary password, reset TOTP, unlock,
sign out everywhere, delete — with the same rule about the last
administrator. Everyone's own page — the name in the header — changes
the password, binds TOTP, says how many recovery codes are left, and
signs out.

## Sessions and protection

- A session ends after `auth.session_idle_hours` (12) without a
  request and after `auth.session_max_days` (30) in any case. An open
  map refreshes itself, and that counts: a wall monitor signed in as a
  viewer stays signed in for weeks. The cookie is `HttpOnly`,
  `SameSite=Strict`, `Path=/`; the database keeps only its SHA-256, so
  a copy of the database signs nobody in. A session that ends while the
  page is open brings the sign-in form up over the map, and the map
  carries on where it was.
- A wrong name and a wrong password get the same answer in the same
  time. After five failures in a row from one address every next
  attempt waits twice as long, up to a minute. Ten in a row lock the
  account for fifteen minutes — not for good, or anybody could lock the
  administrator out by typing the name; `unlock` lifts it early. Every
  failure is a log line with the address; sign-in, sign-out and locks
  are in the journal.

## What sign-in over HTTP protects against

MoonLan speaks plain HTTP so far. Signing in keeps out somebody at a
computer left open, a pupil who found the address, and a guessed or
leaked password — that is what TOTP is for. It does **not** protect
against somebody who can read the traffic in the same network segment:
the password and the session cookie cross the network in clear text,
and a session read off the wire works until it ends. The header of a
signed-in user says so for as long as it is true, and so does the log.
v0.7.6 closes it: HTTPS — TLS of its own or behind a reverse proxy —
and passkeys (WebAuthn), which browsers offer only on a secure page.

`python -m moonlan.diag --config` shows the state of sign-in: on or
off, accounts per role, administrators without TOTP (there should be
none), accounts locked right now, HTTP. In demo mode, whose database
lives in memory, the accounts are kept in `demo-users.db` beside the
configured database: `MOONLAN_DEMO=1 python -m moonlan.users add …`.

