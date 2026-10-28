"""Settings: what the map shows and how alarms rise, kept in the
database and changed from the page (v0.8.4).

Until v0.8.3 every one of these was a config.yaml key, and changing a
group threshold meant a shell on the server, an editor and a restart.
What stays in config.yaml is infrastructure and secrets: where to
listen, what to poll and with which community, how to reach the mail
server and the bot. Everything here is described once, in SETTINGS —
its type, its range, its section on the page, when a change takes
effect — and its default is the field of `Config`, never a copy.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass

from .config import DEFAULT_ALARM_NOTIFY, Config

# The page's sections, in the page's order. Personal notifications
# (moonlan/subscriptions.py) come before them all and are not settings.
SECTIONS = ("map", "polling", "thresholds", "channels", "recipients")

# When a change takes effect: at once; at the next scan; at the next
# ping or counters cycle; at the next cleanup (at the start and once a
# day after it)
APPLIES = ("now", "next_scan", "next_cycle", "next_cleanup")

CHANNELS = ("email", "telegram", "syslog")

@dataclass(frozen=True)
class Setting:
    key: str             # its path in config.yaml and in Config
    kind: str            # "int" | "float" | "bool" | "list" | "table"
    section: str         # one of SECTIONS
    applies: str         # one of APPLIES
    minimum: float | None = None
    maximum: float | None = None
    unit: str = ""       # the page words it: "unit_" + unit
    item: str = ""       # what a list holds: "email" | "chat_id"

    @property
    def label_key(self) -> str:
        """The i18n key of its name on the page"""
        return "set_" + self.key.replace(".", "_")

    @property
    def hint_key(self) -> str:
        """…and of the line behind its ⓘ"""
        return "setHint_" + self.key.replace(".", "_")

    def default(self):
        """The default: Config's own field, read from a fresh one"""
        return copy.deepcopy(_get(Config(), self.key))


def _int(key, section, applies, low, high, unit=""):
    return Setting(key, "int", section, applies, low, high, unit)


def _float(key, section, applies, low, high, unit=""):
    return Setting(key, "float", section, applies, low, high, unit)


def _bool(key, section, applies):
    return Setting(key, "bool", section, applies)


# Ranges: wide enough for any network this was written for, narrow
# enough that a slip of the finger is caught. Where 0 switches the
# thing off (a grouping, a check, damping), 0 is allowed.
SETTINGS: tuple[Setting, ...] = (
    # ---- the map and the inventory
    _int("unmanaged_threshold", "map", "next_scan", 0, 1000, "devices"),
    _int("offline_group_threshold", "map", "next_scan", 0, 1000, "devices"),
    _int("trunk_group_threshold", "map", "next_scan", 0, 1000, "devices"),
    # 0 — a device leaves the map with its MAC; a year is the far end
    _float("host_grace_hours", "map", "next_scan", 0, 8760, "hours"),
    # 0 — never deleted (purge_old_hosts)
    _float("host_retention_days", "map", "next_cleanup", 0, 3650, "days"),
    # a day at least: 0 would forget every position off the map at once
    _float("layout_keep_days", "map", "next_cleanup", 1, 3650, "days"),
    # 0 — an address is never released (_release_unconfirmed_ips)
    _float("ip_confirm_hours", "map", "next_scan", 0, 720, "hours"),
    _int("new_host_confirm_scans", "map", "next_scan", 1, 20, "scans"),
    _float("stale_rate_hide_minutes", "map", "now", 1, 1440, "minutes"),
    _int("stale_switch_scans", "map", "next_scan", 1, 100, "scans"),
    # the map at its next request, alarms at the next ping cycle
    _bool("monitored_by_default", "map", "now"),
    _bool("filter_suspect_macs", "map", "next_scan"),
    _bool("place_trunk_only_hosts", "map", "next_scan"),
    # ---- polling: a sleeping loop is woken and counts again from its
    # last run, so a new interval holds at once
    # 0 — only on request (the Actions menu, /api/scan)
    _int("scan_interval_minutes", "polling", "now", 0, 1440, "minutes"),
    _int("ping_interval_seconds", "polling", "now", 5, 3600, "seconds"),
    _int("counters_interval_seconds", "polling", "now", 5, 3600, "seconds"),
    # ---- alarm thresholds (config.Thresholds)
    # 0 — any damaged frame counts
    _float("thresholds.errors_per_minute", "thresholds", "next_cycle",
           0, 1_000_000, "per_minute"),
    _float("thresholds.error_ratio_percent", "thresholds", "next_cycle",
           0, 100, "percent"),
    _float("thresholds.discards_per_minute", "thresholds", "next_cycle",
           0, 10_000_000, "per_minute"),
    _float("thresholds.port_utilization_percent", "thresholds", "next_cycle",
           1, 100, "percent"),
    _int("thresholds.port_alarm_cycles", "thresholds", "next_cycle",
         1, 100, "cycles"),
    # 0 — port_hosts_down is off
    _int("thresholds.mass_down_hosts", "thresholds", "next_cycle",
         0, 1000, "devices"),
    # a MAC has 48 bits; past half of them "a damaged copy" means nothing
    _int("thresholds.corruption_hamming_bits", "thresholds", "next_scan",
         1, 24, "bits"),
    _int("thresholds.corruption_macs_threshold", "thresholds", "next_scan",
         1, 1000, "addresses"),
    _int("thresholds.stp_changes_per_cycle", "thresholds", "next_scan",
         0, 10_000, "changes"),
    # 0 — port_flapping is off
    _int("thresholds.flaps_per_window", "thresholds", "next_cycle",
         0, 1000, "transitions"),
    _float("thresholds.flap_window_minutes", "thresholds", "next_cycle",
           1, 1440, "minutes"),
    # ---- notifications: repeats and damping, then the table
    _int("notifications.cooldown_seconds", "channels", "now",
         0, 86400, "seconds"),
    # 0 — no damping
    _int("notifications.flap_count", "channels", "now", 0, 100, "raises"),
    _int("notifications.flap_window_seconds", "channels", "now",
         60, 604_800, "seconds"),
    _int("notifications.flap_quiet_seconds", "channels", "now",
         60, 604_800, "seconds"),
    Setting("alarm_notify", "table", "channels", "now"),
    # ---- the shared recipients
    Setting("notifications.email.mail_to", "list", "recipients", "now",
            item="email"),
    Setting("notifications.telegram.chat_ids", "list", "recipients", "now",
            item="chat_id"),
)

BY_KEY: dict[str, Setting] = {s.key: s for s in SETTINGS}

# What a viewer is not shown: people's addresses (the count only).
# Not secrets — the password and the bot token never leave config.yaml
# — but a person's mailbox is not something every viewer needs.
PERSONAL = {"notifications.email.mail_to", "notifications.telegram.chat_ids"}

# The intervals: a change wakes the loop that sleeps on it
INTERVALS = ("scan_interval_minutes", "ping_interval_seconds",
             "counters_interval_seconds")


def yaml_paths(key: str) -> list[str]:
    """The config.yaml keys a setting was read from: the table is one
    key per alarm type."""
    if key == "alarm_notify":
        return [f"alarm_notify.{t}" for t in DEFAULT_ALARM_NOTIFY]
    return [key]


# Every config.yaml key that is a setting now: the loader still reads
# them (that is how they are moved), and diag says they are not used
MOVED_PATHS: frozenset[str] = frozenset(
    path for s in SETTINGS for path in yaml_paths(s.key)
)


# ---------- values ----------

def _get(obj, key: str):
    for part in key.split("."):
        obj = getattr(obj, part)
    return obj
