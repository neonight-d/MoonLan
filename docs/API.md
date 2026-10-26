# MoonLan API

The web interface is a client of this API like any other. Everything is
JSON over HTTP on `listen.host:listen.port` (by default `0.0.0.0:8080`),
or over HTTPS with `listen.tls_cert` — see [HTTPS.md](HTTPS.md).

## Signing in and rights

Until an enabled administrator exists, sign-in is off and every route
except account management is open, as it was before v0.7.4. The first
administrator is created on the server with
`python -m moonlan.users add <name> --role admin` (see
[SIGN-IN.md](SIGN-IN.md)); from the next request on, every route needs
the role written for it in `moonlan/access.py`.

- **401** `{"error": "sign_in_required"}` — no session, or one that
  has ended.
- **403** `{"error": "forbidden", "need": "user", "role": "viewer"}` —
  signed in, but the route needs another role.
- **403** `{"error": "step_required", "step": "password" | "totp"}` —
  signed in with something to do first: a new password after a
  temporary one, or (an administrator) binding a second factor.
- **403** `{"error": "bad_origin"}` — a changing request (anything but
  `GET`/`HEAD`) must carry an `Origin` equal to the map's own address,
  or to `listen.public_url`. Browsers send one; a script must send it
  too.
- **403** `{"error": "https_required", "url": "https://…/"}` — a
  password sent over plain HTTP while `listen.public_url` is https.
- **403** `{"error": "sign_in_off"}` — account management and one's own
  settings while sign-in is not set up.

The session is a cookie, `moonlan_session`: `HttpOnly`,
`SameSite=Strict`, `Path=/`; over HTTPS it is
`__Host-moonlan_session`, also `Secure`. It ends after
`auth.session_idle_hours` without a request and after
`auth.session_max_days` in any case.

`python -m moonlan.diag`, run on the server, reads the token the
service writes next to its database at every start
(`<db_path>.console`, mode 0600) and sends it as the
`X-MoonLan-Console` header: a viewer that may only `GET`.

### Routes and roles

Roles, from least to most: `public` — no sign-in; `signed_in` — any
role, one's own session and settings; `viewer`; `user`; `admin`;
`accounts` — an administrator, and never while sign-in is off. A route
missing from the table in the code is for administrators only.

This table is checked against `moonlan/access.py` by
`tests/test_docs.py`: a route added to one and not the other fails the
tests.

