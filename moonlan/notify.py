"""Alarm notifications: email, Telegram and Syslog.

No new dependencies: SMTP via smtplib, Telegram Bot API via
urllib.request, Syslog via a plain UDP socket. Channel errors are
logged and never propagate into the monitoring loops. Secrets from the
config are never written to the log, and neither is a person's address
(v0.8.4): the log is read by more people than the ones whose mailbox
it is.

Who gets an alarm (v0.8.4): on each channel set up on the server, the
shared recipients (Settings → shared recipients) when the shared table
sends that alarm type there, and every confirmed, enabled personal
subscription whose set of alarm types has it (moonlan/subscriptions.py).
An address in both gets one message. Repeats and flapping are damped
once, for everybody: a subscription does not get round them.

Channel check from the command line:

    python -m moonlan.notify --test

sends a test message to every enabled channel and prints a per-channel
result.
"""

from __future__ import annotations

import asyncio
import json
import logging
import smtplib
import socket
import time
import urllib.request
from dataclasses import dataclass
from email.message import EmailMessage

from .config import Config, NotificationsConfig

log = logging.getLogger(__name__)

SEND_TIMEOUT = 10  # seconds per channel operation
SYSLOG_FACILITY = 16  # local0
SYSLOG_SEVERITY = {"critical": 2, "warning": 4, "info": 6}
# The Bot API, unless notifications.telegram.api_url names another — a
# local Bot API server, or a stand-in for a test
TELEGRAM_API = "https://api.telegram.org"


def format_text(
    alarm_type: str,
    subject: str,
    severity: str,
    message: str,
    cleared: bool,
    head: str | None = None,
) -> str:
    if head is None:
        head = "CLEARED" if cleared else severity.upper()
    text = f"[MoonLan] {head} {alarm_type}: {subject}"
    return f"{text} — {message}" if message else text


def channel_state(cfg: NotificationsConfig) -> dict[str, dict]:
    """Is each channel set up on the server, and if not, why not:
    {"ready": True} or {"ready": False, "why": "off" | "no_server" |
    "no_token"}. Nothing about how it is set up — the page is told
    this and no more."""

    def state(enabled: bool, configured: bool, missing: str) -> dict:
        if not enabled:
            return {"ready": False, "why": "off"}
        if not configured:
            return {"ready": False, "why": missing}
        return {"ready": True}

    return {
        "email": state(cfg.email.enabled, bool(cfg.email.smtp_host), "no_server"),
        "telegram": state(cfg.telegram.enabled, bool(cfg.telegram.bot_token),
                          "no_token"),
        "syslog": state(cfg.syslog.enabled, True, ""),
    }


@dataclass(frozen=True)
class Recipient:
    """An address on a channel, and whose: a personal subscription's
    account id, or None for a shared recipient. Syslog has one
    recipient with no address: the server in config.yaml."""

    address: str
    user_id: int | None = None

    def who(self) -> str:
        """For the log: never the address"""
        return f"user #{self.user_id}" if self.user_id is not None else "a shared recipient"


