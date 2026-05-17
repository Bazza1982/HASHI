from __future__ import annotations

import asyncio
import subprocess
import sys
from typing import Any

from orchestrator.command_registry import RuntimeCommand


async def _send(runtime: Any, update: Any, text: str) -> None:
    chat = getattr(update, "effective_chat", None)
    chat_id = getattr(chat, "id", None)
    if chat_id is not None and hasattr(runtime, "send_long_message"):
        await runtime.send_long_message(chat_id, text, request_id="power-command", purpose="command")
        return
    message = getattr(update, "message", None)
    if message is not None and hasattr(message, "reply_text"):
        await message.reply_text(text)


async def _trigger_after_delay(cmd: list[str], delay: float = 2.0) -> None:
    await asyncio.sleep(delay)
    subprocess.run(cmd, check=False)


async def sleep_command(runtime: Any, update: Any, context: Any) -> None:
    if sys.platform != "win32":
        await _send(runtime, update, "❌ /sleep is only supported on Windows.")
        return
    await _send(runtime, update, "😴 Going to sleep... Wake me via WOL when you're ready.")
    asyncio.ensure_future(_trigger_after_delay(
        ["powershell", "-NonInteractive", "-Command", "Suspend-Computer"]
    ))


async def hibernate_command(runtime: Any, update: Any, context: Any) -> None:
    if sys.platform != "win32":
        await _send(runtime, update, "❌ /hibernate is only supported on Windows.")
        return
    await _send(runtime, update, "💤 Hibernating... Wake me via WOL when you're ready.")
    asyncio.ensure_future(_trigger_after_delay(["shutdown", "/h"]))


COMMANDS = [
    RuntimeCommand(
        name="sleep",
        description="Put this PC into sleep mode (WOL-safe, not shutdown)",
        callback=sleep_command,
    ),
    RuntimeCommand(
        name="hibernate",
        description="Put this PC into hibernate mode (WOL-safe, not shutdown)",
        callback=hibernate_command,
    ),
]
