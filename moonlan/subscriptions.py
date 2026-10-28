"""Personal notifications (v0.8.4): an account's own email and Telegram.

A subscription is an account's address on a channel, kept beside the
accounts, and only once it is confirmed: an address nobody confirmed
gets nothing. Which alarms: the subscription's own list, or — NULL —
whatever the shared table sends to that channel, following it when an
administrator changes it. An explicit list does not follow.
"""

from __future__ import annotations

import json
import logging

from .config import Config
from .db import Database
from .notify import Notifier, Recipient, channel_state

log = logging.getLogger(__name__)

CHANNELS = ("email", "telegram")


class Refused(Exception):
    """A request refused, with a code the page words"""

    def __init__(self, error: str, status: int = 409, **extra):
        super().__init__(error)
        self.error = error
        self.status = status
        self.extra = extra


class Subscriptions:
    """The accounts' own notifications: kept in the accounts' database,
    journalled in the service's."""

    def __init__(self, accounts: Database, journal: Database, config: Config,
                 notifier: Notifier, demo: bool = False):
        self._accounts = accounts
        self._journal = journal
        self._config = config
        self._notifier = notifier
        self._demo = demo

    # ---------- what is needed of a channel ----------

    def _ready(self) -> dict[str, dict]:
        states = channel_state(self._config.notifications)
        return {c: states[c] for c in CHANNELS}

    def routed(self, channel: str) -> list[str]:
        """The alarm types the shared table sends to a channel now"""
        return [t for t, chs in self._config.alarm_notify.items()
                if channel in chs]

    def _chosen(self, row: dict, channel: str) -> list[str]:
        if row["alarm_types"] is None:
            return self.routed(channel)
        try:
            return list(json.loads(row["alarm_types"]))
        except ValueError:
            return self.routed(channel)

    def _need_ready(self, channel: str) -> None:
        state = self._ready()[channel]
        if not state["ready"]:
            raise Refused("channel_off", 409, why=state["why"])

    def _event(self, event: str, name: str, who: str, now: float,
               **details) -> None:
        self._journal.add_event(now, event, name, json.dumps(details), who)

    # ---------- who gets an alarm ----------

    def recipients(self, channel: str, alarm_type: str) -> list[Recipient]:
        """Confirmed and enabled subscriptions of enabled accounts whose
        set of alarm types has this one"""
        if channel not in CHANNELS:
            return []
        return [
            Recipient(row["address"], row["user_id"])
            for row in self._accounts.active_subscriptions(channel)
            if alarm_type in self._chosen(row, channel)
        ]