class Notifier:
    """Routes alarm events to the channels listed in alarm_notify.

    In demo mode nothing leaves the machine: every would-be delivery is
    logged as "NOTIFY (demo): …" instead.
    """

    def __init__(self, config: Config, demo: bool = False):
        self._cfg = config.notifications
        self._routing = config.alarm_notify
        self._demo = demo
        # Personal subscriptions (v0.8.4): (channel, alarm type) ->
        # [Recipient] — confirmed and enabled ones, of enabled accounts.
        # server.py sets it; None — nobody's.
        self.personal = None
        # (type, subject, cleared) -> unix time of the last delivery.
        # Raises and clears are throttled separately so a clear arriving
        # right after a raise is not swallowed by the cooldown.
        self._last_sent: dict[tuple[str, str, bool], float] = {}

    def ready(self, channel: str) -> bool:
        return channel_state(self._cfg)[channel]["ready"]

    def shared(self, channel: str) -> list[Recipient]:
        """The shared recipients of a channel (Settings)"""
        if channel == "email":
            return [Recipient(a) for a in self._cfg.email.mail_to]
        if channel == "telegram":
            return [Recipient(str(c)) for c in self._cfg.telegram.chat_ids]
        return [Recipient("")]

    def plan(self, alarm_type: str) -> dict[str, list[Recipient]]:
        """Who gets this alarm type, per channel set up on the server:
        the shared recipients when the table sends it there, and the
        personal subscriptions that chose it. One message per address."""
        routed = self._routing.get(alarm_type, [])
        out: dict[str, list[Recipient]] = {}
        for channel in ("email", "telegram", "syslog"):
            if not self.ready(channel):
                continue
            recipients = self.shared(channel) if channel in routed else []
            if channel != "syslog" and self.personal is not None:
                recipients += self.personal(channel, alarm_type)
            seen: set[str] = set()
            unique = []
            for r in recipients:
                key = r.address.lower() if channel == "email" else r.address
                if key not in seen:
                    seen.add(key)
                    unique.append(r)
            if unique:
                out[channel] = unique
        return out

    async def notify(
        self,
        alarm_type: str,
        subject: str,
        severity: str,
        message: str = "",
        cleared: bool = False,
        head: str | None = None,
        display: str | None = None,
    ) -> None:
        """head overrides the [MoonLan] <HEAD> prefix (e.g. FLAPPING);
        display replaces the raw subject in the text with a
        human-readable one (e.g. "LAG Slot0/1+Slot0/2")."""
        text = format_text(
            alarm_type, display or subject, severity, message, cleared, head
        )
        if self._demo:
            if self._routing.get(alarm_type):
                log.info("NOTIFY (demo): %s", text)
            return
        # the personal subscriptions are read from the database
        plan = await asyncio.to_thread(self.plan, alarm_type)
        if not plan:
            return
        key = (alarm_type, subject, head or cleared)
        now = time.time()
        if now - self._last_sent.get(key, 0) < self._cfg.cooldown_seconds:
            return
        self._last_sent[key] = now
        await asyncio.gather(*(
            asyncio.to_thread(self.deliver, channel, recipients, text, severity)
            for channel, recipients in plan.items()
        ))

    def deliver(self, channel: str, recipients: list[Recipient], text: str,
                severity: str = "info", subject: str | None = None) -> int:
        """One message to each recipient; a failure is logged with the
        channel and whose it was, never the address, and does not stop
        the rest. Returns how many failed."""
        failures = self.send_each(channel, recipients, text, severity, subject)
        for recipient, error in failures:
            # exception text only: URLError/SMTPException do not carry
            # the token or password
            log.error("Notification via %s to %s failed: %s", channel,
                      recipient.who(), error)
        return len(failures)

    # ---------- channels (synchronous, run in to_thread) ----------

    def send(self, channel: str, text: str, severity: str = "info") -> None:
        """A message to the shared recipients of a channel; the first
        failure is raised (notify --test)"""
        if channel not in ("email", "telegram", "syslog"):
            raise ValueError(f"unknown notification channel: {channel}")
        failures = self.send_each(channel, self.shared(channel), text, severity)
        if failures:
            raise failures[0][1]

    def send_each(self, channel: str, recipients: list[Recipient], text: str,
                  severity: str = "info", subject: str | None = None,
                  ) -> list[tuple[Recipient, Exception]]:
        """[(recipient, exception)] of the ones that failed"""
        if channel == "email":
            return self._send_email(recipients, text, subject)
        if channel == "telegram":
            return self._send_telegram(recipients, text)
        if channel == "syslog":
            try:
                self._send_syslog(text, severity)
            except Exception as exc:  # noqa: BLE001 — reported, not raised
                return [(recipients[0] if recipients else Recipient(""), exc)]
            return []
        raise ValueError(f"unknown notification channel: {channel}")

    def _send_email(self, recipients: list[Recipient], text: str,
                    subject: str | None = None,
                    ) -> list[tuple[Recipient, Exception]]:
        """One message per address over one connection: nobody sees
        who else gets it, and a refused address refuses only itself"""
        cfg = self._cfg.email
        if not recipients:
            return []
        failures = []
        sent: set[int] = set()
        try:
            with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port,
                              timeout=SEND_TIMEOUT) as smtp:
                if cfg.starttls:
                    smtp.starttls()
                if cfg.username:
                    smtp.login(cfg.username, cfg.password)
                for recipient in recipients:
                    msg = EmailMessage()
                    msg["Subject"] = subject or text
                    msg["From"] = cfg.mail_from
                    msg["To"] = recipient.address
                    msg.set_content(text)
                    try:
                        smtp.send_message(msg)
                        sent.add(id(recipient))
                    except smtplib.SMTPServerDisconnected:
                        raise
                    except (smtplib.SMTPException, OSError) as exc:
                        failures.append((recipient, exc))
        except (smtplib.SMTPException, OSError) as exc:
            # the server, not an address: whoever was not reached yet
            done = sent | {id(r) for r, _ in failures}
            failures += [(r, exc) for r in recipients if id(r) not in done]
        return failures

    def telegram_call(self, method: str, payload: dict | None = None,
                      timeout: float = SEND_TIMEOUT) -> dict:
        """One Bot API call; the answer's JSON. The URL carries the
        token, so it is never part of an error that is raised."""
        cfg = self._cfg.telegram
        base = (cfg.api_url or TELEGRAM_API).rstrip("/")
        request = urllib.request.Request(
            f"{base}/bot{cfg.bot_token}/{method}",
            data=json.dumps(payload or {}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")

    def _send_telegram(self, recipients: list[Recipient], text: str,
                       ) -> list[tuple[Recipient, Exception]]:
        failures = []
        for recipient in recipients:
            try:
                self.telegram_call("sendMessage",
                                   {"chat_id": recipient.address, "text": text})
            except Exception as exc:  # noqa: BLE001 — one chat, not all
                failures.append((recipient, exc))
        return failures

    def _send_syslog(self, text: str, severity: str) -> None:
        cfg = self._cfg.syslog
        pri = SYSLOG_FACILITY * 8 + SYSLOG_SEVERITY.get(severity, 6)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(f"<{pri}>moonlan: {text}".encode(), (cfg.host, cfg.port))


def _test() -> int:
    """--test: one message to every enabled channel, result per channel."""
    from .config import load_config
    from .settings import overlay, read_rows

    config = load_config()
    # the shared recipients are Settings now (v0.8.4): in the database
    if not config.demo:
        overlay(config, read_rows(config.db_path))
    notifier = Notifier(config)
    text = format_text("test", "notify --test", "info", "channel check", False)
    states = channel_state(config.notifications)
    failures = 0
    tested = 0
    for name in ("email", "telegram", "syslog"):
        if not states[name]["ready"]:
            print(f"{name}: disabled")
            continue
        tested += 1
        try:
            notifier.send(name, text, "info")
            print(f"{name}: OK")
        except Exception as exc:  # noqa: BLE001 — report and keep testing
            print(f"{name}: FAILED ({exc})")
            failures += 1
    if not tested:
        print("No channels are enabled in config.yaml (notifications section).")
    return 1 if failures else 0


if __name__ == "__main__":
    import sys

    if "--test" in sys.argv:
        sys.exit(_test())
    print("usage: python -m moonlan.notify --test")
    sys.exit(2)
