from __future__ import annotations

from typing import Any

from orchestrator.command_registry import RuntimeCallback


async def cmd_voice(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return

    args = [arg.strip() for arg in (context.args or []) if arg.strip()]
    if not args or args[0].lower() == "status":
        await runtime._reply_text(
            update,
            runtime.voice_manager.voice_menu_text(),
            reply_markup=runtime._voice_keyboard(),
        )
        return

    mode = args[0].lower()
    if mode in {"providers", "list"}:
        await runtime._reply_text(update, runtime.voice_manager.provider_hints())
        return
    if mode in {"voices", "menu"}:
        await runtime._reply_text(
            update,
            runtime.voice_manager.voice_menu_text(),
            reply_markup=runtime._voice_keyboard(),
        )
        return
    if mode == "use":
        if len(args) == 1:
            await runtime._reply_text(update, "Usage: /voice use <alias>")
            return
        try:
            await runtime._reply_text(update, runtime.voice_manager.apply_voice_preset(args[1]))
        except Exception as exc:
            await runtime._reply_text(update, str(exc))
        return
    if mode == "provider":
        if len(args) == 1:
            await runtime._reply_text(update, f"Current voice provider: {runtime.voice_manager.get_provider_name()}")
            return
        try:
            await runtime._reply_text(update, runtime.voice_manager.set_provider(args[1]))
        except Exception as exc:
            await runtime._reply_text(update, str(exc))
        return
    if mode == "name":
        if len(args) == 1:
            await runtime._reply_text(update, "Usage: /voice name <voice-name>")
            return
        await runtime._reply_text(update, runtime.voice_manager.set_voice_name(" ".join(args[1:])))
        return
    if mode == "rate":
        if len(args) == 1:
            await runtime._reply_text(update, "Usage: /voice rate <integer>")
            return
        try:
            await runtime._reply_text(update, runtime.voice_manager.set_rate(int(args[1])))
        except ValueError:
            await runtime._reply_text(update, "Voice rate must be an integer.")
        return
    if mode == "on":
        await runtime._reply_text(update, runtime.voice_manager.set_enabled(True))
        return
    if mode == "off":
        await runtime._reply_text(update, runtime.voice_manager.set_enabled(False))
        return

    await runtime._reply_text(
        update,
        "Usage: /voice [status|on|off|voices|use <alias>|providers|provider <name>|name <voice>|rate <n>]",
    )


async def callback_voice(runtime: Any, update: Any, context: Any) -> None:
    query = update.callback_query
    if not runtime._is_authorized_user(query.from_user.id):
        return
    parts = (query.data or "").split(":", 2)
    action = parts[1] if len(parts) > 1 else "refresh"
    value = parts[2] if len(parts) > 2 else ""
    message = None
    try:
        if action == "toggle":
            message = runtime.voice_manager.set_enabled(value == "on")
        elif action == "use":
            message = runtime.voice_manager.apply_voice_preset(value)
    except Exception as exc:
        await query.answer(str(exc), show_alert=True)
        return

    text = runtime.voice_manager.voice_menu_text()
    if message:
        text = f"{text}\n\n{message}"
    await query.edit_message_text(text, reply_markup=runtime._voice_keyboard())
    await query.answer()


CALLBACKS = [
    RuntimeCallback(pattern=r"^voice:", callback=callback_voice),
]
