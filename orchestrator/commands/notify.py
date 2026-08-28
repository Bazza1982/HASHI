from __future__ import annotations

from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from orchestrator import telegram_notifications, ui_language
from orchestrator.command_registry import RuntimeCallback, RuntimeCommand
from orchestrator.command_ui import refresh_label, selected_label, setting_card


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
    mode = telegram_notifications.notification_mode(runtime)
    facts = [
        f"<b>{ui_language.tr('common.scope')}</b> · {ui_language.tr('notify.scope')}"
    ]
    if notice:
        facts.insert(0, f"✅ {notice}")
    return setting_card(
        "🔔",
        "Telegram notifications",
        current=(
            f"<b>{mode.upper()}</b>"
            if ui_language.current_locale() == ui_language.DEFAULT_LOCALE
            else f"<b>{ui_language.tr(f'notify.mode.{mode}')}</b> · <code>{mode}</code>"
        ),
        facts=facts,
        consequence=ui_language.tr(f"notify.effect.{mode}"),
        action=ui_language.tr("notify.action"),
    )


def _keyboard(runtime: Any) -> InlineKeyboardMarkup:
    mode = telegram_notifications.notification_mode(runtime)
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(selected_label(ui_language.tr("notify.mode.on"), mode == "on"), callback_data="notify:on"),
                InlineKeyboardButton(selected_label(ui_language.tr("notify.mode.quiet"), mode == "quiet"), callback_data="notify:quiet"),
                InlineKeyboardButton(selected_label(ui_language.tr("notify.mode.off"), mode == "off"), callback_data="notify:off"),
            ],
            [InlineKeyboardButton(refresh_label(), callback_data="notify:refresh")],
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
        telegram_notifications.set_notification_mode(runtime, "on")
        await _send(
            runtime,
            update,
            _menu_text(runtime, notice=ui_language.tr("notify.notice.on")),
            reply_markup=_keyboard(runtime),
        )
        return
    if value in {"off", "false", "0", "no"}:
        telegram_notifications.set_notification_mode(runtime, "off")
        await _send(
            runtime,
            update,
            _menu_text(runtime, notice=ui_language.tr("notify.notice.off")),
            reply_markup=_keyboard(runtime),
        )
        return
    if value in {"quiet", "final", "final-only", "final_only"}:
        telegram_notifications.set_notification_mode(runtime, "quiet")
        await _send(
            runtime,
            update,
            _menu_text(runtime, notice=ui_language.tr("notify.notice.quiet")),
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
        telegram_notifications.set_notification_mode(runtime, "on")
        notice = ui_language.tr("notify.notice.on")
    elif action == "quiet":
        telegram_notifications.set_notification_mode(runtime, "quiet")
        notice = ui_language.tr("notify.notice.quiet")
    elif action == "off":
        telegram_notifications.set_notification_mode(runtime, "off")
        notice = ui_language.tr("notify.notice.off")
    await query.edit_message_text(
        _menu_text(runtime, notice=notice),
        parse_mode="HTML",
        reply_markup=_keyboard(runtime),
    )
    await query.answer()


COMMANDS = [
    RuntimeCommand(
        name="notify",
        description="Set Telegram notification sound [on|quiet|off]",
        callback=notify_command,
    ),
]

CALLBACKS = [RuntimeCallback(pattern=r"^notify:", callback=notify_callback)]
