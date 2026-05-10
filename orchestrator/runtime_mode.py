from __future__ import annotations

from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def mode_keyboard(current: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Fixed" if current == "fixed" else "Fixed", callback_data="tgl:mode:fixed"),
            InlineKeyboardButton("✅ Flex" if current == "flex" else "Flex", callback_data="tgl:mode:flex"),
        ]]
    )


async def cmd_mode(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    args = (context.args[0].lower() if context.args else "").strip()
    current = runtime.backend_manager.agent_mode

    if not args or args not in ("fixed", "flex"):
        await runtime._reply_text(
            update,
            f"Current mode: <b>{current}</b>\n\n"
            f"• <b>fixed</b> — continuous CLI session, incremental prompts\n"
            f"• <b>flex</b> — multi-backend switching, full context injection",
            parse_mode="HTML",
            reply_markup=mode_keyboard(current),
        )
        return

    if args == current:
        await runtime._reply_text(update, f"Already in **{current}** mode.", parse_mode="Markdown")
        return

    await switch_mode_from_command(runtime, update, args)


async def switch_mode_from_command(runtime: Any, update: Any, target_mode: str) -> None:
    runtime.backend_manager.agent_mode = target_mode
    runtime.backend_manager._save_state()

    backend = runtime.backend_manager.current_backend
    if target_mode == "fixed":
        if hasattr(backend, "set_session_mode"):
            backend.set_session_mode(True)
        await runtime._reply_text(
            update,
            "Switched to **fixed** mode.\n"
            "• CLI session will persist across messages\n"
            "• Bridge sends incremental prompts (no history re-injection)\n"
            "• `/backend` is disabled; use `/mode flex` to re-enable\n"
            "• `/new` will terminate the current session and start fresh",
            parse_mode="Markdown",
        )
        return

    if hasattr(backend, "set_session_mode"):
        backend.set_session_mode(False)
    await runtime._reply_text(
        update,
        "Switched to **flex** mode.\n"
        "• Full context injection per request\n"
        "• `/backend` switching re-enabled",
        parse_mode="Markdown",
    )


async def callback_mode_toggle(runtime: Any, query: Any, value: str) -> None:
    current = runtime.backend_manager.agent_mode
    if value == current:
        await query.answer(f"Already in {current} mode.")
        return

    runtime.backend_manager.agent_mode = value
    runtime.backend_manager._save_state()
    backend = runtime.backend_manager.current_backend
    if value == "fixed":
        if hasattr(backend, "set_session_mode"):
            backend.set_session_mode(True)
        detail = "CLI session persists · /backend disabled"
    else:
        if hasattr(backend, "set_session_mode"):
            backend.set_session_mode(False)
        detail = "Full context injection · /backend enabled"

    await query.edit_message_text(
        f"Mode: <b>{value}</b>\n{detail}",
        parse_mode="HTML",
        reply_markup=mode_keyboard(value),
    )
    await query.answer(f"Switched to {value}")
