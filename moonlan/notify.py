"""Alarm notifications: email, Telegram and Syslog.

No new dependencies: SMTP via smtplib, Telegram Bot API via
urllib.request, Syslog via a plain UDP socket. Channel errors are
logged and never propagate into the monitoring loops. Secrets from the
config are never written to the log.

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
from email.message import EmailMessage

from .config import Config

log = logging.getLogger(__name__)

SEND_TIMEOUT = 10  # seconds per channel operation
SYSLOG_FACILITY = 16  # local0
SYSLOG_SEVERITY = {"critical": 2, "warning": 4, "info": 6}


def format_text(
    alarm_type: str, subject: str, severity: str, message: str, cleared: bool
) -> str:
    head = "CLEARED" if cleared else severity.upper()
    text = f"[MoonLan] {head} {alarm_type}: {subject}"
    return f"{text} — {message}" if message else text


class Notifier:
    """Routes alarm events to the channels listed in alarm_notify.

    In demo mode nothing leaves the machine: every would-be delivery is
    logged as "NOTIFY (demo): …" instead.
    """

    def __init__(self, config: Config, demo: bool = False):
        self._cfg = config.notifications
        self._routing = config.alarm_notify
        self._demo = demo
        # (type, subject, cleared) -> unix time of the last delivery.
        # Raises and clears are throttled separately so a clear arriving
        # right after a raise is not swallowed by the cooldown.
        self._last_sent: dict[tuple[str, str, bool], float] = {}

    def _enabled_channels(self, alarm_type: str) -> list[str]:
        enabled = {
            "email": self._cfg.email.enabled,
            "telegram": self._cfg.telegram.enabled,
            "syslog": self._cfg.syslog.enabled,
        }
        return [
            name
            for name in self._routing.get(alarm_type, [])
            if enabled.get(name)
        ]

    async def notify(
        self,
        alarm_type: str,
        subject: str,
        severity: str,
        message: str = "",
        cleared: bool = False,
    ) -> None:
        text = format_text(alarm_type, subject, severity, message, cleared)
        if self._demo:
            if self._routing.get(alarm_type):
                log.info("NOTIFY (demo): %s", text)
            return
        channels = self._enabled_channels(alarm_type)
        if not channels:
            return
        key = (alarm_type, subject, cleared)
        now = time.time()
        if now - self._last_sent.get(key, 0) < self._cfg.cooldown_seconds:
            return
        self._last_sent[key] = now
        results = await asyncio.gather(
            *(asyncio.to_thread(self.send, ch, text, severity) for ch in channels),
            return_exceptions=True,
        )
        for channel, result in zip(channels, results):
            if isinstance(result, BaseException):
                # exception text only: URLError/SMTPException do not
                # carry the token or password
                log.error("Notification via %s failed: %s", channel, result)

    # ---------- channels (synchronous, run in to_thread) ----------

    def send(self, channel: str, text: str, severity: str = "info") -> None:
        if channel == "email":
            self._send_email(text)
        elif channel == "telegram":
            self._send_telegram(text)
        elif channel == "syslog":
            self._send_syslog(text, severity)
        else:
            raise ValueError(f"unknown notification channel: {channel}")

    def _send_email(self, text: str) -> None:
        cfg = self._cfg.email
        msg = EmailMessage()
        msg["Subject"] = text
        msg["From"] = cfg.mail_from
        msg["To"] = ", ".join(cfg.mail_to)
        msg.set_content(text)
        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=SEND_TIMEOUT) as smtp:
            if cfg.starttls:
                smtp.starttls()
            if cfg.username:
                smtp.login(cfg.username, cfg.password)
            smtp.send_message(msg)

    def _send_telegram(self, text: str) -> None:
        cfg = self._cfg.telegram
        url = f"https://api.telegram.org/bot{cfg.bot_token}/sendMessage"
        for chat_id in cfg.chat_ids:
            payload = json.dumps({"chat_id": chat_id, "text": text}).encode()
            request = urllib.request.Request(
                url, data=payload, headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(request, timeout=SEND_TIMEOUT) as resp:
                resp.read()

    def _send_syslog(self, text: str, severity: str) -> None:
        cfg = self._cfg.syslog
        pri = SYSLOG_FACILITY * 8 + SYSLOG_SEVERITY.get(severity, 6)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(f"<{pri}>moonlan: {text}".encode(), (cfg.host, cfg.port))


def _test() -> int:
    """--test: one message to every enabled channel, result per channel."""
    from .config import load_config

    config = load_config()
    notifier = Notifier(config)
    text = format_text("test", "notify --test", "info", "channel check", False)
    channels = [
        ("email", config.notifications.email.enabled),
        ("telegram", config.notifications.telegram.enabled),
        ("syslog", config.notifications.syslog.enabled),
    ]
    failures = 0
    tested = 0
    for name, enabled in channels:
        if not enabled:
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
