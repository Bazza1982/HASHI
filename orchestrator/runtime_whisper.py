from __future__ import annotations

from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from orchestrator.voice_transcriber import get_transcriber


async def cmd_whisper(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return

    args = [arg.strip().lower() for arg in (context.args or []) if arg.strip()]
    transcriber = get_transcriber()

    if not args:
        current = transcriber.model_size
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ small" if current == "small" else "small", callback_data="tgl:whisper:small"),
            InlineKeyboardButton("✅ medium" if current == "medium" else "medium", callback_data="tgl:whisper:medium"),
            InlineKeyboardButton("✅ large" if str(current).startswith("large") else "large", callback_data="tgl:whisper:large"),
        ]])
        await runtime._reply_text(update, f"Whisper model: <b>{current}</b>", parse_mode="HTML", reply_markup=markup)
        return

    mapping = {
        "small": "small",
        "medium": "medium",
        "large": "large-v3",
        "large-v3": "large-v3",
    }
    value = args[0]
    if value not in mapping:
        await runtime._reply_text(update, "Usage: /whisper [small|medium|large]")
        return

    new_size = mapping[value]
    transcriber.model_size = new_size
    transcriber._model = None
    await runtime._reply_text(
        update,
        f"✅ Whisper model set to: {new_size}. It will load on the next voice message.",
    )
