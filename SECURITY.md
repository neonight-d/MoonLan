# Security Policy

## Scope

MoonLan is a self-hosted network monitoring application intended for trusted management networks.

## Reporting a vulnerability

Do not publish a working exploit or sensitive configuration in a public issue. Prefer GitHub private vulnerability reporting or security advisories when available. Otherwise, contact the maintainer privately through the repository's normal contact channel.

Include the affected version or commit, endpoint/component, reproduction steps, impact, and suggested mitigation if known.

## Deployment guidance

### SNMP

MoonLan currently uses SNMP v2c. Community strings are credentials.

- restrict SNMP access to the MoonLan host;
- use read-only communities;
- never commit real configuration files or secrets;
- rotate exposed communities.

### Web service

Do not expose the MoonLan web server directly to an untrusted network. Put it behind an appropriate firewall or reverse proxy and restrict access to the management network. This stays true with sign-in on.

### Sign-in

Since v0.7.4 the map can be put behind a sign-in ([docs/SIGN-IN.md](docs/SIGN-IN.md)). Until the first administrator is created on the server with `python -m moonlan.users`, it is open to everyone who can reach it, and the header says so.

- Three roles: a viewer looks, a user may also make the server act (ping, traceroute, a scan) and change the shared picture, an administrator may also reset the layout and manage accounts. Every route's role is in one table, `moonlan/access.py`; a route missing from it is for administrators only, and a test fails on any route the table does not name.
- Passwords are scrypt hashes; sessions are random tokens of which the database keeps only a SHA-256; the cookie is `HttpOnly` and `SameSite=Strict`, and changing requests must carry the map's own `Origin`.
- TOTP is required for administrators; recovery codes are stored as hashes; a code works once.
- A wrong name and a wrong password get the same answer in the same time; failures slow an address down and lock an account for fifteen minutes after ten in a row.
- `/api/health` answers without sign-in with the status and the version only.

**Plain HTTP.** MoonLan does not speak HTTPS yet. Signing in keeps out a stranger at a computer left open, a pupil who found the address, and a guessed or leaked password. It does not protect against somebody who can read the traffic in the same network segment: the password and the session cookie cross the network in clear text, and a session read off the wire works until it ends. v0.7.6 brings HTTPS (TLS of its own or behind a reverse proxy) and passkeys. Until then, bind TOTP on the server rather than in a browser.

### On-demand actions

Ping and traceroute requests are resolved from MoonLan node identities. The API does not accept an arbitrary destination address as a command target. Commands are built as argument lists rather than shell strings, and configurable context-menu URLs are validated against an allowlist.

### Data privacy

MoonLan can store MAC addresses, IP addresses, host names, topology information, alarms, journal events, and layout data, and since v0.7.4 password hashes, TOTP secrets and session hashes. Treat the SQLite database as operational network data and as a secret. The file `<db_path>.console` next to it holds the token `diag` uses to read from the running service; it is written with mode 0600 at every start.

## Security-sensitive changes

Changes involving command execution, URL handling, SNMP credentials, API authorization, file paths, database writes, or user-controlled identifiers should include regression tests and a short security note in the pull request.
