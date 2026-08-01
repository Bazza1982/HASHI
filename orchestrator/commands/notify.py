from __future__ import annotations

from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from orchestrator.command_registry import RuntimeCallback, RuntimeCommand
from orchestrator.command_ui import REFRESH_LABEL, selected_label, setting_card
from orchestrator.telegram_notifications import notify_enabled, set_notify_enabled


def _is_authorized(runtime: Any, update: Any) -> bool:
    checker = getattr(runtime, "_is_authorized_user", None)
    user = getattr(update, "effective_user", None)
    user_id = getattr(user, "id", None)
    if callable(checker):
        return bool(checker(user_id))
    global_config = getattr(runtime, "global_config", None)
    authorized_id = getattr(global_config, "authorized_id", None)
    return authorized_id is None or user_id == authorized_id


def _menu_text(runtime: Any, *, notice: str | None = None) -> str:
    enabled = notify_enabled(runtime)
    facts = ["<b>Scope</b> · Telegram messages from this agent"]
    if notice:
        facts.insert(0, f"✅ {notice}")
    return setting_card(
        "🔔",
        "Telegram notifications",
        current=f"<b>{'ON' if enabled else 'OFF'}</b>",
        facts=facts,
        consequence=(
            "Messages use normal Telegram notification sound."
            if enabled
            else "Messages are still delivered, but Telegram receives them silently."
        ),
        action="Choose a state below. Changes persist in this workspace.",
    )


def _keyboard(runtime: Any) -> InlineKeyboardMarkup:
    enabled = notify_enabled(runtime)
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(selected_label("On", enabled), callback_data="notify:on"),
                InlineKeyboardButton(selected_label("Off", not enabled), callback_data="notify:off"),
            ],
            [InlineKeyboardButton(REFRESH_LABEL, callback_data="notify:refresh")],
        ]
    )


async def _send(runtime: Any, update: Any, text: str, *, reply_markup=None) -> None:
    if hasattr(runtime, "_reply_text"):
        await runtime._reply_text(update, text, parse_mode="HTML", reply_markup=reply_markup)
        return
    message = getattr(update, "message", None)
    if message is not None and hasattr(message, "reply_text"):
        await message.reply_text(text, parse_mode="HTML", reply_markup=reply_markup)
        return
    chat = getattr(update, "effective_chat", None)
    chat_id = getattr(chat, "id", None)
    if chat_id is not None and hasattr(runtime, "send_long_message"):
        await runtime.send_long_message(chat_id, text, request_id="notify-command", purpose="command")


async def notify_command(runtime: Any, update: Any, context: Any) -> None:
    if not _is_authorized(runtime, update):
        return
    args = [str(arg).strip().lower() for arg in (getattr(context, "args", None) or []) if str(arg).strip()]
    if not args:
        await _send(runtime, update, _menu_text(runtime), reply_markup=_keyboard(runtime))
        return

    value = args[0]
    if value in {"on", "true", "1", "yes"}:
        set_notify_enabled(runtime, True)
        await _send(
            runtime,
            update,
            _menu_text(runtime, notice="Notification sound enabled."),
            reply_markup=_keyboard(runtime),
        )
        return
    if value in {"off", "false", "0", "no"}:
        set_notify_enabled(runtime, False)
        await _send(
            runtime,
            update,
            _menu_text(runtime, notice="Notification sound disabled."),
            reply_markup=_keyboard(runtime),
        )
        return
    await _send(runtime, update, _menu_text(runtime), reply_markup=_keyboard(runtime))


async def notify_callback(runtime: Any, update: Any, context: Any) -> None:
    query = update.callback_query
    if not _is_authorized(runtime, update):
        await query.answer()
        return
    action = (query.data or "").split(":", 1)[1] if ":" in (query.data or "") else "refresh"
    notice = None
    if action == "on":
        set_notify_enabled(runtime, True)
        notice = "Notification sound enabled."
    elif action == "off":
        set_notify_enabled(runtime, False)
        notice = "Notification sound disabled."
    await query.edit_message_text(
        _menu_text(runtime, notice=notice),
        parse_mode="HTML",
        reply_markup=_keyboard(runtime),
    )
    await query.answer()


COMMANDS = [
    RuntimeCommand(
        name="notify",
        description="Toggle Telegram notification sound [on|off]",
        callback=notify_command,
    ),
]

CALLBACKS = [RuntimeCallback(pattern=r"^notify:", callback=notify_callback)]
