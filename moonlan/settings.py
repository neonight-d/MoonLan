"""Settings: what the map shows and how alarms rise, kept in the
database and changed from the page (v0.8.4).

Until v0.8.3 every one of these was a config.yaml key, and changing a
group threshold meant a shell on the server, an editor and a restart.
What stays in config.yaml is infrastructure and secrets: where to
listen, what to poll and with which community, how to reach the mail
server and the bot. Everything here is described once, in SETTINGS —
its type, its range, its section on the page, when a change takes
effect — and its default is the field of `Config`, never a copy.

A value is put onto the live `config` by changing the field in place.
The alarm engine holds `config.thresholds` and `config.notifications`,
the notifier `config.alarm_notify`: a new object in their place would
not be seen by either.

Moving from config.yaml (the rule runs at every start, for every key):

    in the database   in config.yaml   what happens
    yes               no               the database's value
    yes               yes              the database's value; config.yaml's
                                       is said to be unused
    no                yes              moved into the database (journal:
                                       "moved from config.yaml"), then the same
    no                no               the default

A value config.yaml has that is out of range is not moved: the log
names the key and the default applies. The service starts either way.
"""

from __future__ import annotations

import copy
import json
import re
import sqlite3
import time
from dataclasses import dataclass, field

from .config import DEFAULT_ALARM_NOTIFY, Config

# The page's sections, in the page's order. Personal notifications
# (moonlan/subscriptions.py) come before them all and are not settings.
SECTIONS = ("map", "polling", "thresholds", "channels", "recipients")

# When a change takes effect: at once; at the next scan; at the next
# ping or counters cycle; at the next cleanup (at the start and once a
# day after it)
APPLIES = ("now", "next_scan", "next_cycle", "next_cleanup")

CHANNELS = ("email", "telegram", "syslog")

# A list longer than this is a mistake, not a mailing list
LIST_MAX = 50

# Loose on purpose: user@localhost is a working address on a server
# with its own mail system, and the mail server is the judge of the
# rest. What is refused is what cannot be one address at all.
EMAIL = re.compile(r"^[^@\s<>,;\"']+@[^@\s<>,;\"']+$")
# A chat id is a number (negative for a group), or @name for a channel
CHAT_ID = re.compile(r"^(-?\d{1,20}|@[A-Za-z0-9_]{4,32})$")


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


def current(cfg: Config, key: str):
    return copy.deepcopy(_get(cfg, key))


def apply(cfg: Config, key: str, value) -> None:
    """Puts a checked value onto the live config — the field changed in
    place, on the object everybody already holds."""
    if key == "alarm_notify":
        # item by item, never cleared first: a notification sent from
        # another thread meanwhile finds every type in its place
        for alarm_type, channels in value.items():
            cfg.alarm_notify[alarm_type] = list(channels)
        return
    head, _, name = key.rpartition(".")
    owner = _get(cfg, head) if head else cfg
    setattr(owner, name, copy.deepcopy(value))


