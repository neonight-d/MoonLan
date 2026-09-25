# Health Checks

MoonLan has a lightweight liveness endpoint for service managers,
reverse proxies and outside monitors:

~~~bash
curl -fsS http://127.0.0.1:8080/api/health
~~~

~~~json
{"status": "ok", "version": "0.7.5"}
~~~

It answers without sign-in, and that is exactly why it says nothing
else. Since v0.7.4 the map can be behind a sign-in, and a route anybody
on the network can call must not tell them anything about the network:
not the time of the last scan, not the text of the last error (which
names switches and addresses), not which switches are running late.
The endpoint does not need a successful SNMP scan either — a slow or
unreachable network must not make the web process look dead.

## Scan health

The state of polling is in `/api/status`: scan progress, the last scan
and the last error, the switches that ran out of time. It needs a
viewer once sign-in is on. `/api/polling` has the last complete poll of
every switch, and `python -m moonlan.diag --config` prints both.

A check running on the MoonLan machine itself can read them with the
token the service writes next to its database at every start (the same
one `diag` uses; readable only by the service's user):

~~~bash
curl -fsS -H "X-MoonLan-Console: $(cat /opt/moonlan/moonlan.db.console)" \
  http://127.0.0.1:8080/api/status
~~~

## Reverse proxies

Use `/api/health` for a simple HTTP 200 liveness check.

## Monitoring rule

Treat these as separate signals:

- **process alive:** `/api/health` responds;
- **network healthy:** `/api/status` — scan age, the last error, late
  switches;
- **topology current:** the scan age and the stale-data marks on the
  map.
