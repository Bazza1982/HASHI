"""User controls for PAO-owned question cards."""
from __future__ import annotations

import asyncio
import sqlite3
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from orchestrator import runtime_session, ui_language
from orchestrator.command_registry import RuntimeCallback, RuntimeCommand
from orchestrator.command_ui import card_title, selected_label, status_label
from orchestrator.run_questions import RunQuestions


def _authorized(runtime: Any, update: Any) -> bool:
    checker = getattr(runtime, "_is_authorized_user", None)
    actor = getattr(getattr(update, "effective_user", None), "id", None)
    return bool(callable(checker) and checker(actor))


def _service(runtime: Any) -> RunQuestions:
    return RunQuestions(runtime_session.ensure_store(runtime))


def _card(policy: dict[str, bool]) -> tuple[str, InlineKeyboardMarkup]:
    text = "\n".join((
        card_title("❔", ui_language.tr("askme.title")),
        ui_language.tr("askme.instance", state=status_label(policy["instance_enabled"])),
        ui_language.tr("askme.agent", state=status_label(policy["agent_enabled"])),
        ui_language.tr("askme.effective", state=status_label(policy["enabled"])),
        "",
        ui_language.tr("askme.scope"),
    ))
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(selected_label(ui_language.tr("askme.agent_on"), policy["agent_enabled"]),
                                 callback_data="askme:agent:on"),
            InlineKeyboardButton(selected_label(ui_language.tr("askme.agent_off"), not policy["agent_enabled"]),
                                 callback_data="askme:agent:off"),
        ],
        [
            InlineKeyboardButton(selected_label(ui_language.tr("askme.instance_on"), policy["instance_enabled"]),
                                 callback_data="askme:instance:on"),
            InlineKeyboardButton(selected_label(ui_language.tr("askme.instance_off"), not policy["instance_enabled"]),
                                 callback_data="askme:instance:off"),
        ],
    ])
    return text, keyboard


async def askme_command(runtime: Any, update: Any, context: Any) -> None:
    if not _authorized(runtime, update):
        return
    args = [str(arg).strip().casefold() for arg in (getattr(context, "args", None) or [])]
    if args == ["status"]:
        args = []
    scope = "agent"
    if len(args) == 2 and args[0] in {"global", "instance"}:
        scope, args = "instance", args[1:]
    if args and (len(args) != 1 or args[0] not in {"on", "off"}):
        await runtime._reply_text(update, ui_language.tr("askme.usage"))
        return
    try:
        service = _service(runtime)
        if args:
            policy = await asyncio.to_thread(service.set_question_policy, scope=scope,
                                             agent_id=runtime.name, enabled=args[0] == "on")
        else:
            policy = await asyncio.to_thread(service.question_policy, runtime.name)
    except (OSError, sqlite3.Error, ValueError):
        await runtime._reply_text(update, ui_language.tr("askme.error"))
        return
    text, keyboard = _card(policy)
    await runtime._reply_text(update, text, parse_mode="HTML", reply_markup=keyboard)


async def askme_callback(runtime: Any, update: Any, context: Any) -> None:
    query = update.callback_query
    if not _authorized(runtime, update):
        await query.answer()
        return
    parts = str(query.data or "").split(":")
    if len(parts) != 3 or parts[0] != "askme" or parts[1] not in {"agent", "instance"} or parts[2] not in {"on", "off"}:
        await query.answer(ui_language.tr("askme.usage"), show_alert=True)
        return
    try:
        service = _service(runtime)
        policy = await asyncio.to_thread(service.set_question_policy, scope=parts[1],
                                         agent_id=runtime.name, enabled=parts[2] == "on")
    except (OSError, sqlite3.Error, ValueError):
        await query.answer(ui_language.tr("askme.error"), show_alert=True)
        return
    text, keyboard = _card(policy)
    await query.answer()
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)


COMMANDS = [RuntimeCommand("askme", "Control question cards", askme_command)]
CALLBACKS = [RuntimeCallback(pattern=r"^askme:(?:agent|instance):(?:on|off)$", callback=askme_callback)]
