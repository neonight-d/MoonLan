"""The journal's groups (v0.8.14).

What the journal's filters on the page choose from, in one place: the
route of the journal reads them, and so does the daily cleanup, which
keeps the pings for a term of their own. Moved out of routes/alarms.py
word for word (docs/ARCHITECTURE.md), so that housekeeping.py does not
import a module of routes. Nothing here imports moonlan.server.
"""

from __future__ import annotations


# What the journal's filters on the page choose from (v0.8.6), in one
# place. Every event the code writes belongs to one of these, or to
# "other" by being in none of them — tests/test_journal.py reads the
# code for every add_event and holds this to it.
JOURNAL_GROUPS: dict[str, tuple[str, ...]] = {
    "alarms": ("alarm_raised", "alarm_cleared"),
    "ping": ("host_down", "host_up"),
    # an address taken off a record ARP no longer confirms (v0.8.7):
    # two in three device events, and they buried the new devices
    "addresses": ("ip_released",),
    "devices": (
        "new_mac", "hosts_purged", "copies_removed", "monitor_on",
        "monitor_off",
    ),
    "map": (
        "layout_pinned", "layout_released", "layout_saved",
        "layout_cleared", "note_changed", "note_moved", "notes_imported",
        "link_dropped",
    ),
    # sign-ins, accounts, keys, one's own notifications: ACCOUNT_EVENTS
    # of the page
    "users": (
        "login", "logout", "account_locked", "user_added", "user_role",
        "user_password", "user_disabled", "user_enabled",
        "user_totp_reset", "user_totp_enabled", "user_unlocked",
        "user_sessions_closed", "user_deleted", "user_passkey_added",
        "user_passkey_removed", "user_passkeys_reset",
        "passkey_clone_suspected", "subscription_added",
        "subscription_removed", "user_notifications_removed",
    ),
    "settings": ("settings_changed", "settings_moved"),
}
JOURNAL_OTHER = "other"
EVENT_GROUP: dict[str, str] = {
    event: group for group, events in JOURNAL_GROUPS.items()
    for event in events
}
