from __future__ import annotations

import asyncio
import json
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


async def cmd_stop(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return

    runtime.logger.warning(
        f"Manual stop requested for flex agent {runtime.name} "
        f"(queue_size={runtime.queue.qsize()}, backend={runtime.config.active_backend})"
    )
    if runtime.backend_manager.current_backend:
        await runtime.backend_manager.current_backend.shutdown()

    dropped = 0
    while not runtime.queue.empty():
        try:
            runtime.queue.get_nowait()
            runtime.queue.task_done()
            dropped += 1
        except asyncio.QueueEmpty:
            break

    await runtime._reply_text(
        update,
        f"Stopped execution. Cleared {dropped} queued messages and killed active backend process tree.",
    )


async def cmd_retry(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    args = [a.strip().lower() for a in (context.args or []) if a.strip()]
    mode = args[0] if args else "response"
    chat_id = update.effective_chat.id
    if mode in {"response", "resp"}:
        await retry_response(runtime, update, chat_id)
        return
    if mode in {"prompt", "req", "request"}:
        await retry_prompt(runtime, update, chat_id)
        return
    markup = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("重发回复", callback_data="tgl:retry:response"),
                InlineKeyboardButton("重跑 Prompt", callback_data="tgl:retry:prompt"),
            ]
        ]
    )
    await runtime._reply_text(update, "Retry — choose action:", reply_markup=markup)


async def callback_retry_toggle(runtime: Any, query: Any, value: str) -> None:
    chat_id = query.message.chat_id
    await query.answer(f"Retrying {value}...")
    if value == "response":
        if runtime.last_response:
            await runtime.send_long_message(
                chat_id=runtime.last_response["chat_id"],
                text=runtime.last_response["text"],
                request_id=runtime.last_response.get("request_id"),
                purpose="retry-response",
            )
            return
        transcript_text = load_last_text_from_transcript(runtime, "assistant")
        if transcript_text:
            await runtime.send_long_message(chat_id=chat_id, text=transcript_text, purpose="retry-response")
        elif runtime.last_prompt:
            await runtime.enqueue_request(runtime.last_prompt.chat_id, runtime.last_prompt.prompt, "retry", "Retry request")
        else:
            await query.answer("Nothing to retry.", show_alert=True)
        return

    if runtime.last_prompt:
        await runtime.enqueue_request(runtime.last_prompt.chat_id, runtime.last_prompt.prompt, "retry", "Retry request")
        return
    transcript_text = load_last_text_from_transcript(runtime, "user")
    if transcript_text:
        await runtime.enqueue_request(chat_id, transcript_text, "retry", "Retry request")
    else:
        await query.answer("No previous prompt.", show_alert=True)


async def retry_response(runtime: Any, update: Any, chat_id: int) -> None:
    if not runtime.last_response:
        transcript_text = load_last_text_from_transcript(runtime, "assistant")
        if transcript_text:
            await runtime._reply_text(update, "Restoring last response from transcript...")
            await runtime.send_long_message(
                chat_id=chat_id,
                text=transcript_text,
                purpose="retry-response",
            )
            return
        if runtime.last_prompt:
            await runtime._reply_text(update, "No cached response — retrying last prompt...")
            await runtime.enqueue_request(
                runtime.last_prompt.chat_id,
                runtime.last_prompt.prompt,
                "retry",
                "Retry request",
            )
        else:
            await runtime._reply_text(update, "Nothing to retry — no previous response or prompt.")
        return

    await runtime._reply_text(update, "Resending last response...")
    await runtime.send_long_message(
        chat_id=runtime.last_response["chat_id"],
        text=runtime.last_response["text"],
        request_id=runtime.last_response.get("request_id"),
        purpose="retry-response",
    )


async def retry_prompt(runtime: Any, update: Any, chat_id: int) -> None:
    if not runtime.last_prompt:
        transcript_text = load_last_text_from_transcript(runtime, "user")
        if transcript_text:
            await runtime._reply_text(update, "Restoring last prompt from transcript...")
            await runtime.enqueue_request(chat_id, transcript_text, "retry", "Retry request")
        else:
            await runtime._reply_text(update, "No previous prompt to rerun.")
        return

    await runtime._reply_text(update, "Retrying last prompt...")
    await runtime.enqueue_request(
        runtime.last_prompt.chat_id,
        runtime.last_prompt.prompt,
        "retry",
        "Retry request",
    )


def load_last_text_from_transcript(runtime: Any, role: str) -> str | None:
    try:
        if not runtime.transcript_log_path.exists():
            return None
        last_text = None
        with open(runtime.transcript_log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("role") == role and entry.get("text"):
                        last_text = entry["text"]
                except Exception:
                    pass
        return last_text
    except Exception:
        return None
