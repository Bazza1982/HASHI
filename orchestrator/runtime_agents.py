from __future__ import annotations

import re
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from orchestrator.command_registry import RuntimeCallback


def build_agents_view(orchestrator: Any) -> tuple[str, InlineKeyboardMarkup]:
    all_agents = orchestrator.get_all_agents_raw()
    running_names = set(orchestrator._runtime_map().keys())
    starting_names = set(orchestrator._startup_tasks.keys())

    lines = ["<b>📋 HASHI Agents</b>"]
    rows = []

    for agent in all_agents:
        name = agent.get("name", "?")
        display = agent.get("display_name", name)
        is_active = agent.get("is_active", True)

        if name in starting_names:
            status_icon, status_text = "⏳", "starting"
        elif name in running_names:
            status_icon, status_text = "🟢", "running"
        elif is_active:
            status_icon, status_text = "⚪", "stopped"
        else:
            status_icon, status_text = "🔴", "inactive"

        lines.append(f"{status_icon} <b>{name}</b> — {display} [{status_text}]")

        row = []
        if is_active:
            row.append(InlineKeyboardButton(f"❌ {name}", callback_data=f"agents:deactivate:{name}"))
        else:
            row.append(InlineKeyboardButton(f"✅ {name}", callback_data=f"agents:activate:{name}"))

        if name in starting_names:
            row.append(InlineKeyboardButton("⏳", callback_data="agents:noop"))
        elif name in running_names:
            row.append(InlineKeyboardButton("⏹ Stop", callback_data=f"agents:stop:{name}"))
        elif is_active:
            row.append(InlineKeyboardButton("▶ Start", callback_data=f"agents:start:{name}"))

        row.append(InlineKeyboardButton("🗑", callback_data=f"agents:delete:{name}"))
        rows.append(row)

    rows.append([InlineKeyboardButton("🔄 Refresh", callback_data="agents:refresh")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def cmd_agents(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return

    args = context.args or []
    if args and args[0] == "add":
        await cmd_agents_add(runtime, update, context)
        return

    orchestrator = getattr(runtime, "orchestrator", None)
    if orchestrator is None:
        await runtime._reply_text(update, "Agent management unavailable.")
        return

    text, markup = build_agents_view(orchestrator)
    await runtime._reply_text(update, text, reply_markup=markup, parse_mode="HTML")


async def cmd_agents_add(runtime: Any, update: Any, context: Any) -> None:
    orchestrator = getattr(runtime, "orchestrator", None)
    if orchestrator is None:
        await runtime._reply_text(update, "Agent management unavailable.")
        return

    args = context.args or []
    if len(args) < 3:
        await runtime._reply_text(update, "Usage: /agents add <id> <display_name> [telegram_token]")
        return

    new_id = args[1]
    if not re.match(r"^[a-zA-Z0-9_]+$", new_id):
        await runtime._reply_text(update, "Agent ID must be alphanumeric with underscores only.")
        return

    if len(args) >= 4 and re.match(r"^\d+:[A-Za-z0-9_-]+$", args[-1]):
        token = args[-1]
        display_name = " ".join(args[2:-1])
    else:
        token = None
        display_name = " ".join(args[2:])
    if not display_name:
        display_name = new_id

    _ok, message = orchestrator.add_agent_to_config(new_id, display_name, token)
    await runtime._reply_text(update, message)


async def callback_agents(runtime: Any, update: Any, context: Any) -> None:
    query = update.callback_query
    if not runtime._is_authorized_user(query.from_user.id):
        return

    orchestrator = getattr(runtime, "orchestrator", None)
    if orchestrator is None:
        await query.answer("Agent management unavailable.", show_alert=True)
        return

    data = query.data or ""
    parts = data.split(":", 2)
    action = parts[1] if len(parts) > 1 else ""
    name = parts[2] if len(parts) > 2 else ""

    if action in ("refresh", "noop"):
        await query.answer()
        text, markup = build_agents_view(orchestrator)
        await query.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
        return

    if action == "activate":
        await query.answer(f"Activating {name}…")
        orchestrator.set_agent_active(name, True)
    elif action == "deactivate":
        if name in orchestrator._runtime_map():
            await query.answer(f"Stop {name} first.", show_alert=True)
            return
        await query.answer(f"Deactivating {name}…")
        orchestrator.set_agent_active(name, False)
    elif action == "start":
        await query.answer(f"Starting {name}…")
        ok, message = await orchestrator.start_agent(name)
        if not ok:
            await query.answer(message, show_alert=True)
            return
    elif action == "stop":
        await query.answer(f"Stopping {name}…")
        ok, message = await orchestrator.stop_agent(name)
        if not ok:
            await query.answer(message, show_alert=True)
            return
    elif action == "delete":
        await query.answer()
        confirm_markup = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"⚠️ Confirm delete {name}", callback_data=f"agents:confirmdelete:{name}"),
            InlineKeyboardButton("Cancel", callback_data="agents:refresh"),
        ]])
        await query.edit_message_text(
            f"⚠️ <b>Delete '{name}'?</b>\n\nRemoves from config only — workspace files are kept.",
            reply_markup=confirm_markup,
            parse_mode="HTML",
        )
        return
    elif action == "confirmdelete":
        if name in orchestrator._runtime_map():
            await query.answer(f"Stop {name} first.", show_alert=True)
            return
        await query.answer(f"Deleted {name}.")
        orchestrator.delete_agent_from_config(name)

    text, markup = build_agents_view(orchestrator)
    await query.edit_message_text(text, reply_markup=markup, parse_mode="HTML")


CALLBACKS = [
    RuntimeCallback(pattern=r"^agents:", callback=callback_agents),
]
