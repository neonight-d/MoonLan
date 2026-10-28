"""Personal notifications (v0.8.4): an account's own email and Telegram.

A person adds their own address, and nothing is sent to it but the
confirmation until it is confirmed: otherwise MoonLan would be a way to
send alarms into somebody else's mailbox. An email is confirmed with a
six-digit code sent to it; a Telegram chat with a link to the bot — the
person presses Start, Telegram sends the bot "/start <key>", and the
private chat it came from is the address.

While a link waits, one getUpdates loop runs (long polling, with an
offset of its own). With none waiting it stops; what arrives meanwhile
is neither kept nor read — MoonLan is not a chat bot. A bot another
program polls, or one with a webhook, answers 409: then a chat cannot
be linked, and the page says so plainly.

Which alarms: the subscription's own list, or — NULL — whatever the
shared table sends to that channel, following it when an administrator
changes it. An explicit list does not follow.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
import urllib.error

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
LINK_SECONDS = 10 * 60       # a Telegram link is good for


POLL_SECONDS = 25            # getUpdates' long polling
# A message sent before its link was made is not read, but Telegram's
# clock and this machine's may differ by a little
CLOCK_SLACK = 60
# 24 characters of the base64url alphabet; Telegram takes up to 64
KEY = re.compile(r"^[A-Za-z0-9_-]{24}$")

# What MoonLan says outside the page: the email with a code, the bot's
# answers. In the language of the page that asked.
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
        "linked": "MoonLan: notifications for {name} are connected.",
        "expired": "MoonLan: this link is out of date — get a new one in "
                   "Settings → My notifications.",
        "group": "MoonLan: notifications are connected in a private chat "
                 "with the bot only, not in a group.",
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
        "linked": "MoonLan: уведомления для {name} подключены.",
        "expired": "MoonLan: ссылка устарела — получите новую в "
                   "«Настройки» → «Мои уведомления».",
        "group": "MoonLan: уведомления подключаются только в личном чате "
                 "с ботом, не в группе.",
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


def key_hash(key: str) -> str:
    """A link's key is 144 random bits: its hash finds its row"""
    return hashlib.sha256(key.encode()).hexdigest()


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
        self._bot: str | None = None   # the bot's username, from getMe
        self._task: asyncio.Task | None = None
        self.offset = 0                # getUpdates' own
        # the text of a 409 Conflict, while the bot is somebody else's
        self.conflict: str | None = None

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

    def link_state(self, user_id: int, now: float | None = None) -> dict:
        """waiting | group (opened in a group; still waiting) | expired |
        conflict (the bot is somebody else's) | connected | none"""
        now = time.time() if now is None else now
        row = self._accounts.code_of(user_id, "telegram")
        if row is not None:
            if row["expires_at"] <= now:
                state = "expired"
            elif self.conflict and not self.polling():
                state = "conflict"
            elif row["refused"] == "group":
                state = "group"
            else:
                state = "waiting"
            return {"state": state, "expires_at": row["expires_at"],
                    "conflict": self.conflict if state == "conflict" else None}
        if "telegram" in self._accounts.subscriptions_of(user_id):
            return {"state": "connected"}
        return {"state": "none"}

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

    # ---------- Telegram: a link ----------

    def bot_name(self) -> str:
        if self._bot is None:
            answer = self._notifier.telegram_call("getMe")
            self._bot = answer["result"]["username"]
        return self._bot

    def start_link(self, user_id: int, name: str, lang: str,
                   now: float | None = None) -> dict:
        """A one-time link to the bot; the one before it is gone"""
        now = time.time() if now is None else now
        self._need_ready("telegram")
        if self._demo:
            raise Refused("demo", 409)
        try:
            bot = self.bot_name()
        except Exception as exc:  # noqa: BLE001 — said, and refused
            log.warning("Telegram: getMe failed: %s", exc)
            raise Refused("telegram_unavailable", 502) from None
        key = secrets.token_urlsafe(18)
        self._accounts.put_code(user_id, "telegram", "", key_hash(key),
                                now + LINK_SECONDS, lang, now)
        log.info("Notifications: a Telegram link made for user #%d (%s)",
                 user_id, name)
        return {"link": f"https://t.me/{bot}?start={key}",
                "expires_at": now + LINK_SECONDS, "bot": bot}

    def polling(self) -> bool:
        return self._task is not None and not self._task.done()

    def ensure_polling(self) -> None:
        """One getUpdates loop, while a link waits"""
        if not self.polling():
            self._task = asyncio.create_task(self.poll())

    async def poll(self) -> None:
        log.info("Telegram: reading the bot's updates while a link waits")
        while True:
            links = await asyncio.to_thread(self._accounts.waiting_links,
                                            time.time())
            if not links:
                log.info("Telegram: no link waits — not reading the bot's "
                         "updates any more")
                return
            since = min(link["created_at"] for link in links)
            try:
                answer = await asyncio.to_thread(
                    self._notifier.telegram_call, "getUpdates",
                    {"offset": self.offset, "timeout": POLL_SECONDS,
                     "allowed_updates": ["message"]},
                    POLL_SECONDS + 10,
                )
            except urllib.error.HTTPError as exc:
                if exc.code == 409:
                    self.conflict = _description(exc) or "409 Conflict"
                    log.warning(
                        "Telegram: getUpdates answered 409 Conflict (%s) — "
                        "another program reads this bot's updates, or it has "
                        "a webhook. A chat cannot be connected by a link "
                        "until the bot is MoonLan's alone.", self.conflict,
                    )
                    return
                log.warning("Telegram: getUpdates failed: %s", exc)
                await asyncio.sleep(5)
                continue
            except Exception as exc:  # noqa: BLE001 — try again shortly
                log.warning("Telegram: getUpdates failed: %s", exc)
                await asyncio.sleep(5)
                continue
            self.conflict = None
            for update in answer.get("result", []):
                self.offset = max(self.offset, int(update["update_id"]) + 1)
                try:
                    await asyncio.to_thread(self.handle_update, update, since)
                except Exception:  # noqa: BLE001 — one update, not the loop
                    log.exception("Telegram: an update could not be handled")

    def handle_update(self, update: dict, since: float) -> str | None:
        """"/start <key>" from a chat: what came of it (for the tests),
        None for anything that is not one of ours"""
        message = update.get("message") or {}
        if message.get("date", 0) < since - CLOCK_SLACK:
            return None  # from before any link waited: not read
        parts = (message.get("text") or "").split()
        if len(parts) != 2 or parts[0] != "/start" or not KEY.match(parts[1]):
            return None
        row = self._accounts.code_by_secret("telegram", key_hash(parts[1]))
        if row is None:
            return None  # not a key of ours: nothing is said
        chat = message.get("chat") or {}
        said = words(row["lang"])
        now = time.time()
        if row["expires_at"] <= now:
            self._reply(chat.get("id"), said["expired"])
            return "expired"
        if chat.get("type") != "private":
            # one person's alarms do not go to a group chat
            self._accounts.refuse_code(row["user_id"], "telegram", "group")
            self._reply(chat.get("id"), said["group"])
            log.info("Notifications: a Telegram link for user #%d was opened "
                     "in a group — refused", row["user_id"])
            return "group"
        self._accounts.confirm_subscription(row["user_id"], "telegram",
                                            str(chat["id"]), now)
        self._event("subscription_added", row["name"], row["name"], now,
                    channel="telegram")
        self._reply(chat["id"], said["linked"].format(name=row["name"]))
        log.info("Notifications: Telegram connected for user #%d (%s)",
                 row["user_id"], row["name"])
        return "linked"

    def _reply(self, chat_id, text: str) -> None:
        if chat_id is None:
            return
        try:
            self._notifier.telegram_call("sendMessage",
                                         {"chat_id": chat_id, "text": text})
        except Exception as exc:  # noqa: BLE001 — the answer is a courtesy
            log.warning("Telegram: could not answer in a chat: %s", exc)

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


def _description(exc: urllib.error.HTTPError) -> str:
    """The Bot API's own words for a refusal"""
    try:
        return json.loads(exc.read().decode("utf-8")).get("description", "")
    except Exception:  # noqa: BLE001 — the status then
        return ""
