from __future__ import annotations

from typing import Any


async def cmd_effort(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    if not runtime.backend_manager.current_backend:
        return

    available = runtime._get_available_efforts()
    if not available:
        await runtime._reply_text(update, "Effort control is only available when the active backend is Claude or Codex.")
        return

    args = context.args
    if args:
        requested = args[0].strip().lower()
        if requested == "extra":
            requested = "extra_high"
        if requested not in available:
            await runtime._reply_text(update, f"Unknown effort level: {requested}\nAvailable: {', '.join(available)}")
            return
        runtime._set_active_effort(requested)
        await runtime._reply_text(update, f"Effort switched to: {requested}")
        return

    current_effort = runtime._get_current_effort() or available[0]
    await runtime._reply_text(
        update,
        f"Current effort: {current_effort}\nSelect:",
        reply_markup=runtime._effort_keyboard(current_effort),
    )
