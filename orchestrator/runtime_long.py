from __future__ import annotations

import asyncio
from typing import Any

from orchestrator.runtime_common import _print_user_message, _safe_excerpt


async def cmd_long(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    if runtime._long_buffer_active:
        await runtime._reply_text(update, "⏳ Already in /long mode. Send /end to finish.")
        return
    runtime._long_buffer = []
    runtime._long_buffer_active = True
    runtime._long_buffer_chat_id = update.effective_chat.id
    args_text = " ".join(context.args).strip() if context.args else ""
    if args_text:
        runtime._long_buffer.append(args_text)
    if runtime._long_buffer_timeout_task and not runtime._long_buffer_timeout_task.done():
        runtime._long_buffer_timeout_task.cancel()
    runtime._long_buffer_timeout_task = asyncio.create_task(runtime._long_buffer_timeout())
    await runtime._reply_text(update, "📝 /long mode started. Paste your text, then send /end to submit.")


async def cmd_end(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    if not runtime._long_buffer_active:
        await runtime._reply_text(update, "No /long session active.")
        return
    if runtime._long_buffer_timeout_task and not runtime._long_buffer_timeout_task.done():
        runtime._long_buffer_timeout_task.cancel()
        runtime._long_buffer_timeout_task = None
    combined = "\n".join(runtime._long_buffer).strip()
    runtime._long_buffer = []
    runtime._long_buffer_active = False
    chat_id = runtime._long_buffer_chat_id or update.effective_chat.id
    runtime._long_buffer_chat_id = None
    if not combined:
        await runtime._reply_text(update, "⚠️ /long buffer was empty, nothing to submit.")
        return
    chunk_count = len(combined.splitlines())
    await runtime._reply_text(update, f"✅ Collected {chunk_count} lines. Submitting...")
    _print_user_message(runtime.name, combined)
    await runtime.enqueue_request(chat_id, combined, "text", _safe_excerpt(combined))
