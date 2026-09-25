# MoonLan API

The web interface is a client of this API like any other. Everything is
JSON over HTTP on `listen.host:listen.port` (by default `0.0.0.0:8080`).

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
  `GET`/`HEAD`) must carry an `Origin` equal to the map's own address.
  Browsers send one; a script must send it too.
- **403** `{"error": "sign_in_off"}` — account management and one's own
  settings while sign-in is not set up.

The session is a cookie, `moonlan_session`: `HttpOnly`,
`SameSite=Strict`, `Path=/`. It ends after `auth.session_idle_hours`
without a request and after `auth.session_max_days` in any case.

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
| POST | `/api/auth/logout` | signed_in |
| POST | `/api/auth/password` | signed_in |
| POST | `/api/auth/totp/setup` | signed_in |
| POST | `/api/auth/totp/confirm` | signed_in |
| GET | `/api/topology` | viewer |
| GET | `/api/switch/{ip}/ports` | viewer |
| GET | `/api/stp` | viewer |
| GET | `/api/alarms` | viewer |
| GET | `/api/journal` | viewer |
| GET | `/api/node-menu` | viewer |
| GET | `/api/actions` | viewer |
| GET | `/api/layout` | viewer |
| GET | `/api/search` | viewer |
| GET | `/api/skipped-oids` | viewer |
| GET | `/api/polling` | viewer |
| GET | `/api/status` | viewer |
| POST | `/api/actions` | user |
| GET | `/api/actions/{job_id}` | user |
| POST | `/api/alarms/{alarm_id}/clear` | user |
| PATCH | `/api/host/{mac}` | user |
| PATCH | `/api/layout` | user |
| PATCH | `/api/layout/{node_id:path}` | user |
| DELETE | `/api/layout/{node_id:path}` | user |
| POST | `/api/scan` | user |
| PUT | `/api/layout` | admin |
| DELETE | `/api/layout` | admin |
| GET | `/api/users` | accounts |
| POST | `/api/users` | accounts |
| PATCH | `/api/users/{name}` | accounts |
| POST | `/api/users/{name}/password` | accounts |
| POST | `/api/users/{name}/reset-totp` | accounts |
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

## Signing in

### GET /api/auth/me

`{"sign_in": false, "create_admin": "…"}` while sign-in is off; the
signed-in account — `name`, `role`, `step` (`null`, `"password"` or
`"totp"`), `totp`, `recovery_left` — or **401**.

### POST /api/auth/login

JSON, not a form: `{"name": "…", "password": "…"}`.

- `{"status": "signed_in"}` and the session cookie;
- `{"status": "code_required", "ticket": "…"}` when the account has
  TOTP — the ticket lives five minutes and opens nothing but the next
  step;
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
recovery codes. Errors: `invalid_code`, `code_used` (a code works once),
`ticket_expired`, `locked`, `too_many_attempts`.

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

## Accounts (administrators)

### GET /api/users

Every account: `name`, `role`, `disabled`, `totp`, `recovery_left`,
`must_change`, `locked_until`, `last_login`, `created_at`, `sessions`.
No hashes, no secrets.

### POST /api/users

`{"name": "…", "role": "viewer" | "user" | "admin"}` → `{"name",
"role", "password"}`: a generated temporary password, shown once; the
person sets their own at the first sign-in.

### PATCH /api/users/{name}

`{"role": "…"}` and/or `{"disabled": true | false}`.

### POST /api/users/{name}/password, /reset-totp, /unlock, /logout

A new temporary password (answered once), remove the second factor and
the recovery codes, lift a lock, sign the account out everywhere.

### DELETE /api/users/{name}

Deletes the account.

The last enabled administrator cannot be demoted, disabled or deleted:
**409** `last_admin`. Other refusals: **404** `unknown`, **409**
`exists`, **400** `bad_name`, `bad_role`. A new password, a new role,
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

### GET /api/switch/{ip}/ports

Port status, speed, traffic rates, errors, discards, LAG membership,
LLDP information, link changes, and loop-detection state.

## Monitoring

### GET /api/stp

Per-switch STP information and the network-level verdict.

### GET /api/alarms

Query parameters: `active=1` for active alarms, `active=0` for
cleared ones, and `limit=1..500`.

### POST /api/alarms/{id}/clear

Manually clears an active alarm; the journal records who did.

### GET /api/journal

Recent journal events, newest first; `limit` up to 1000. Each carries
`user`: who did it, `"@console"` for `python -m moonlan.users`, empty
for what MoonLan did itself.

## Inventory

### GET /api/search?q=...

Searches known devices by name, IP and MAC.

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
positions placed by another browser.

### PATCH /api/layout

Places or releases nodes in one action (one journal entry);
`neighbours` records the offsets round a pinned node.

### DELETE /api/layout

Resets the shared layout for everybody.

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
