from __future__ import annotations

from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from orchestrator import ui_language, runtime_session
from orchestrator.connector_delivery_preferences import (
    get_connector_preference,
    set_connector_preference,
)
from orchestrator.command_registry import RuntimeCallback, RuntimeCommand
from orchestrator.command_ui import selected_label, setting_card, status_label

def _is_authorized(runtime: Any, update: Any) -> bool:
    checker = getattr(runtime, "_is_authorized_user", None)
    user = getattr(update, "effective_user", None)
    user_id = getattr(user, "id", None)
    if callable(checker):
        return bool(checker(user_id))
    global_config = getattr(runtime, "global_config", None)
    authorized_id = getattr(global_config, "authorized_id", None)
    return authorized_id is None or user_id == authorized_id


def _bridge_home(runtime: Any) -> Any:
    global_config = getattr(runtime, "global_config", None)
    return getattr(global_config, "bridge_home", None)


def _owner_id(runtime: Any, update: Any) -> str:
    owner = getattr(update, "_hashi_session_owner_id", None)
    return runtime_session.owner_id(runtime, str(owner).strip() if owner else None)


def _enabled(runtime: Any, update: Any, connector_id: str = "telegram") -> bool:
    home = _bridge_home(runtime)
    if home is None:
        return connector_id == "telegram"
    return get_connector_preference(
        home, _owner_id(runtime, update), connector_id, "mirror",
        default=connector_id == "telegram",
    )


def _menu_text(runtime: Any, update: Any, *, connector_id: str = "telegram", notice: str | None = None) -> str:
    enabled = _enabled(runtime, update, connector_id)
    prefix = f"menu.{connector_id}"
    facts = [
        f"<b>{ui_language.tr('common.scope')}</b> · "
        f"{ui_language.tr(prefix + '.scope')}"
    ]
    if notice:
        facts.insert(0, f"✅ {notice}")
    return setting_card(
        "📡",
        ui_language.tr(prefix + ".title"),
        current=f"<b>{status_label(enabled)}</b>",
        facts=facts,
        consequence=(
            ui_language.tr(prefix + ".enabled")
            if enabled
            else ui_language.tr(prefix + ".disabled")
        ),
        action=ui_language.tr(prefix + ".action"),
    )


def _keyboard(runtime: Any, update: Any, connector_id: str = "telegram") -> InlineKeyboardMarkup:
    enabled = _enabled(runtime, update, connector_id)
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    selected_label(ui_language.tr("common.on"), enabled),
                    callback_data=f"{connector_id}:set:on",
                ),
                InlineKeyboardButton(
                    selected_label(ui_language.tr("common.off"), not enabled),
                    callback_data=f"{connector_id}:set:off",
                ),
            ]
        ]
    )


async def _send(runtime: Any, update: Any, text: str, *, reply_markup=None) -> None:
    if hasattr(runtime, "_reply_text"):
        await runtime._reply_text(
            update, text, parse_mode="HTML", reply_markup=reply_markup
        )
        return
    message = getattr(update, "message", None)
    if message is not None and hasattr(message, "reply_text"):
        await message.reply_text(text, parse_mode="HTML", reply_markup=reply_markup)
        return
    chat = getattr(update, "effective_chat", None)
    chat_id = getattr(chat, "id", None)
    if chat_id is not None and hasattr(runtime, "send_long_message"):
        await runtime.send_long_message(
            chat_id, text, request_id="telegram-command", purpose="command"
        )


async def _mirror_command(runtime: Any, update: Any, context: Any, connector_id: str) -> None:
    if not _is_authorized(runtime, update):
        return
    home = _bridge_home(runtime)
    if home is None:
        await _send(runtime, update, ui_language.tr(f"menu.{connector_id}.unavailable"))
        return
    args = [
        str(arg).strip().lower()
        for arg in (getattr(context, "args", None) or [])
        if str(arg).strip()
    ]
    if not args:
        await _send(
            runtime,
            update,
            _menu_text(runtime, update, connector_id=connector_id),
            reply_markup=_keyboard(runtime, update, connector_id),
        )
        return
    value = args[0]
    if len(args) == 1 and value in {"on", "off"}:
        set_connector_preference(home, _owner_id(runtime, update), connector_id, "mirror", value == "on")
        await _send(
            runtime,
            update,
            _menu_text(runtime, update, connector_id=connector_id, notice=ui_language.tr(f"menu.{connector_id}.notice.{value}")),
            reply_markup=_keyboard(runtime, update, connector_id),
        )
        return
    await _send(runtime, update, ui_language.tr(f"menu.{connector_id}.usage"))


async def telegram_command(runtime: Any, update: Any, context: Any) -> None:
    await _mirror_command(runtime, update, context, "telegram")


async def whatsapp_command(runtime: Any, update: Any, context: Any) -> None:
    await _mirror_command(runtime, update, context, "whatsapp")


async def telegram_callback(runtime: Any, update: Any, context: Any) -> None:
    await _mirror_callback(runtime, update, context, "telegram")


async def whatsapp_callback(runtime: Any, update: Any, context: Any) -> None:
    await _mirror_callback(runtime, update, context, "whatsapp")


async def _mirror_callback(runtime: Any, update: Any, context: Any, connector_id: str) -> None:
    query = update.callback_query
    if not _is_authorized(runtime, update):
        await query.answer()
        return
    home = _bridge_home(runtime)
    if home is None:
        await query.answer(ui_language.tr(f"menu.{connector_id}.unavailable"), show_alert=True)
        return
    data = query.data or ""
    parts = data.split(":", 2)
    action = parts[1] if len(parts) > 1 else ""
    value = parts[2] if len(parts) > 2 else ""
    notice = None
    if action == "set" and value in {"on", "off"}:
        set_connector_preference(
            home, _owner_id(runtime, update), connector_id, "mirror", value == "on"
        )
        notice = ui_language.tr(
            f"menu.{connector_id}.notice.{value}"
        )
    await query.edit_message_text(
        _menu_text(runtime, update, connector_id=connector_id, notice=notice),
        parse_mode="HTML",
        reply_markup=_keyboard(runtime, update, connector_id),
    )
    await query.answer()


COMMANDS = [
    RuntimeCommand(
        name="telegram",
        description="Toggle Telegram mirror for other connectors [on|off]",
        callback=telegram_command,
    ),
    RuntimeCommand(
        name="whatsapp",
        description="Toggle WhatsApp mirror for other connectors [on|off]",
        callback=whatsapp_command,
    ),
]

CALLBACKS = [
    RuntimeCallback(pattern=r"^telegram:", callback=telegram_callback),
    RuntimeCallback(pattern=r"^whatsapp:", callback=whatsapp_callback),
]
