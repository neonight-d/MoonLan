# MoonLan Configuration Guide

The reference configuration is **config.example.yaml**, commented in
English and Russian. Copy it to a private config file and keep real
SNMP communities and notification credentials out of Git (`config.yaml`
is in `.gitignore`).

Every key is optional: a missing one takes its default, and a
`config.yaml` written for an older version keeps working.
`python -m moonlan.diag --config` prints every setting with its
effective value and whether it came from your file or from a default,
the keys your file has that MoonLan does not know (a typo, or a setting
removed later), and the keys it lacks. Run it after every upgrade.

## Configuration sources

MoonLan reads `config.yaml` in the directory it runs in, or the file
the `MOONLAN_CONFIG` environment variable names:

~~~bash
export MOONLAN_CONFIG=/absolute/path/to/config.yaml
~~~

That is also how a second instance runs beside the service without
taking its database and port — see [OPERATIONS.md](OPERATIONS.md).

A relative `db_path` is relative to the directory the service runs in
(the systemd unit's `WorkingDirectory=`), not to the config file. The
startup log prints the absolute path of both the config file and the
database, and `MOONLAN_DEMO=1` switches demo mode on whatever the file
says.

## Main sections

### listen

The web server's bind address and port.

### snmp

Global SNMP defaults: timeout, retries, retries after a walk that broke
off, the time budget of one switch's whole poll, and dead-OID
suppression. Every switch inherits them unless its own entry says
otherwise.

### switches

The switch inventory. The classic form is a list of IP addresses. An
entry may instead be a mapping with `ip:` and any of `community`,
`timeout`, `retries`, `retries_on_break`, `host_budget_seconds`, which
then apply to that switch alone, and `web_scheme` (`http` or `https`)
for how its web interface is opened from the node menu.

### routers

Devices whose ARP tables are used to associate IP addresses with MAC
addresses. This is especially useful for hosts that are visible in
routing infrastructure but not directly visible on a switch port.

### context_menu

Node-menu actions and operator-defined links. URLs are validated
against the scheme allowlist and placeholder values are URL-encoded.
Built-in items include ping and traceroute (run on the MoonLan machine,
for the role *user* and above), the web interface, SSH, RDP and copying
the IP or MAC, where the address or tool they need is available.

### loop_detection

Vendor-specific loop-detection profiles. Built-in profiles are selected
from observed `sysObjectID` values; more can be added without changing
the core code. Investigate a new model with:

~~~bash
python -m moonlan.diag --loop
~~~

### auth

Session lifetimes after signing in (v0.7.4). Sign-in itself is not a
setting: it switches on when the first administrator is created on the
server — see [SIGN-IN.md](SIGN-IN.md).

### thresholds

Port errors, discards and utilisation, the cycles an alarm needs to
rise and clear, mass outages on one port, frame corruption, STP changes
and flapping. Keep changes targeted: raising every threshold can hide a
real fault.

### notifications

Notification destinations and anti-spam timing. Supported channels are
Email, Telegram and Syslog. Check them after configuring:

~~~bash
python -m moonlan.notify --test
~~~

### alarm_notify

Which alarm types go to which notification channels. An empty list
silences the notifications of that type without disabling the alarm
itself.

## Reference

Every key the loader reads, with its default. `tests/test_docs.py`
fails on a key the loader knows and this page does not.

### listen

| Key | Default | Meaning |
|---|---|---|
| `listen.host` | `0.0.0.0` | Address the web interface listens on. |
| `listen.port` | `8080` | Its port. |

### snmp

| Key | Default | Meaning |
|---|---|---|
| `snmp.community` | `public` | SNMP v2c community, read-only. A credential: keep it out of Git. |
| `snmp.timeout` | `5` | Seconds to wait for one reply. |
| `snmp.retries` | `2` | Re-sends of one request. |
| `snmp.retries_on_break` | `2` | Times a walk that stops mid-table is picked up again from where it stopped. |
| `snmp.host_budget_seconds` | `120` | The whole poll of one switch, start to finish. A switch past it is left out of that scan and marked late, not unreachable. |
| `snmp.dead_oid_strikes` | `3` | Walks in a row with no rows and a timeout, after which that OID is left alone on that host (0 — never). |
| `snmp.dead_oid_cooldown_scans` | `30` | For how many scans; then it is tried again. |

### Inventory and polling

| Key | Default | Meaning |
|---|---|---|
| `switches` | `[]` | Managed switches: addresses, or mappings with `ip:` and per-switch settings (see above). |
| `routers` | `[]` | Devices with an ARP table — the source of host IPs. |
| `scan_interval_minutes` | `10` | SNMP polling period (0 — only on request). |
| `ping_interval_seconds` | `60` | Ping monitoring period. |
| `counters_interval_seconds` | `60` | Port counters polling period. |
| `db_path` | `moonlan.db` | SQLite file: hosts, journal, alarms, layout, accounts. |

### Map and inventory rules

| Key | Default | Meaning |
|---|---|---|
| `unmanaged_threshold` | `3` | More hosts than this behind one port are drawn under a "switch without SNMP" node (0 — never). |
| `monitored_by_default` | `false` | `true` raises host_down for every host, not only those marked "Monitor". |
| `host_grace_hours` | `24` | How long a host stays on the map after its MAC left the switch tables. |
| `host_retention_days` | `30` | Then it is deleted from the database. |
| `offline_group_threshold` | `2` | Offline devices on one port hang off one "Offline · N" node (0 — never). |
| `trunk_group_threshold` | `3` | Devices seen only through a trunk are drawn as one "Beyond the trunk · N" node (0 — never). |
| `ip_confirm_hours` | `6` | A stale host whose IP ARP has not confirmed for this long gives the address up. |
| `stale_rate_hide_minutes` | `30` | A measured rate is shown dimmed with its age up to this, then the cell empties. |
| `layout_keep_days` | `90` | A saved position is forgotten only when it is both older than this and has no node on the map. |
| `stale_switch_scans` | `5` | Scans in a row a switch may miss its budget before its card says so and switch_stale is raised. |
| `new_host_confirm_scans` | `2` | Polls a new MAC must appear in before it becomes a device (an IP from ARP is enough). |
| `filter_suspect_macs` | `true` | Keep damaged copies of a real address off the map (`false` — draw them). |
| `place_trunk_only_hosts` | `true` | A MAC seen only on trunks is drawn on the trunk with the best claim, marked approximate. |
| `known_bridges` | `[]` | Bridges supposed to be behind an access port (chassis id or management IP): named, never alarmed on. |
| `uplink_ports` | `[]` | Ports that leave the network, as `"<switch ip>:<port name>"`: what is behind them is one "External network" node. |

### loop_detection

| Key | Default | Meaning |
|---|---|---|
| `loop_detection.enabled` | `true` | Read loop detection from the vendors' private MIBs. |
| `loop_detection.profiles` | `[]` | Profiles of your own, added to or replacing the built-in ones by name: `name`, `roots` (sysObjectID prefix → branch root), `scalars`, `columns`, `status_type`, `normal`. config.example.yaml has a commented example. |

### context_menu

| Key | Default | Meaning |
|---|---|---|
| `context_menu.max_targets` | `64` | Nodes one action may name; more is refused with the ceiling in the reason. |
| `context_menu.max_running` | `4` | Diagnostic runs at once on the server; one more is refused. |
| `context_menu.web_scheme` | `http` | How a switch's web interface is opened, unless its own entry says `web_scheme`. |
| `context_menu.allowed_schemes` | `[]` | Link schemes beyond `http`, `https`, `ssh` and `telnet` — `winbox`, say. `javascript:`, `data:` and `vbscript:` are refused even if listed. |
| `context_menu.links` | `[]` | Your own items: `label`, `url` with `{ip}`, `{mac}`, `{name}`, `{switch}`, `{port}` filled in and URL-encoded, and `applies_to` (`switch`, `host`, `group`; every node without it). |

### auth

| Key | Default | Meaning |
|---|---|---|
| `auth.session_idle_hours` | `12` | A session ends after this long without a request. An open map refreshes itself, which counts. |
| `auth.session_max_days` | `30` | …and after this long in any case. A wall monitor signed in as a viewer stays signed in for weeks. |

### thresholds

| Key | Default | Meaning |
|---|---|---|
| `thresholds.errors_per_minute` | `5` | port_errors: damaged frames only… |
| `thresholds.error_ratio_percent` | `0.01` | …and at least this share of all frames. |
| `thresholds.discards_per_minute` | `500` | port_discards (info, syslog only). |
| `thresholds.port_utilization_percent` | `90` | port_util: % of the link speed (of the total, for a LAG). |
| `thresholds.port_alarm_cycles` | `3` | Cycles over or under a threshold before a port alarm rises or clears. |
| `thresholds.mass_down_hosts` | `3` | port_hosts_down: devices of one port gone silent in one ping cycle. |
| `thresholds.corruption_hamming_bits` | `8` | How far a MAC may sit from a real one on the same port and still be its damaged copy. |
| `thresholds.corruption_macs_threshold` | `5` | Copies on one port within the flap window → port_frame_corruption. |
| `thresholds.stp_changes_per_cycle` | `3` | stp_topology_change: topology changes per scan on an operating switch. |
| `thresholds.flaps_per_window` | `4` | port_flapping: link state transitions… |
| `thresholds.flap_window_minutes` | `10` | …inside this window. |

### notifications

| Key | Default | Meaning |
|---|---|---|
| `notifications.cooldown_seconds` | `300` | Anti-spam per (alarm type, subject). |
| `notifications.flap_count` | `3` | This many raises of one subject… |
| `notifications.flap_window_seconds` | `7200` | …within this window mute its notifications… |
| `notifications.flap_quiet_seconds` | `3600` | …until it stays quiet for this long. |
| `notifications.email.enabled` | `false` | Send email. |
| `notifications.email.smtp_host` | `""` | SMTP server. |
| `notifications.email.smtp_port` | `587` | Its port. |
| `notifications.email.starttls` | `true` | STARTTLS before signing in. |
| `notifications.email.username` | `""` | SMTP account. |
| `notifications.email.password` | `""` | Its password — a secret. |
| `notifications.email.mail_from` | `""` | Sender address. |
| `notifications.email.mail_to` | `[]` | Recipients. |
| `notifications.telegram.enabled` | `false` | Send to Telegram. |
| `notifications.telegram.bot_token` | `""` | Bot API token from @BotFather — a secret. |
| `notifications.telegram.chat_ids` | `[]` | Chats to send to. |
| `notifications.syslog.enabled` | `false` | Send to syslog over UDP. |
| `notifications.syslog.host` | `127.0.0.1` | Syslog server. |
| `notifications.syslog.port` | `514` | Its port. |

### alarm_notify

The channels (`email`, `telegram`, `syslog`) each alarm type goes to.

| Key | Default |
|---|---|
| `alarm_notify.host_down` | email, telegram, syslog |
| `alarm_notify.switch_down` | email, telegram, syslog |
| `alarm_notify.switch_stale` | syslog |
| `alarm_notify.port_errors` | syslog |
| `alarm_notify.port_discards` | syslog |
| `alarm_notify.port_util` | telegram, syslog |
| `alarm_notify.new_mac` | syslog |
| `alarm_notify.port_hosts_down` | email, telegram, syslog |
| `alarm_notify.lag_degraded` | telegram, syslog |
| `alarm_notify.port_frame_corruption` | telegram, syslog |
| `alarm_notify.unmanaged_bridge_detected` | telegram, syslog |
| `alarm_notify.stp_root_changed` | telegram, syslog |
| `alarm_notify.stp_topology_change` | telegram, syslog |
| `alarm_notify.stp_fragmented` | telegram, syslog |
| `alarm_notify.port_flapping` | telegram, syslog |
| `alarm_notify.loop_detected` | email, telegram, syslog |
| `alarm_notify.loop_detection_disabled` | syslog |

## Recommended deployment pattern

1. Start with demo mode.
2. Add one or two switches.
3. Verify SNMP with diagnostics.
4. Add routers for ARP enrichment if needed.
5. Tune thresholds only after observing normal traffic.
6. Enable notifications last.
7. Create the first administrator on the server when the map should
   stop being open to everyone ([SIGN-IN.md](SIGN-IN.md)).

## Secrets

Do not commit:

- SNMP community strings;
- SMTP passwords;
- Telegram bot tokens;
- private management URLs containing credentials.

`diag --config` prints them as `***`. Use filesystem permissions and
environment/service-manager secret handling appropriate to your
deployment. The database holds password hashes and TOTP secrets since
v0.7.4 — treat it as a secret too.
