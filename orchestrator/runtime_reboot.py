from __future__ import annotations

from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def reboot_keyboard(runtime: Any, all_names: list[str]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("This bot", callback_data="tgl:reboot:min"),
            InlineKeyboardButton("All active", callback_data="tgl:reboot:max"),
            InlineKeyboardButton("Running only", callback_data="tgl:reboot:same"),
        ]
    ]
    for index, name in enumerate(all_names, 1):
        rows.append([InlineKeyboardButton(f"#{index} {name}", callback_data=f"tgl:reboot:{index}")])
    return InlineKeyboardMarkup(rows)


async def callback_reboot_toggle(runtime: Any, query: Any, value: str) -> None:
    orchestrator = getattr(runtime, "orchestrator", None)
    if orchestrator is None:
        await query.answer("Hot restart unavailable.", show_alert=True)
        return

    if value == "min":
        mode, label = "min", f"Restarting only <b>{runtime.name}</b>..."
    elif value == "max":
        mode, label = "max", "Restarting all active agents..."
    elif value.isdigit():
        all_names = orchestrator.configured_agent_names()
        number = int(value)
        mode, label = "number", f"Restarting agent #{number} (<b>{all_names[number - 1]}</b>)..."
    else:
        mode, label = "same", "Restarting all running agents..."

    await query.edit_message_text(label, parse_mode="HTML")
    await query.answer()
    orchestrator.request_restart(
        mode=mode,
        agent_name=runtime.name,
        agent_number=int(value) if value.isdigit() else None,
    )


async def cmd_reboot(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return

    orchestrator = getattr(runtime, "orchestrator", None)
    if orchestrator is None:
        await runtime._reply_text(update, "Hot restart is unavailable.")
        return

    arg = " ".join(context.args).strip().lower() if context.args else ""
    if not arg or arg == "help":
        all_names = orchestrator.configured_agent_names()
        lines = ["<b>Reboot</b> — select target:"]
        running_names = {rt.name for rt in orchestrator.runtimes}
        for index, name in enumerate(all_names, 1):
            marker = "●" if name in running_names else "○"
            lines.append(f"  {index}. {marker} {name}")
        await runtime._reply_text(
            update,
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=reboot_keyboard(runtime, all_names),
        )
        return

    if arg == "min":
        mode, label = "min", f"Restarting only <b>{runtime.name}</b>..."
    elif arg == "max":
        mode, label = "max", "Restarting all active agents..."
    elif arg.isdigit():
        number = int(arg)
        all_names = orchestrator.configured_agent_names()
        if number < 1 or number > len(all_names):
            await runtime._reply_text(update, f"Invalid agent number. Use 1–{len(all_names)}. /reboot help to list.")
            return
        mode, label = "number", f"Restarting agent #{number} (<b>{all_names[number - 1]}</b>)..."
    else:
        mode, label = "same", "Restarting all running agents..."

    await runtime._reply_text(update, label, parse_mode="HTML")
    orchestrator.request_restart(
        mode=mode,
        agent_name=runtime.name,
        agent_number=int(arg) if arg.isdigit() else None,
    )
