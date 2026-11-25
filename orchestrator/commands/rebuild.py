from __future__ import annotations

from typing import Any

from orchestrator.command_registry import RuntimeCommand


RETIRED_NOTICE = (
    "ℹ️ <b>/rebuild 已停用</b>\n\n"
    "HER v1 的本地 Rust 运行时已经移除。HERV3 使用 Python，没有需要构建的本地运行时。\n\n"
    "本次没有构建、重载或重启。要启用 HASHI Python 更新，请使用 <code>/reboot</code>。"
    "这个兼容提示会在下个版本移除。"
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
            runtime, update, "⛔ 此命令仅限已授权的所有者使用。"
        )
        return
    await _send(runtime, update, RETIRED_NOTICE)


COMMANDS = [
    RuntimeCommand(
        name="rebuild",
        description="已停用的 /rebuild 提示",
        callback=command_rebuild,
    )
]
