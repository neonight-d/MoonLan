# MoonLan Configuration Guide

The reference configuration is **config.example.yaml**. Copy it to a private config file and keep real SNMP communities and notification credentials out of Git.

## Configuration sources

MoonLan supports the MOONLAN_CONFIG environment variable for selecting the configuration file.

~~~bash
export MOONLAN_CONFIG=/absolute/path/to/config.yaml
~~~

Relative paths are resolved from the configuration file location where applicable. Startup logs show the effective configuration and paths.

## Main sections

### listen

Controls the web server bind address and port.

### snmp

Global SNMP defaults. Important controls include timeout, retries, retry behavior after broken walks, per-host poll budgets, and dead-OID suppression.

### switches

The switch inventory. The classic form is a list of IP addresses. Per-device SNMP settings can override global values without changing the existing list format.

Typical per-device overrides include community, timeout, retries, retries_on_break, and host_budget_seconds.

### routers

Devices whose ARP tables are used to associate IP addresses with MAC addresses. This is especially useful for hosts that are visible in routing infrastructure but not directly visible on a switch port.

### context_menu

Controls node-menu actions and operator-defined links. URLs are validated against the configured scheme allowlist and placeholder values are URL-encoded.

Built-in actions include web, SSH, RDP, ping, and traceroute where the required address/tool is available.

### loop_detection

Enables vendor-specific loop-detection profiles. Built-in profiles are selected from observed sysObjectID values; additional profiles can be added without changing the core topology code.

Use the diagnostic command first when investigating a new model:

~~~bash
python -m moonlan.diag --loop
~~~

### thresholds

Contains topology, counter, stale-host, corruption, flapping, and alarm thresholds. Keep changes targeted: raising every threshold can hide a real fault.

### notifications

Notification destinations and anti-spam timing. Supported channels include Email, Telegram, and Syslog.

### alarm_notify

Controls which alarm types are sent to which notification channels and can suppress noisy categories without disabling the underlying alarm engine.

## Recommended deployment pattern

1. Start with demo mode.
2. Add one or two switches.
3. Verify SNMP with diagnostics.
4. Add routers for ARP enrichment if needed.
5. Tune thresholds only after observing normal traffic.
6. Enable notifications last.

## Secrets

Do not commit:

- SNMP community strings;
- SMTP passwords;
- Telegram bot tokens;
- private management URLs containing credentials.

Use filesystem permissions and environment/service-manager secret handling appropriate to your deployment.
