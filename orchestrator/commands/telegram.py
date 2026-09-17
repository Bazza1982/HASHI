from __future__ import annotations

from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from orchestrator import ui_language, workbench_telegram_state
from orchestrator.command_registry import RuntimeCallback, RuntimeCommand
from orchestrator.command_ui import selected_label, setting_card, status_label

TITLE = "Workbench Telegram mirror"


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
    if owner is not None and str(owner).strip():
        return str(owner).strip()
    user = getattr(update, "effective_user", None)
    user_id = getattr(user, "id", None)
    if user_id is not None:
        return str(user_id)
    query = getattr(update, "callback_query", None)
    query_user = getattr(query, "from_user", None)
    query_id = getattr(query_user, "id", None)
    if query_id is not None:
        return str(query_id)
    global_config = getattr(runtime, "global_config", None)
    return str(getattr(global_config, "authorized_id", "default") or "default")


def _enabled(runtime: Any, update: Any) -> bool:
    home = _bridge_home(runtime)
    if home is None:
        return True
    return workbench_telegram_state.mirror_enabled(
        home, _owner_id(runtime, update), default=True
    )


def _menu_text(runtime: Any, update: Any, *, notice: str | None = None) -> str:
    enabled = _enabled(runtime, update)
    facts = [
        f"<b>{ui_language.tr('common.scope')}</b> · "
        f"{ui_language.tr('menu.telegram.scope')}"
    ]
    if notice:
        facts.insert(0, f"✅ {notice}")
    return setting_card(
        "📡",
        TITLE,
        current=f"<b>{status_label(enabled)}</b>",
        facts=facts,
        consequence=(
            ui_language.tr("menu.telegram.enabled")
            if enabled
            else ui_language.tr("menu.telegram.disabled")
        ),
        action=ui_language.tr("menu.telegram.action"),
    )


def _keyboard(runtime: Any, update: Any) -> InlineKeyboardMarkup:
    enabled = _enabled(runtime, update)
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    selected_label(ui_language.tr("common.on"), enabled),
                    callback_data="telegram:set:on",
                ),
                InlineKeyboardButton(
                    selected_label(ui_language.tr("common.off"), not enabled),
                    callback_data="telegram:set:off",
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


async def telegram_command(runtime: Any, update: Any, context: Any) -> None:
    if not _is_authorized(runtime, update):
        return
    home = _bridge_home(runtime)
    if home is None:
        await _send(runtime, update, ui_language.tr("menu.telegram.unavailable"))
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
            _menu_text(runtime, update),
            reply_markup=_keyboard(runtime, update),
        )
        return
    value = args[0]
    if value == "on":
        workbench_telegram_state.set_mirror(home, _owner_id(runtime, update), True)
        await _send(
            runtime,
            update,
            _menu_text(runtime, update, notice=ui_language.tr("menu.telegram.notice.on")),
            reply_markup=_keyboard(runtime, update),
        )
        return
    if value == "off":
        workbench_telegram_state.set_mirror(home, _owner_id(runtime, update), False)
        await _send(
            runtime,
            update,
            _menu_text(runtime, update, notice=ui_language.tr("menu.telegram.notice.off")),
            reply_markup=_keyboard(runtime, update),
        )
        return
    await _send(runtime, update, ui_language.tr("menu.telegram.usage"))


async def telegram_callback(runtime: Any, update: Any, context: Any) -> None:
    query = update.callback_query
    if not _is_authorized(runtime, update):
        await query.answer()
        return
    home = _bridge_home(runtime)
    if home is None:
        await query.answer(ui_language.tr("menu.telegram.unavailable"), show_alert=True)
        return
    data = query.data or ""
    parts = data.split(":", 2)
    action = parts[1] if len(parts) > 1 else ""
    value = parts[2] if len(parts) > 2 else ""
    notice = None
    if action == "set" and value in {"on", "off"}:
        workbench_telegram_state.set_mirror(
            home, _owner_id(runtime, update), value == "on"
        )
        notice = ui_language.tr(
            "menu.telegram.notice.on" if value == "on" else "menu.telegram.notice.off"
        )
    await query.edit_message_text(
        _menu_text(runtime, update, notice=notice),
        parse_mode="HTML",
        reply_markup=_keyboard(runtime, update),
    )
    await query.answer()


COMMANDS = [
    RuntimeCommand(
        name="telegram",
        description="Toggle Workbench Telegram mirror [on|off]",
        callback=telegram_command,
    ),
]

CALLBACKS = [RuntimeCallback(pattern=r"^telegram:", callback=telegram_callback)]
