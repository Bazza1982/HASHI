from __future__ import annotations

from typing import Any


async def cmd_fyi(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return

    prompt = runtime._build_fyi_request_prompt(" ".join(context.args or []))
    await runtime._reply_text(update, "Refreshing AGENT FYI...")
    await runtime.enqueue_request(
        update.effective_chat.id,
        prompt,
        "fyi",
        "AGENT FYI refresh",
    )