<!-- routes:begin -->
| Method | Route | Role |
|---|---|---|
| GET | `/` | public |
| GET | `/index.html` | public |
| GET | `/{file}` (the web interface's files) | public |
| GET | `/api/health` | public |
| GET | `/api/auth/me` | public |
| POST | `/api/auth/login` | public |
| POST | `/api/auth/totp` | public |
| POST | `/api/auth/passkey/begin` | public |
| POST | `/api/auth/passkey/finish` | public |
| POST | `/api/auth/logout` | signed_in |
| POST | `/api/auth/password` | signed_in |
| POST | `/api/auth/totp/setup` | signed_in |
| POST | `/api/auth/totp/confirm` | signed_in |
| POST | `/api/auth/passkeys/begin` | signed_in |
| POST | `/api/auth/passkeys/finish` | signed_in |
| DELETE | `/api/auth/passkeys/{key_id}` | signed_in |
| GET | `/api/topology` | viewer |
| GET | `/api/switch/{ip}/ports` | viewer |
| GET | `/api/stp` | viewer |
| GET | `/api/alarms` | viewer |
| GET | `/api/journal` | viewer |
| GET | `/api/node-menu` | viewer |
| GET | `/api/actions` | viewer |
| GET | `/api/layout` | viewer |
| GET | `/api/notes` | viewer |
| GET | `/api/notes/export` | viewer |
| GET | `/api/search` | viewer |
| GET | `/api/skipped-oids` | viewer |
| GET | `/api/polling` | viewer |
| GET | `/api/status` | viewer |
| GET | `/api/about` | viewer |
| GET | `/api/about/license` | viewer |
| POST | `/api/actions` | user |
| GET | `/api/actions/{job_id}` | user |
| POST | `/api/alarms/{alarm_id}/clear` | user |
| PATCH | `/api/host/{mac}` | user |
| PATCH | `/api/layout` | user |
| PATCH | `/api/layout/{node_id:path}` | user |
| DELETE | `/api/layout/{node_id:path}` | user |
| PUT | `/api/notes/{node_id:path}` | user |
| DELETE | `/api/notes/{node_id:path}` | user |
| POST | `/api/notes/{node_id:path}/move` | user |
| POST | `/api/scan` | user |
| PUT | `/api/layout` | admin |
| DELETE | `/api/layout` | admin |
| POST | `/api/notes/import` | admin |
| GET | `/api/users` | accounts |
| POST | `/api/users` | accounts |
| PATCH | `/api/users/{name}` | accounts |
| POST | `/api/users/{name}/password` | accounts |
| POST | `/api/users/{name}/reset-totp` | accounts |
| DELETE | `/api/users/{name}/passkeys/{key_id}` | accounts |
| POST | `/api/users/{name}/reset-passkeys` | accounts |
| POST | `/api/users/{name}/unlock` | accounts |
| POST | `/api/users/{name}/logout` | accounts |
| DELETE | `/api/users/{name}` | accounts |
| GET | `/openapi.json` | admin |
| GET | `/docs` | admin |
| GET | `/docs/oauth2-redirect` | admin |
| GET | `/redoc` | admin |
<!-- routes:end -->

## Health

### GET /api/health

Without sign-in, for a service manager, a reverse proxy or an outside
monitor. It says the web process is up and which version it is — and
nothing about the network:

~~~json
{"status": "ok", "version": "0.7.5"}
~~~

Scan progress, the last error and the switches that ran out of time are
in `/api/status`, which needs a viewer. See [HEALTHCHECK.md](HEALTHCHECK.md).

## About

### GET /api/about

What the page's "About" shows: the version, the build — the short hash
of the commit the service was started from, read out of `.git` at
startup without running `git`; `null` when it runs from no clone — the
copyright line of `LICENSE`, the components with the versions
**installed** (`null` for one that is not: `fido2` is optional) and
their licences, and the IEEE register of makers.

~~~json
{
  "version": "0.8.1",
  "build": "317bd0c",
  "copyright": "2026 neonight-d",
  "components": [
    {"name": "FastAPI", "version": "0.141.1", "license": "MIT", "where": "server"},
    {"name": "fido2", "version": null, "license": "BSD-2-Clause, Apache-2.0, MPL-2.0", "where": "server"},
    {"name": "vis-network", "version": "9.1.9", "license": "Apache-2.0 OR MIT", "where": "browser"}
  ],
  "ieee": {"loaded": true, "date": 1792050000.0, "command": "python -m moonlan.oui update"},
  "server": {
    "python": "3.13.5", "os": "Linux, Debian GNU/Linux 13 (trixie)",
    "started_at": 1792750000.0, "uptime": 7260,
    "sign_in": true, "https": "own"
  }
}
~~~

`ieee.date` is when the oldest of the register's files was written.
`server` is in the answer to an administrator only — and to everybody
while sign-in is off: the system and its distribution are something to
look exploits up for, not something a reader of the map needs. `https`
is `own` (MoonLan serves it), `proxy` (a trusted proxy in front) or
`off`.

### GET /api/about/license

The `LICENSE` file as `text/plain`: MIT asks for the text to go with
every copy. `404 {"error": "no_license"}` when the service runs without
it.

## Signing in

### GET /api/auth/me

`{"sign_in": false, "create_admin": "…"}` while sign-in is off; the
signed-in account — `name`, `role`, `step` (`null`, `"password"` or
`"totp"`), `totp`, `recovery_left`, `keys` (each: `id`, `number`,
`label`, `passwordless`, `unconfirmed` — marked "without a password"
before v0.7.7 and never confirmed by the key — `created_at`,
`last_used`) — or **401**. Both
carry `passkeys`: `{"why": null | "no_public_url" | "public_url_ip" |
"public_url_http" | "no_fido2", "origin": "https://…" | null}` — whether
the server can take a key, and the address keys are bound to; and
`https_url` when the page came over plain HTTP although the public
address is https.

### POST /api/auth/login

JSON, not a form: `{"name": "…", "password": "…"}`.

- `{"status": "signed_in"}` and the session cookie;
- `{"status": "code_required", "ticket": "…", "methods": […]}` when
  the account has TOTP or a key — the ticket lives five minutes and
  opens nothing but the next step; `methods` says what that step can
  be: `"totp"`, `"passkey"` (while the server can check keys),
  `"recovery"`;
- **401** `invalid_credentials` — the same for a wrong name and a wrong
  password;
- **403** `disabled` — said only when the password was right;
- **423** `locked` with `retry_after` — ten failures in a row lock the
  account for fifteen minutes;
- **429** `too_many_attempts` with `retry_after` and a `Retry-After`
  header — after five failures in a row from one address;
- **409** `sign_in_off`.

### POST /api/auth/totp

`{"ticket": "…", "code": "…"}` — a six-digit TOTP code or one of the
recovery codes (which work for an account whose second factor is a key
too). Errors: `invalid_code`, `code_used` (a code works once),
`ticket_expired`, `locked`, `too_many_attempts`.

### POST /api/auth/passkey/begin and /api/auth/passkey/finish

Signing in with a key. `begin` takes `{"ticket": "…"}` for the second
step after the password — the options then list that account's keys,
user verification `discouraged` — or `{}` to sign in with a key alone:
`allowCredentials` empty (the key offers what it keeps for this host)
and user verification `required`. It answers `{"request": "…",
"options": {…}}` (`PublicKeyCredentialRequestOptions` in JSON form).
`finish` takes `{"request": "…", "credential": {…}}` and answers like
`/api/auth/login`: `{"status": "signed_in"}` and the session cookie.

Checked: the challenge (once, five minutes), the origin (exactly
`listen.public_url`), the RP ID hash, the signature, whose key it is
(its user handle, or the ticket's account), user verification when
alone, and the signature counter — a counter that goes back while both
it and the stored one are above 0 is refused and raises
`passkey_clone_suspected` (critical); a key that always says 0 is
taken. Failures count like wrong passwords: per address, and per
account once the key's owner is known. Errors (**401** unless noted):
`challenge_expired`, `key_refused`, `key_unknown`,
`no_user_verification`, `key_second_only` (the key was added to work
after the password only), `ticket_expired`, `locked` (**423**),
`too_many_attempts` (**429**), `disabled` (**403**), `passkeys_off`
(**409**).

### POST /api/auth/logout

Ends this session and deletes the cookie.

### POST /api/auth/password

`{"current": "…", "new": "…"}`. The current password is asked for; the
account's other sessions are closed, this one stays. Errors:
`wrong_password`, `too_short`, `too_long`, `same_as_name`,
`same_as_current`.

### POST /api/auth/totp/setup and /api/auth/totp/confirm

`setup` answers `{"secret": "…", "uri": "otpauth://…"}` and binds
nothing. `confirm` takes `{"code": "…", "password": "…"}`, binds the
secret once the code is right, and answers `{"recovery": [8 codes]}` —
the only time they are shown.

### POST /api/auth/passkeys/begin and /api/auth/passkeys/finish

Adding a key to one's own account. `begin` takes `{"password": "…",
"passwordless": true | false}` and answers `{"request": "…", "options":
{…}}` — `options` is `PublicKeyCredentialCreationOptions` in JSON form
(bytes in base64url) for `navigator.credentials.create()`. With
`passwordless` true, `residentKey` and `userVerification` are
`required` — the key must keep the sign-in on itself and ask for its
PIN or a finger, or refuse; otherwise both are `preferred`. `finish`
takes `{"request": "…", "credential": {…}, "label": "…"}` —
`credential` as `PublicKeyCredential.toJSON()` makes it — and answers
`{"status": "added", "number": n, "passwordless": …, "recovery": null |
[8 codes]}`. Asked for without a password, a key that answers without
user verification, or with `credProps.rk` false, is refused with
`cannot_passwordless`; one whose browser sends no `credProps` is taken
as keeping the sign-in, which is what `required` means. Asked for as a
second factor, a missing `credProps` is stored as unknown. The first
key of an account without recovery codes brings them, shown this once.

`DELETE /api/auth/passkeys/{key_id}` removes one of one's own keys and
closes the account's other sessions; an administrator's last second
factor is refused with **409** `last_factor`.

A request is good once, for five minutes, for the account that asked.
Errors: `wrong_password`, `too_many_keys` (ten at most),
`passkeys_off` (**409**, with `why`), `challenge_expired`,
`key_refused` (the origin, the RP ID, the signature or the algorithm
was wrong), `cannot_passwordless`, `key_exists`, `https_required`.

## Accounts (administrators)

### GET /api/users

Every account: `name`, `role`, `disabled`, `totp`, `recovery_left`,
`must_change`, `locked_until`, `last_login`, `created_at`, `sessions`,
`passkeys` (as in `/api/auth/me`'s `keys`). No hashes, no secrets, no
key material.

### POST /api/users

`{"name": "…", "role": "viewer" | "user" | "admin"}` → `{"name",
"role", "password"}`: a generated temporary password, shown once; the
person sets their own at the first sign-in.

### PATCH /api/users/{name}

`{"role": "…"}` and/or `{"disabled": true | false}`.

### POST /api/users/{name}/password, /reset-totp, /unlock, /logout

A new temporary password (answered once), remove the second factor and
the recovery codes, lift a lock, sign the account out everywhere.

### DELETE /api/users/{name}/passkeys/{key_id}, POST /api/users/{name}/reset-passkeys

Remove one key of an account (not an administrator's last second
factor: **409** `last_factor`), or all of them — `{"removed": n,
"sessions_closed": n}`; an administrator without TOTP then binds a new
second factor at the next sign-in. Both close the account's sessions.

### DELETE /api/users/{name}

Deletes the account.

The last enabled administrator cannot be demoted, disabled or deleted:
**409** `last_admin`. Other refusals: **404** `unknown`,
`unknown_key`, **409** `exists`, `last_factor`, **400** `bad_name`,
`bad_role`. A new password, a new role,
disabling, deleting and resetting TOTP close the account's sessions.

## Operational status

### GET /api/status

Version, demo mode, device counts, scan progress, the last error,
layout timestamps and process resource hints (`open_fds`, `rss_kb`).

### GET /api/polling

When each switch was last polled in full, how long it took, and its
budget — what `diag --config` prints.

### GET /api/skipped-oids

The OIDs paused after answering nothing but timeouts — what
`diag --skipped` prints.

## Topology

### GET /api/topology

Switches, links, hosts, unlocated devices, pseudo-switches,
LLDP-discovered bridges, VLAN names, and current monitoring fields.

A link joins two switches (`a`, `b`) at `a_port` and `b_port`. Two
cables between the same two switches without a LAG are two links: the
one carrying traffic, and beside it the other with `parallel: true` and
its own ports and STP state (`stp_blocking`).

A switch's `port_prefix` is what all its physical ports have before
their number ("GigabitEthernet1/0/" for "GigabitEthernet1/0/3"), or
`null` when they have no common beginning or a name ends in no number.

Hosts, switches, and bridges whose chassis id is a MAC carry who made
them, from the IEEE register: `vendor` — the name exactly as the
register has it, or `null` — and `vendor_status`, which says why it is
`null`: `found`, `unregistered` (a global address the register does not
list), `local` (locally administered: a random or hand-set address,
which has no maker), `group`, or `no_registry` (the register is not
loaded — `python -m moonlan.oui update`).

### GET /api/switch/{ip}/ports

Port status, speed, traffic rates, errors, discards, LAG membership,
LLDP information, link changes, and loop-detection state.

## Monitoring

### GET /api/stp

Per-switch STP information and the network-level verdict.

Why a switch is said to run a tree or not comes three ways: `reason`,
an English sentence for the log and `diag --stp`; and, for the page to
word in its own language, `reason_code` — which branch of the verdict
decided — and `reason_params`, the numbers in it:

| `reason_code` | `reason_params` |
|---|---|
| `not_supported` | — |
| `foreign_root` | `root`, `cost` |
| `reports_nothing` | — |
| `no_port_enabled` | — |
| `all_disabled` | — |
| `no_root` | `root`, `cost` |
| `never_converged` | `changes`, `since`, `uptime` (seconds) |
| `converged` | `changes`, `since`, `uptime` (seconds) |
| `root_confirmed` | `count`, `neighbours` (addresses) |

A code the page does not know is shown as `reason`.

### GET /api/alarms

Query parameters: `active=1` for active alarms, `active=0` for
cleared ones, and `limit=1..500`.

### POST /api/alarms/{id}/clear

Manually clears an active alarm; the journal records who did.

### GET /api/journal

Recent journal events, newest first; `limit` up to 1000. Each carries
`user`: who did it, `"@console"` for `python -m moonlan.users`, empty
for what MoonLan did itself. An event about a device the IEEE register
knows carries its `vendor`, looked up when asked.

`details` is data where the page words the event itself: since v0.8.2 a
`link_dropped` carries `{"a", "a_port", "b", "b_port", "source",
"reason"}` and, by the reason, `behind` (LLDP puts `b` behind that
switch) or `cycle` (the ring it was the weakest edge of). An entry
written before v0.8.2 is the English line it was then.

## Inventory

### GET /api/search?q=...

Searches known devices by name, IP, MAC and maker: `mercusys` finds
every MERCUSYS device. A substring, in any letter case.

### PATCH /api/host/{mac}

Changes whether a host is monitored for host-down alarms:

~~~json
{"monitored": true}
~~~

### POST /api/scan

Starts a poll of every switch now.

## Layout

### GET /api/layout

Pinned positions, the offsets recorded round them (`anchor`), the last
reset (`cleared_at`), the nodes laid out from the pins, and positions
with no node on the map.

### PUT /api/layout

Stores positions. `only_new` lets older clients avoid overwriting
positions pinned from another browser.

### PATCH /api/layout

Pins or unpins nodes in one action (one journal entry);
`neighbours` records the offsets round a pinned node.

### DELETE /api/layout

Resets the shared layout for everybody.

## Names and notes

A node's **name** (`label`: one line, at most 40 characters, on the
map) and **note** (`note`: free text with line breaks, at most 2000
characters, in its card), kept by the node id the layout uses:
`sw:<ip>`, `host:<mac>` (a device off the map, `unloc:<mac>`, has the
same note), `bridge:<chassis id>`, `pseudo:<switch>:<port>`,
`external:<switch>:<port>`, `trunk:<switch>:<port>`. An offline group
(`offline:`) has none: the server refuses it with `offline_group`.

In `/api/topology` every node with a name or a note has
`note: {label, note, source, updated_at, updated_by}`; `source` is
`manual` or `import`. A node without one has no `note` field.

An offline group stands in for the switch without SNMP of its port
(v0.8.1): when every device of a room is off, the switch is not drawn
and the group is, so the group carries that switch's note, with
`note_key: "pseudo:<switch>:<port>"` — where it is kept, and where a
PUT through the group writes it. Not when the switch is drawn too:
then the name is on it.

### GET /api/notes

Every note, with `state`: `present`, `missing` — its node has not
been on the map for `missing_scans` scans in a row (dated by
`missing_since`) — or `not_found`, an import whose device has not been
seen yet. MoonLan never deletes a note by itself.

### PUT /api/notes/{node_id}

`{"label": "...", "note": "..."}`. Both are trimmed; both empty deletes
the note. Refused: a name with a line break or another control
character (`label_control`), over 40 characters (`label_long`), a
note over 2000 (`note_long`), an offline group, and a node the map
does not have (`no_node`, 404) — unless the note is already there.
Journalled as `note_changed`.

### DELETE /api/notes/{node_id}

Deletes the note; `note_changed` in the journal.

### POST /api/notes/import

The body is a CSV — `mac;label;note`, see [OPERATIONS.md](OPERATIONS.md#names-and-notes-from-a-file).
With `?dry_run=1` nothing is written and the answer says what would
be: `written` (`on_map`, `not_found`), `skipped_manual` (a note written
by hand is never overwritten), `errors` and `warnings`, each with its
line. Journalled once as `notes_imported`. Administrators, and the
server's console (`python -m moonlan.notes import`).

### GET /api/notes/export

The notes of nodes that have a MAC, as the CSV the import takes
(UTF-8 with a BOM, `;`). `?preview=1` answers `{exported, left_out}`:
notes of switches without SNMP, external networks and groups have no
MAC to be written by.

### POST /api/notes/{node_id}/move

`{"to": "<node id>", "replace": false}` puts the note on another node
on the map — the one a cable was moved to. A target with a note of its
own answers 409 `target_has_note` unless `replace` is true. Journalled
as `note_moved`, with both ids.

## Node actions

### GET /api/node-menu?id=...

What one node's menu offers: its address, MAC and name as MoonLan knows
them, links filled in, and which tools the server has.

### POST /api/actions

Starts a ping or traceroute job, by MoonLan node ids:

~~~json
{
  "action": "ping",
  "nodes": ["host:aa:bb:cc:00:00:01"]
}
~~~

The server rejects unknown ids, addresses sent in place of ids,
excessive target counts, unavailable tools and unsupported actions.

### GET /api/actions/{id}

The state and results of a job — for whoever started it and for
administrators; to anybody else it does not exist (**404**).

## Compatibility

Prefer additive response fields when extending an endpoint. Avoid
changing the meaning or type of an existing field without a documented
migration path. A new route needs a line in `moonlan/access.py` and in
the table above; the tests fail until both are there.
