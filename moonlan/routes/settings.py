"""Settings, and one's own notifications (v0.8.13).

/api/settings — every setting, saving and resetting them, the test
message of a channel — and /api/me/notifications: one's own email and
Telegram, and an administrator removing somebody's. Moved out of
server.py word for word (docs/ARCHITECTURE.md); the state is read as
rt.X when a route runs. Nothing here imports moonlan.server.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .. import notify, settings, signin, subscriptions
from ..runtime import rt
from .common import _refuse

log = logging.getLogger("moonlan")

router = APIRouter()


# ---------- Settings (v0.8.4) ----------

def _may_change_settings(on: bool) -> bool:
    """An administrator — or everybody while sign-in is off, when
    everybody is one. The console reads only."""
    who = signin.principal()
    return not on or (who is not None and who.role == "admin"
                      and not who.console)


async def _settings_answer() -> dict:
    on = await asyncio.to_thread(rt.sign_in.sign_in_on)
    editor = _may_change_settings(on)
    rows = await asyncio.to_thread(rt.db.settings)
    return {
        "settings": settings.describe(rt.config, rows, personal=editor),
        "sections": list(settings.SECTIONS),
        "alarm_types": list(rt.config.alarm_notify),
        "channels": notify.channel_state(rt.config.notifications),
        "can_edit": editor,
        "sign_in": on,
    }


def _settings_applied(changes: list[dict], how: str) -> None:
    """After a save or a reset: what holds a value of its own is told,
    and the log says what changed, by whom, in one line."""
    keys = {settings.setting_of(c["key"]) for c in changes}
    for key in keys & set(settings.INTERVALS):
        rt.interval_changed[key].set()
    if "thresholds.flap_window_minutes" in keys:
        rt.flap_tracker.window_seconds = rt.config.thresholds.flap_window_minutes * 60
    log.info("Settings %s by %s: %s", how, signin.actor() or "(sign-in off)",
             settings.log_line(changes))


@router.get("/api/settings")
async def api_settings() -> dict:
    """Every setting: its description, value, default, where it comes
    from and who changed it last; whether each channel is set up on the
    server (and nothing about how — no secret ever leaves config.yaml)."""
    return await _settings_answer()


@router.put("/api/settings")
async def api_settings_save(request: Request) -> JSONResponse:
    """{key: value, …}: all checked, then all written, or nothing.
    An unknown key is an error."""
    try:
        body = await request.json()
    except ValueError:
        return _refuse("bad_request", 400)
    changes, errors = await asyncio.to_thread(
        settings.save, rt.config, rt.db, body, signin.actor()
    )
    if errors:
        return JSONResponse({"errors": errors}, status_code=400)
    if changes:
        _settings_applied(changes, "changed")
    return JSONResponse({"changed": changes, **await _settings_answer()})


@router.delete("/api/settings/{key}")
async def api_settings_reset(key: str) -> JSONResponse:
    if key not in settings.BY_KEY:
        return _refuse("unknown_setting", 404)
    changes = await asyncio.to_thread(
        settings.reset, rt.config, rt.db, key, signin.actor()
    )
    if changes:
        _settings_applied(changes, "reset")
    return JSONResponse({"changed": changes, **await _settings_answer()})


@router.post("/api/settings/test/{channel}")
async def api_settings_test(channel: str) -> JSONResponse:
    """A test message to the shared recipients of one channel"""
    state = notify.channel_state(rt.config.notifications).get(channel)
    if state is None:
        return _refuse("unknown_channel", 404)
    if not state["ready"]:
        return _refuse("channel_off", 409, why=state["why"])
    recipients = rt.notifier.shared(channel)
    if not recipients:
        return _refuse("no_recipients", 409)
    text = notify.format_text(
        "test", "Settings", "info",
        f"a test message to the shared recipients, sent by "
        f"{signin.actor() or 'the page'}", False,
    )
    if rt.config.demo:
        log.info("NOTIFY (demo): %s (%s, %d recipient(s))", text, channel,
                 len(recipients))
        return JSONResponse({"sent": len(recipients), "failed": 0,
                             "demo": True})
    failed = await asyncio.to_thread(rt.notifier.deliver, channel, recipients,
                                     text)
    log.info("Test message via %s to the shared recipients, by %s: %d sent, "
             "%d failed", channel, signin.actor() or "(sign-in off)",
             len(recipients) - failed, failed)
    return JSONResponse({"sent": len(recipients) - failed, "failed": failed})


# ---------- one's own notifications (v0.8.4) ----------

class AddressBody(BaseModel):
    address: str = ""
    lang: str = "en"


class CodeBody(BaseModel):
    code: str = ""


class LangBody(BaseModel):
    lang: str = "en"


def _lang(value: str) -> str:
    return value if value in ("en", "ru") else "en"


async def _personal(call, *args) -> JSONResponse:
    """A call for the account of this session — never one named in the
    request — with its refusal as the page's code"""
    try:
        return JSONResponse(await asyncio.to_thread(call, *args))
    except subscriptions.Refused as refused:
        return _refuse(refused.error, refused.status, **refused.extra)


