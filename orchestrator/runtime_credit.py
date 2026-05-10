from __future__ import annotations

from typing import Any


async def cmd_credit(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return

    backend = runtime.backend_manager.current_backend
    if not backend or not hasattr(backend, "get_key_info"):
        await runtime._reply_text(update, "Credit info is only available for OpenRouter backends.")
        return

    key_info = await backend.get_key_info()
    if not key_info:
        await runtime._reply_text(update, "Failed to fetch credit info.")
        return

    data = key_info.get("data", {})
    label = data.get("label", "unknown")
    usage = data.get("usage", "unknown")
    limit = data.get("limit", "unknown")
    limit_remaining = data.get("limit_remaining", "unknown")
    is_free_tier = data.get("is_free_tier", False)
    await runtime._reply_text(
        update,
        f"OpenRouter key: {label}\n"
        f"Usage: {usage}\n"
        f"Limit: {limit}\n"
        f"Remaining: {limit_remaining}\n"
        f"Free tier: {is_free_tier}",
    )
