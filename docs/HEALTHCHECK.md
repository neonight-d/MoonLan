# Health Checks

MoonLan exposes a lightweight liveness endpoint for service managers and reverse proxies.

~~~bash
curl -fsS http://127.0.0.1:8000/api/health
~~~

A healthy response has `status: ok` and includes the running version plus the latest scan/error timestamps.

The endpoint intentionally does **not** require a successful SNMP scan. Use `/api/status` when you need detailed operational state.

## Reverse proxies

Use `/api/health` for a simple HTTP 200 liveness check. Disable response caching because the endpoint already sends `Cache-Control: no-store`.

## Monitoring rule

Treat these as separate signals:

- **process alive:** `/api/health` responds;
- **network healthy:** inspect `/api/status` and scan fields;
- **topology current:** inspect scan age and stale-data indicators.
