from __future__ import annotations

from pathlib import Path
from typing import Any

from orchestrator.command_registry import RuntimeCommand
from tools.anatta_diagnostics import build_report


USAGE = "Usage: /anatta [status|full]\nRead-only diagnostics. This command does not enable or modify Anatta."


async def anatta_command(runtime: Any, update: Any, context: Any) -> None:
    args = [str(arg).strip().lower() for arg in (getattr(context, "args", []) or []) if str(arg).strip()]
    if args and args[0] in {"help", "-h", "--help"}:
        await _send(runtime, update, USAGE)
        return
    if args and args[0] not in {"status", "full"}:
        await _send(runtime, update, USAGE)
        return

    full = bool(args and args[0] == "full")
    workspace = Path(getattr(runtime, "workspace_dir"))
    report = build_report(workspace, full=full)
    await _send(runtime, update, report)


async def _send(runtime: Any, update: Any, text: str) -> None:
    chat = getattr(update, "effective_chat", None)
    chat_id = getattr(chat, "id", None)
    if chat_id is not None and hasattr(runtime, "send_long_message"):
        await runtime.send_long_message(
            chat_id,
            text,
            request_id="anatta-command",
            purpose="command",
        )
        return
    message = getattr(update, "message", None)
    if message is not None and hasattr(message, "reply_text"):
        await message.reply_text(text)


COMMANDS = [
    RuntimeCommand(
        name="anatta",
        description="Read-only Anatta diagnostics",
        callback=anatta_command,
    )
]
