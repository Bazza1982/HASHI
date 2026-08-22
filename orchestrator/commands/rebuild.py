from __future__ import annotations

from typing import Any

from orchestrator.command_registry import RuntimeCommand


RETIRED_NOTICE = (
    "ℹ️ <b>/rebuild is retired</b>\n\n"
    "HER v1's native Rust runtime has been removed. HER v2 is Python-based "
    "and has no native runtime to build.\n\n"
    "No build, reload, or restart was performed. Use <code>/reboot</code> to "
    "adopt HASHI Python updates. This compatibility notice will be removed "
    "after one release."
)


def _authorized(runtime: Any, update: Any) -> bool:
    user = getattr(update, "effective_user", None)
    user_id = getattr(user, "id", None)
    checker = getattr(runtime, "_is_authorized_user", None)
    if callable(checker):
        return bool(checker(user_id))
    authorized_id = getattr(
        getattr(runtime, "global_config", None), "authorized_id", None
    )
    return authorized_id is None or user_id == authorized_id


async def _send(runtime: Any, update: Any, text: str) -> None:
    if hasattr(runtime, "_reply_text"):
        await runtime._reply_text(update, text, parse_mode="HTML")
        return
    message = getattr(update, "effective_message", None) or getattr(
        update, "message", None
    )
    if message is not None and hasattr(message, "reply_text"):
        await message.reply_text(text, parse_mode="HTML")
        return
    chat = getattr(update, "effective_chat", None)
    if chat is not None and hasattr(runtime, "send_long_message"):
        await runtime.send_long_message(
            chat.id,
            text,
            request_id="her-rebuild-command",
            purpose="command",
        )


async def command_rebuild(runtime: Any, update: Any, context: Any) -> None:
    del context
    if not _authorized(runtime, update):
        await _send(
            runtime, update, "⛔ This command is restricted to the authorized owner."
        )
        return
    await _send(runtime, update, RETIRED_NOTICE)


COMMANDS = [
    RuntimeCommand(
        name="rebuild",
        description="Retired HER rebuild notice",
        callback=command_rebuild,
    )
]
