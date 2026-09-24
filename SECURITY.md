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

Do not expose the MoonLan web server directly to an untrusted network. Put it behind an appropriate firewall or reverse proxy and restrict access to the management network.

### On-demand actions

Ping and traceroute requests are resolved from MoonLan node identities. The API does not accept an arbitrary destination address as a command target. Commands are built as argument lists rather than shell strings, and configurable context-menu URLs are validated against an allowlist.

### Data privacy

MoonLan can store MAC addresses, IP addresses, host names, topology information, alarms, journal events, and layout data. Treat the SQLite database as operational network data.

## Security-sensitive changes

Changes involving command execution, URL handling, SNMP credentials, API authorization, file paths, database writes, or user-controlled identifiers should include regression tests and a short security note in the pull request.
