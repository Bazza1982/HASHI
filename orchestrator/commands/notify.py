from __future__ import annotations

from typing import Any

from orchestrator.command_registry import RuntimeCommand
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


async def _send(runtime: Any, update: Any, text: str) -> None:
    if hasattr(runtime, "_reply_text"):
        await runtime._reply_text(update, text)
        return
    message = getattr(update, "message", None)
    if message is not None and hasattr(message, "reply_text"):
        await message.reply_text(text)
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
        state = "ON" if notify_enabled(runtime) else "OFF"
        await _send(runtime, update, f"Telegram notifications: {state}\nUse /notify on or /notify off.")
        return

    value = args[0]
    if value in {"on", "true", "1", "yes"}:
        set_notify_enabled(runtime, True)
        await _send(runtime, update, "Telegram notifications: ON\nFuture Telegram messages will notify normally.")
        return
    if value in {"off", "false", "0", "no"}:
        set_notify_enabled(runtime, False)
        await _send(runtime, update, "Telegram notifications: OFF\nFuture Telegram messages will be delivered silently.")
        return
    await _send(runtime, update, "Usage: /notify [on|off]")


COMMANDS = [
    RuntimeCommand(
        name="notify",
        description="Toggle Telegram notification sound [on|off]",
        callback=notify_command,
    ),
]
