from __future__ import annotations

import time
from typing import Any


async def cmd_say(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return

    text = runtime._load_last_text_from_transcript("assistant")
    if not text:
        await runtime._reply_text(update, "No recent message to read.")
        return

    chat_id = update.effective_chat.id
    request_id = f"say-{int(time.time())}"
    ok = await runtime._send_voice_reply(chat_id, text, request_id, force=True)
    if not ok:
        await runtime._reply_text(update, "Voice synthesis failed. Check /voice status for provider settings.")
