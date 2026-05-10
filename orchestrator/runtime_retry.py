from __future__ import annotations

from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


async def cmd_retry(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return

    args = [arg.strip().lower() for arg in (context.args or []) if arg.strip()]
    mode = args[0] if args else "response"
    chat_id = update.effective_chat.id

    if mode in {"response", "resp"}:
        if not runtime.last_response:
            transcript_text = runtime._load_last_text_from_transcript("assistant")
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
        return

    if mode in {"prompt", "req", "request"}:
        if not runtime.last_prompt:
            transcript_text = runtime._load_last_text_from_transcript("user")
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
        return

    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("重发回复", callback_data="tgl:retry:response"),
        InlineKeyboardButton("重跑 Prompt", callback_data="tgl:retry:prompt"),
    ]])
    await runtime._reply_text(update, "Retry — choose action:", reply_markup=markup)