def _number(setting: Setting, value):
    """(value, error): a number of the setting's kind, in its range"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, {"code": "type", "kind": setting.kind}
    if setting.kind == "int":
        if isinstance(value, float):
            if not value.is_integer():
                return None, {"code": "type", "kind": "int"}
            value = int(value)
    else:
        value = float(value)
    if not (setting.minimum <= value <= setting.maximum):
        return None, {"code": "range", "min": _plain(setting.minimum),
                      "max": _plain(setting.maximum)}
    return value, None


def _plain(number):
    """3.0 -> 3: a range the page shows as people write it"""
    return int(number) if float(number).is_integer() else number


def _list(setting: Setting, value):
    if not isinstance(value, list) or not all(
        isinstance(v, (str, int)) and not isinstance(v, bool) for v in value
    ):
        return None, {"code": "type", "kind": "list"}
    items: list[str] = []
    pattern = EMAIL if setting.item == "email" else CHAT_ID
    for raw in value:
        item = str(raw).strip()
        if setting.item == "email":
            item = item.lower()
        if not pattern.match(item):
            return None, {"code": setting.item, "value": item}
        if item not in items:
            items.append(item)
    if len(items) > LIST_MAX:
        return None, {"code": "too_many", "max": LIST_MAX}
    return items, None


def check_table(value, base: dict | None = None):
    """(table, errors): alarm type -> channels, every type present.

    A partial table changes the types it names and keeps the rest from
    `base`. Errors are per type, keyed "alarm_notify.<type>"."""
    table = copy.deepcopy(base if base is not None else DEFAULT_ALARM_NOTIFY)
    if not isinstance(value, dict):
        return None, {"alarm_notify": {"code": "type", "kind": "table"}}
    errors: dict[str, dict] = {}
    for alarm_type, channels in value.items():
        where = f"alarm_notify.{alarm_type}"
        if alarm_type not in DEFAULT_ALARM_NOTIFY:
            errors[where] = {"code": "alarm_type", "value": str(alarm_type)}
            continue
        if not isinstance(channels, list):
            errors[where] = {"code": "type", "kind": "list"}
            continue
        wrong = [c for c in channels if c not in CHANNELS]
        if wrong:
            errors[where] = {"code": "channel", "value": str(wrong[0])}
            continue
        table[alarm_type] = [c for c in CHANNELS if c in channels]
    return table, errors


def check(setting: Setting, value, cfg: Config | None = None):
    """(normalized value, error or None) for one setting"""
    if setting.kind in ("int", "float"):
        return _number(setting, value)
    if setting.kind == "bool":
        if not isinstance(value, bool):
            return None, {"code": "type", "kind": "bool"}
        return value, None
    if setting.kind == "list":
        return _list(setting, value)
    table, errors = check_table(
        value, cfg.alarm_notify if cfg is not None else None
    )
    if errors:
        where, error = next(iter(errors.items()))
        return None, {**error, "where": where}
    return table, None


def validate(cfg: Config, changes: dict):
    """({key: normalized}, {key: error}): everything checked, nothing
    applied. An unknown key is an error, not something to skip."""
    if not isinstance(changes, dict):
        return {}, {"": {"code": "type", "kind": "object"}}
    good: dict = {}
    errors: dict[str, dict] = {}
    for key, value in changes.items():
        setting = BY_KEY.get(key)
        if setting is None:
            errors[key] = {"code": "unknown"}
            continue
        normalized, error = check(setting, value, cfg)
        if error:
            errors[key] = error
        else:
            good[key] = normalized
    return good, errors


# ---------- what the page and diag are told ----------

def describe(cfg: Config, rows: dict[str, dict], personal: bool) -> list[dict]:
    """Every setting with its value, default, source and last change.
    `personal` False hides the shared recipients' addresses (a count
    stays)."""
    out = []
    for s in SETTINGS:
        row = rows.get(s.key)
        entry = {
            "key": s.key, "kind": s.kind, "section": s.section,
            "applies": s.applies, "unit": s.unit,
            "label": s.label_key, "hint": s.hint_key,
            "default": s.default(), "value": current(cfg, s.key),
            "source": "database" if row else "default",
            "updated_at": row["updated_at"] if row else None,
            "updated_by": row["updated_by"] if row else None,
        }
        if s.minimum is not None:
            entry["min"] = _plain(s.minimum)
            entry["max"] = _plain(s.maximum)
        if s.item:
            entry["item"] = s.item
        if s.key in PERSONAL and not personal:
            entry["count"] = len(entry["value"])
            entry["value"] = None
            entry["default"] = None
            entry["hidden"] = True
        out.append(entry)
    return out


def setting_of(path: str) -> str:
    """The setting a journal change belongs to: "alarm_notify.host_down"
    is the table's"""
    return "alarm_notify" if path.startswith("alarm_notify.") else path


def changes_between(key: str, before, after) -> list[dict]:
    """What the journal and the log say about one key: the table per
    alarm type, a list of people's addresses by its length only — the
    journal is for every viewer."""
    if key == "alarm_notify":
        return [
            {"key": f"alarm_notify.{t}", "from": before.get(t, []),
             "to": after.get(t, [])}
            for t in DEFAULT_ALARM_NOTIFY
            if before.get(t, []) != after.get(t, [])
        ]
    if before == after:
        return []
    if key in PERSONAL:
        return [{"key": key, "from": len(before), "to": len(after),
                 "count": True}]
    return [{"key": key, "from": before, "to": after}]


def log_line(changes: list[dict]) -> str:
    """offline_group_threshold 2 → 3, alarm_notify.port_util [syslog] →
    [telegram, syslog], notifications.email.mail_to 2 → 3 addresses"""

    def word(value, count=False):
        if count:
            return f"{value} address(es)"
        if isinstance(value, list):
            return "[" + ", ".join(value) + "]"
        if isinstance(value, bool):
            return "on" if value else "off"
        return f"{value:g}" if isinstance(value, float) else str(value)

    return ", ".join(
        f"{c['key']} {word(c['from'], c.get('count'))} → "
        f"{word(c['to'], c.get('count'))}"
        for c in changes
    )


# ---------- the database ----------

