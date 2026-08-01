from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

from orchestrator.runtime_common import _print_user_message, _safe_excerpt


def active_session_id(runtime: Any, chat_id: int) -> str | None:
    """Return the active /long session for this chat, if any."""
    if not runtime._long_buffer_active or runtime._long_buffer_chat_id != chat_id:
        return None
    return runtime._long_buffer_session_id


def buffer_chunk(
    runtime: Any,
    chat_id: int,
    text: str,
    *,
    session_id: str | None = None,
) -> bool:
    """Append one text or media prompt to the matching /long session."""
    current_session_id = active_session_id(runtime, chat_id)
    if current_session_id is None:
        return False
    if session_id is not None and session_id != current_session_id:
        return False
    if text and text.strip():
        runtime._long_buffer.append(text)
    return True


async def cmd_long(runtime: Any, update: Any, context: Any) -> None:
    """Start collecting multi-part text and media input."""
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    if runtime._long_buffer_active:
        await runtime._reply_text(update, "⏳ Already in /long mode. Send /end to finish.")
        return

    runtime._long_buffer = []
    runtime._long_buffer_active = True
    runtime._long_buffer_chat_id = update.effective_chat.id
    runtime._long_buffer_session_id = uuid4().hex
    args_text = " ".join(context.args).strip() if context.args else ""
    if args_text:
        runtime._long_buffer.append(args_text)
    if runtime._long_buffer_timeout_task and not runtime._long_buffer_timeout_task.done():
        runtime._long_buffer_timeout_task.cancel()
    runtime._long_buffer_timeout_task = asyncio.create_task(runtime._long_buffer_timeout())
    await runtime._reply_text(
        update,
        "📝 /long mode started. Send text, photos, voice/audio, or files, then send /end to submit.",
    )


async def cmd_end(runtime: Any, update: Any, _context: Any) -> None:
    """End /long buffering and submit all collected input as one request."""
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    if not runtime._long_buffer_active:
        await runtime._reply_text(update, "No /long session active.")
        return

    session_id = active_session_id(runtime, update.effective_chat.id)
    if session_id and any(
        pending.get("long_session_id") == session_id
        for pending in runtime._pending_voice.values()
    ):
        await runtime._reply_text(
            update,
            "⚠️ Confirm or discard the pending Safe Voice transcript before /end.",
        )
        return

    if runtime._long_buffer_timeout_task and not runtime._long_buffer_timeout_task.done():
        runtime._long_buffer_timeout_task.cancel()
        runtime._long_buffer_timeout_task = None
    item_count = len(runtime._long_buffer)
    combined = "\n".join(runtime._long_buffer).strip()
    runtime._long_buffer = []
    runtime._long_buffer_active = False
    chat_id = runtime._long_buffer_chat_id or update.effective_chat.id
    runtime._long_buffer_chat_id = None
    runtime._long_buffer_session_id = None
    if not combined:
        await runtime._reply_text(update, "⚠️ /long buffer was empty, nothing to submit.")
        return

    await runtime._reply_text(update, f"✅ Collected {item_count} items. Submitting...")
    _print_user_message(runtime.name, combined)
    await runtime.enqueue_request(chat_id, combined, "text", _safe_excerpt(combined))


async def buffer_timeout(runtime: Any) -> None:
    """Auto-submit an abandoned /long session after five minutes."""
    try:
        await asyncio.sleep(300)
    except asyncio.CancelledError:
        return
    if not runtime._long_buffer_active:
        return

    item_count = len(runtime._long_buffer)
    combined = "\n".join(runtime._long_buffer).strip()
    runtime._long_buffer = []
    runtime._long_buffer_active = False
    chat_id = runtime._long_buffer_chat_id
    runtime._long_buffer_chat_id = None
    runtime._long_buffer_session_id = None
    runtime._long_buffer_timeout_task = None
    if chat_id and combined:
        await runtime.send_long_message(
            chat_id,
            f"⏰ /long auto-submitted after 5min timeout ({item_count} items).",
            request_id=f"long-timeout-{uuid4().hex[:8]}",
            purpose="long-timeout",
        )
        _print_user_message(runtime.name, combined)
        await runtime.enqueue_request(chat_id, combined, "text", _safe_excerpt(combined))
    elif chat_id:
        await runtime.send_long_message(
            chat_id,
            "⏰ /long timed out with empty buffer. Cancelled.",
            request_id=f"long-timeout-{uuid4().hex[:8]}",
            purpose="long-timeout",
        )
