from __future__ import annotations

from typing import Any

from orchestrator.command_registry import RuntimeCallback
from orchestrator.runtime_superloop import handle_superloop_callback


async def superloop_callback(runtime: Any, update: Any, context: Any) -> None:
    query = getattr(update, "callback_query", None)
    if query is None:
        return

    checker = getattr(runtime, "_is_authorized_user", None)
    user_id = getattr(getattr(query, "from_user", None), "id", None)
    if callable(checker) and not checker(user_id):
        await query.answer()
        return

    channel_guard = getattr(runtime, "_telegram_channel_allowed", None)
    if callable(channel_guard):
        allowed = await channel_guard(update, source_channel="telegram_callback")
        if not allowed:
            return

    await handle_superloop_callback(runtime, update, context)


CALLBACKS = [
    RuntimeCallback(pattern=r"^superloop:", callback=superloop_callback),
]
