"""Personal notifications (v0.8.4): an account's own email and Telegram.

A person adds their own address, and nothing is sent to it but the
confirmation until it is confirmed: otherwise MoonLan would be a way to
send alarms into somebody else's mailbox. An email is confirmed with a
six-digit code sent to it.

Which alarms: the subscription's own list, or — NULL — whatever the
shared table sends to that channel, following it when an administrator
changes it. An explicit list does not follow.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time

from .config import Config
from .db import Database
from .notify import Notifier, Recipient, channel_state
from .settings import EMAIL

log = logging.getLogger(__name__)

CHANNELS = ("email", "telegram")

CODE_SECONDS = 15 * 60       # an email code is good for
CODE_ATTEMPTS = 5            # wrong codes, and it burns
CODE_GAP = 60                # one code a minute per account…
CODES_PER_HOUR = 5           # …and five an hour


# What MoonLan says outside the page: the email with a code. In the
# language of the page that asked.
MESSAGES = {
    "en": {
        "code_subject": "MoonLan: confirmation code",
        "code_body": (
            "The code that confirms this address for MoonLan's "
            "notifications: {code}\n\n"
            "It is valid for 15 minutes. Enter it in Settings → My "
            "notifications.\n\n"
            "If you did not ask for it, just delete this message.\n"
        ),
    },
    "ru": {
        "code_subject": "MoonLan: код подтверждения",
        "code_body": (
            "Код, подтверждающий этот адрес для уведомлений MoonLan: "
            "{code}\n\n"
            "Он действует 15 минут. Введите его в «Настройки» → «Мои "
            "уведомления».\n\n"
            "Если вы этого не запрашивали — просто удалите письмо.\n"
        ),
    },
}


def words(lang: str) -> dict[str, str]:
    return MESSAGES.get(lang, MESSAGES["en"])


def code_hash(code: str, salt: str | None = None) -> str:
    """salt$sha256(salt + code): the code itself is never stored"""
    salt = salt or secrets.token_hex(8)
    return salt + "$" + hashlib.sha256((salt + code).encode()).hexdigest()


def code_matches(code: str, stored: str) -> bool:
    salt, _, _ = stored.partition("$")
    return hmac.compare_digest(code_hash(code, salt), stored)


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
        # account id -> when its codes were sent (the last hour's)
        self._codes_sent: dict[int, list[float]] = {}

    # ---------- what the page is told ----------

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

    # ---------- email: a code ----------

    def _codes_allowed(self, user_id: int, now: float) -> None:
        sent = [t for t in self._codes_sent.get(user_id, []) if now - t < 3600]
        self._codes_sent[user_id] = sent
        if sent and now - sent[-1] < CODE_GAP:
            raise Refused("too_soon", 429, retry_at=sent[-1] + CODE_GAP)
        if len(sent) >= CODES_PER_HOUR:
            raise Refused("too_many_codes", 429, retry_at=sent[0] + 3600)

    def send_code(self, user_id: int, name: str, address: str, lang: str,
                  now: float | None = None) -> dict:
        """A code to the address; the address is the subscription's only
        once the code comes back"""
        now = time.time() if now is None else now
        self._need_ready("email")
        address = (address or "").strip().lower()
        if not EMAIL.match(address):
            raise Refused("bad_address", 400)
        self._codes_allowed(user_id, now)
        code = f"{secrets.randbelow(10 ** 6):06d}"
        self._accounts.put_code(user_id, "email", address, code_hash(code),
                                now + CODE_SECONDS, lang, now)
        self._codes_sent.setdefault(user_id, []).append(now)
        said = words(lang)
        if self._demo:
            # nothing leaves the machine in demo mode; the demo's
            # accounts are throwaway, so the log may say the code
            log.info("NOTIFY (demo): confirmation code for user #%d: %s",
                     user_id, code)
        else:
            failures = self._notifier.send_each(
                "email", [Recipient(address, user_id)],
                said["code_body"].format(code=code), "info",
                subject=said["code_subject"],
            )
            if failures:
                self._accounts.drop_code(user_id, "email")
                log.error("Notifications: the confirmation code to user #%d "
                          "was not sent: %s", user_id, failures[0][1])
                raise Refused("send_failed", 502)
        log.info("Notifications: a confirmation code sent to user #%d (%s)",
                 user_id, name)
        return {"status": "sent", "expires_at": now + CODE_SECONDS}

    def confirm_code(self, user_id: int, name: str, code: str,
                     now: float | None = None) -> dict:
        now = time.time() if now is None else now
        row = self._accounts.code_of(user_id, "email")
        if row is None:
            raise Refused("no_code", 409)
        if row["expires_at"] <= now:
            self._accounts.drop_code(user_id, "email")
            raise Refused("code_expired", 410)
        if not code_matches((code or "").strip(), row["secret"]):
            attempts = self._accounts.code_failed(user_id, "email")
            if attempts >= CODE_ATTEMPTS:
                self._accounts.drop_code(user_id, "email")
                raise Refused("code_burned", 409)
            raise Refused("wrong_code", 400, left=CODE_ATTEMPTS - attempts)
        self._accounts.confirm_subscription(user_id, "email", row["address"],
                                            now)
        self._event("subscription_added", name, name, now, channel="email")
        log.info("Notifications: email confirmed for user #%d (%s)",
                 user_id, name)
        return self.state(user_id, now)

    # ---------- what the person changes ----------

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
