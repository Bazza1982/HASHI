from __future__ import annotations

from typing import Any


async def cmd_active(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    if not runtime.skill_manager:
        await runtime._reply_text(update, "Active mode is unavailable because the skill manager is not configured.")
        return

    args = [a.strip().lower() for a in (context.args or []) if a.strip()]
    if not args:
        status = runtime.skill_manager.describe_active_heartbeat(runtime.name)
        markup = runtime._active_keyboard()
        await runtime._reply_text(update, status, reply_markup=markup)
        return

    mode = args[0]
    if mode == "off":
        _, message = runtime.skill_manager.set_active_heartbeat(runtime.name, enabled=False)
        await runtime._reply_text(update, message)
        return
    if mode != "on":
        await runtime._reply_text(update, "Usage: /active on [minutes] | /active off")
        return

    minutes = runtime.skill_manager.ACTIVE_HEARTBEAT_DEFAULT_MINUTES
    if len(args) > 1:
        try:
            minutes = max(1, int(args[1]))
        except ValueError:
            await runtime._reply_text(update, "Minutes must be a positive integer. Usage: /active on [minutes]")
            return

    _, message = runtime.skill_manager.set_active_heartbeat(runtime.name, enabled=True, minutes=minutes)
    await runtime._reply_text(update, message)