def read_rows(path: str) -> dict[str, dict]:
    """The settings table of a database file, opened read-only — for
    diag and the console, which do not write the service's database.
    {} when there is no file or no table yet."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    try:
        conn.row_factory = sqlite3.Row
        return {
            row["key"]: dict(row) for row in conn.execute(
                "SELECT key, value, updated_at, updated_by FROM settings"
            )
        }
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def stored(rows: dict[str, dict]) -> dict:
    """{key: decoded value} of the settings table"""
    out = {}
    for key, row in rows.items():
        try:
            out[key] = json.loads(row["value"])
        except (TypeError, ValueError):
            continue
    return out


@dataclass
class LoadReport:
    """What the start did with config.yaml, for the log and diag"""

    # moved into the database at this start
    moved: list[str] = field(default_factory=list)
    # in config.yaml, in Settings now: no longer read
    unused: list[str] = field(default_factory=list)
    # config.yaml values that could not be moved: (key, value, error)
    invalid: list[tuple[str, object, dict]] = field(default_factory=list)
    # database values a later version no longer accepts (default used)
    stale: list[tuple[str, object, dict]] = field(default_factory=list)


def overlay(cfg: Config, rows: dict[str, dict]) -> list[tuple[str, object, dict]]:
    """The database's values onto cfg, read-only (diag). Returns the
    stored values that are no longer valid; their default applies."""
    stale = []
    values = stored(rows)
    for s in SETTINGS:
        if s.key not in values:
            apply(cfg, s.key, s.default())
            continue
        value, error = check(s, values[s.key])
        if error:
            stale.append((s.key, values[s.key], error))
            apply(cfg, s.key, s.default())
        else:
            apply(cfg, s.key, value)
    return stale


def _in_yaml(cfg: Config) -> set[str]:
    """Settings config.yaml has a value for"""
    report = cfg.report
    if report is None:
        return set()
    read = {key for key, _, source in report.values if source == "config.yaml"}
    return {s.key for s in SETTINGS if read & set(yaml_paths(s.key))}


def load(cfg: Config, db, now: float | None = None) -> LoadReport:
    """At every start: the database's values onto cfg, config.yaml's
    moved where the database has none (see the module's docstring)."""
    now = time.time() if now is None else now
    report = LoadReport()
    in_yaml = _in_yaml(cfg)
    rows = db.settings()
    values = stored(rows)
    to_store: dict = {}
    for s in SETTINGS:
        if s.key in values:
            value, error = check(s, values[s.key])
            if error:
                report.stale.append((s.key, values[s.key], error))
                value = s.default()
            apply(cfg, s.key, value)
            if s.key in in_yaml:
                report.unused.append(s.key)
            continue
        if s.key not in in_yaml:
            apply(cfg, s.key, s.default())
            continue
        # as the loader read it from the file
        found = next(v for path, v, source in cfg.report.values
                     if path == s.key and source == "config.yaml") \
            if s.kind != "table" else None
        if s.kind == "table":
            # one bad alarm type keeps its default, the rest move
            given = {
                path.split(".", 1)[1]: v
                for path, v, source in cfg.report.values
                if source == "config.yaml" and path.startswith("alarm_notify.")
            }
            value, errors = check_table(given)
            for where, error in errors.items():
                report.invalid.append((where, given[where.split(".", 1)[1]],
                                       error))
        else:
            value, error = check(s, found)
            if error:
                report.invalid.append((s.key, found, error))
                apply(cfg, s.key, s.default())
                continue
        apply(cfg, s.key, value)
        to_store[s.key] = value
        report.moved.append(s.key)
        report.unused.append(s.key)
    if to_store:
        db.save_settings(
            {k: json.dumps(v) for k, v in to_store.items()}, now, "config.yaml"
        )
        db.add_event(
            now, "settings_moved", "",
            json.dumps({"changes": [
                {"key": k, "to": (len(v) if k in PERSONAL else v),
                 **({"count": True} if k in PERSONAL else {})}
                for k, v in to_store.items()
            ]}),
        )
    return report


def save(cfg: Config, db, changes: dict, user: str,
         now: float | None = None):
    """A change from the page: everything checked, then everything
    written and applied, or nothing. Returns (changes for the journal,
    errors); one journal entry per save."""
    now = time.time() if now is None else now
    good, errors = validate(cfg, changes)
    if errors:
        return [], errors
    diff: list[dict] = []
    to_store: dict[str, str] = {}
    for key, value in good.items():
        before = current(cfg, key)
        said = changes_between(key, before, value)
        if not said:
            continue
        diff += said
        to_store[key] = json.dumps(value)
    if not to_store:
        return [], {}
    db.save_settings(to_store, now, user)
    for key in to_store:
        apply(cfg, key, good[key])
    db.add_event(now, "settings_changed", "", json.dumps({"changes": diff}),
                 user)
    return diff, {}


def reset(cfg: Config, db, key: str, user: str, now: float | None = None):
    """Back to the default: the row goes, the default applies. Returns
    the journal's changes ([] when it was the default already)."""
    now = time.time() if now is None else now
    s = BY_KEY[key]
    before = current(cfg, key)
    db.delete_setting(key)
    apply(cfg, key, s.default())
    diff = changes_between(key, before, s.default())
    if diff:
        for change in diff:
            change["reset"] = True
        db.add_event(now, "settings_changed", "",
                     json.dumps({"changes": diff}), user)
    return diff
