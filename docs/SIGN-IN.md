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

An administrator needs a second factor — TOTP, or a key (next
section): signed in with a password alone and neither bound, an
administrator can do nothing but bind one. Optional for everyone
else.
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
or by `totp`) or with the first key of an account that has none; each
works once instead of a code, for a lost phone or key. Only their
hashes are kept. The account page says how many are left; binding TOTP
again gives a new set.

## Signing in with a key (passkeys, security keys)

Since v0.7.6: a hardware key (YubiKey, Token2, an RS-Key…), or the
passkey a phone or a computer keeps (Windows Hello, Android, iOS,
macOS). A key signs the page's address together with a one-time
challenge, so it cannot be typed into a fake page the way a password
or a TOTP code can.

It needs three things, and the page says which one is missing — the
"Add a key" and "Sign in with a key" buttons are greyed out with the
reason:

1. **`listen.public_url` with a host name**, e.g.
   `https://moonlan.example.local:8443`. A key is bound to a host name,
   never to an IP address, and works only at that address.
2. **HTTPS** at that address — MoonLan's own certificate or a proxy in
   front ([HTTPS.md](HTTPS.md)); browsers offer keys only on a secure
   page. `http://localhost` counts as secure, for trying it out.
3. **`fido2`** installed on the server (`pip install -r
   requirements.txt`; without it the log and the form say so).

**Adding a key**: the name in the header → "Add a key" → the password
→ touch the key (or confirm with a PIN, a finger, a face). Unticked,
"Sign in with this key without a password" makes the key a second
factor after the password, like TOTP: any key will do. Ticked, it makes
it a passkey, and that asks two things of the key. It has to **keep the
sign-in on itself** — "Sign in with a key" gives it no name to look up,
so a key that keeps nothing has nothing to offer — and it has to **ask
for its PIN or a finger**, every time. MoonLan asks for both as
required: a key that cannot do them refuses, and the page says to
untick the box. A key with no PIN set gets one offered by Windows or
the browser while it is being added; a hardware key keeps a limited
number of sign-ins, and a full one refuses too. Ten
keys at most per account; add a spare and keep it in a drawer.

**Signing in**: "Sign in with a key" — no name, no password; or the
password first and "Use the key" on the next step. Once an account has
a key, its password alone is not enough any more. A lost key: sign in
with a recovery code or TOTP, and remove it on the account page.

**A copied key.** A key counts its signatures, and each sign-in is
checked against the last count. A count that goes back means two keys
hold the same secret: the sign-in is refused, the journal records it,
and the critical alarm `passkey_clone_suspected` goes out
(`alarm_notify.passkey_clone_suspected`). Keys that do not count —
many passkeys always say 0 — are accepted.

**USB keys on Linux.** A browser on Linux reaches a USB key through
`/dev/hidraw*`, which only root can open unless a udev rule hands it
to the person at the console. systemd 244 and later (Debian 11+,
Ubuntu 20.04+) ship that rule, `60-fido-id.rules`: plug the key in
and it works. On an older system install the rules the distribution
packages (`sudo apt install libu2f-udev` on Debian 10 and Ubuntu 18.04),
or copy `70-u2f.rules` from the libfido2 repository to
`/etc/udev/rules.d/`, and run
`sudo udevadm control --reload && sudo udevadm trigger`. This
is about the computers people sign in from, not the MoonLan server.
Windows and macOS need nothing.

## Getting back in

From the server's shell, with the service running (the database is in
WAL mode and the service reads accounts from it on every request):

| Command | What it does |
|---|---|
| `python -m moonlan.users list` | every account: role, state, TOTP, keys, recovery codes left, last sign-in, sessions |
| `… passwd <name> [--temporary]` | a new password; `--temporary` asks for another at the next sign-in |
| `… role <name> viewer\|user\|admin` | change the role |
| `… disable <name>` / `enable <name>` | no sign-in until enabled again |
| `… reset-totp <name>` | remove TOTP and the recovery codes: a lost phone and no codes |
| `… totp <name>` | bind TOTP here and print the secret |
| `… passkeys <name>` | the account's keys, numbered, with what each may do and when it was last used |
| `… remove-passkey <name> <number>` | remove one key — not an administrator's last second factor |
| `… reset-passkeys <name>` | remove every key: all of them lost |
| `… unlock <name>` | lift a lock after wrong passwords |
| `… delete <name>` | delete the account (asks; `--yes` does not) |

A new password, a new role, disabling, deleting, resetting TOTP and
removing keys sign the account out everywhere. An administrator
without TOTP keeps at least one key: the last one is removed with
`reset-passkeys`, after which a new second factor is bound at the next
sign-in, before anything else. Every action goes into the journal as done
from the console. Run the command where MoonLan runs, with the same
`config.yaml` (or `MOONLAN_CONFIG`): it prints which database it works
on.

In the browser, **Users** in the Actions menu (⋯) does the same for an
administrator: create an account with a temporary password (generated,
shown once; the person sets their own at the first sign-in), change a
role, disable and enable, a new temporary password, reset TOTP, remove
a key or reset all of them, unlock, sign out everywhere, delete — with
the same rules about the last administrator and an administrator's last
second factor. Everyone's own page — the name in the header — changes
the password, binds TOTP, adds and removes keys, says how many recovery
codes are left, and signs out.

## Sessions and protection

- A session ends after `auth.session_idle_hours` (12) without a
  request and after `auth.session_max_days` (30) in any case. An open
  map refreshes itself, and that counts: a wall monitor signed in as a
  viewer stays signed in for weeks. The cookie is `HttpOnly`,
  `SameSite=Strict`, `Path=/` — and over HTTPS `Secure`, named
  `__Host-moonlan_session`; the database keeps only its SHA-256, so a
  copy of the database signs nobody in. A session that ends while the
  page is open brings the sign-in form up over the map, and the map
  carries on where it was.
- A wrong name and a wrong password get the same answer in the same
  time. After five failures in a row from one address every next
  attempt waits twice as long, up to a minute. Ten in a row lock the
  account for fifteen minutes — not for good, or anybody could lock the
  administrator out by typing the name; `unlock` lifts it early. A key
  whose answer is refused counts the same. Every failure is a log line
  with the address; sign-in (and how: password, TOTP, a key, a recovery
  code), sign-out and locks are in the journal.

## What sign-in over HTTP protects against

Over plain HTTP, signing in keeps out somebody at a computer left
open, a pupil who found the address, and a guessed or leaked
password — that is what TOTP is for. It does **not** protect
against somebody who can read the traffic in the same network segment:
the password and the session cookie cross the network in clear text,
and a session read off the wire works until it ends. The header of a
signed-in user says so for as long as it is true, and so does the log.
HTTPS closes it — TLS of MoonLan's own or a reverse proxy in front:
[HTTPS.md](HTTPS.md). With an https `listen.public_url` a password is
then not taken over plain HTTP at all.

`python -m moonlan.diag --config` shows the state of sign-in: on or
off, accounts per role, administrators without TOTP (there should be
none), accounts locked right now, HTTP. In demo mode, whose database
lives in memory, the accounts are kept in `demo-users.db` beside the
configured database: `MOONLAN_DEMO=1 python -m moonlan.users add …`.