def _who() -> tuple[int, str]:
    who = signin.principal()
    return who.user_id, who.name


@router.get("/api/me/notifications")
async def api_my_notifications() -> JSONResponse:
    user_id, _ = _who()
    return await _personal(rt.personal.state, user_id)


@router.post("/api/me/notifications/email")
async def api_my_email(body: AddressBody) -> JSONResponse:
    """A code to the address, which is not anybody's until it comes back"""
    user_id, name = _who()
    return await _personal(rt.personal.send_code, user_id, name, body.address,
                           _lang(body.lang))


@router.post("/api/me/notifications/email/confirm")
async def api_my_email_confirm(body: CodeBody) -> JSONResponse:
    user_id, name = _who()
    return await _personal(rt.personal.confirm_code, user_id, name, body.code)


@router.post("/api/me/notifications/telegram")
async def api_my_telegram(body: LangBody) -> JSONResponse:
    """A link to the bot, good for ten minutes; the bot's updates are
    read while it waits"""
    user_id, name = _who()
    answer = await _personal(rt.personal.start_link, user_id, name,
                             _lang(body.lang))
    if answer.status_code == 200:
        rt.personal.ensure_polling()
    return answer


@router.get("/api/me/notifications/telegram")
async def api_my_telegram_state() -> JSONResponse:
    user_id, _ = _who()
    return await _personal(rt.personal.link_state, user_id)


@router.patch("/api/me/notifications/{channel}")
async def api_my_notifications_change(channel: str,
                                      request: Request) -> JSONResponse:
    """{enabled} and/or {alarm_types}: a list, or null — as the shared
    table"""
    try:
        body = await request.json()
    except ValueError:
        return _refuse("bad_request", 400)
    if not isinstance(body, dict):
        return _refuse("bad_request", 400)
    user_id, _ = _who()
    return await _personal(rt.personal.change, user_id, channel, body)


@router.post("/api/me/notifications/{channel}/test")
async def api_my_notifications_test(channel: str,
                                    body: LangBody) -> JSONResponse:
    user_id, name = _who()
    return await _personal(rt.personal.test, user_id, name, channel,
                           _lang(body.lang))


@router.delete("/api/me/notifications/{channel}")
async def api_my_notifications_remove(channel: str) -> JSONResponse:
    user_id, name = _who()
    return await _personal(rt.personal.remove, user_id, name, channel)


@router.delete("/api/users/{name}/notifications")
async def api_user_notifications_remove(name: str) -> JSONResponse:
    """Somebody's subscriptions, by an administrator: a person who left,
    whose account is not deleted yet"""
    row = await asyncio.to_thread(rt.sign_in.accounts.user, name)
    if row is None:
        return _refuse("unknown", 404, name=name)
    removed = await asyncio.to_thread(
        rt.personal.remove_all, row["id"], row["name"], signin.actor()
    )
    return JSONResponse({"status": "removed", "removed": removed})
