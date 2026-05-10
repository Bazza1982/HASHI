from __future__ import annotations

from typing import Any


async def cmd_timeout(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    args = [a.strip() for a in (context.args or []) if a.strip()]
    backend = getattr(runtime, "backend", None) or (
        runtime.backend_manager.current_backend if hasattr(runtime, "backend_manager") else None
    )
    extra = {}
    if backend and hasattr(backend, "config") and backend.config.extra:
        extra = backend.config.extra

    default_idle = getattr(type(backend), "DEFAULT_IDLE_TIMEOUT_SEC", 300) if backend else 300
    default_hard = getattr(type(backend), "DEFAULT_HARD_TIMEOUT_SEC", 1800) if backend else 1800

    if not args:
        idle_s = extra.get("idle_timeout_sec") or extra.get("process_timeout") or default_idle
        hard_s = extra.get("hard_timeout_sec") or default_hard
        idle_min = int(idle_s) // 60
        hard_min = int(hard_s) // 60
        def_idle_min = default_idle // 60
        def_hard_min = default_hard // 60
        text = (
            f"<b>⏱ Timeout — {runtime.name}</b>\n\n"
            f"  Idle:  <b>{idle_min} min</b>  (default: {def_idle_min} min)\n"
            f"  Hard:  <b>{hard_min} min</b>  (default: {def_hard_min} min)\n\n"
            f"Usage:\n"
            f"  <code>/timeout 30</code>        — set idle to 30 min\n"
            f"  <code>/timeout 30 120</code>    — idle=30 min, hard=120 min\n"
            f"  <code>/timeout reset</code>     — restore defaults"
        )
        await runtime._reply_text(update, text, parse_mode="HTML")
        return

    if args[0].lower() == "reset":
        if backend and hasattr(backend, "config") and backend.config.extra:
            backend.config.extra.pop("idle_timeout_sec", None)
            backend.config.extra.pop("hard_timeout_sec", None)
            backend.config.extra.pop("process_timeout", None)
        def_idle_min = default_idle // 60
        def_hard_min = default_hard // 60
        await runtime._reply_text(
            update,
            f"⏱ Timeout reset to defaults: idle={def_idle_min} min, hard={def_hard_min} min",
        )
        return

    try:
        idle_min = int(args[0])
        if idle_min <= 0:
            raise ValueError
    except ValueError:
        await runtime._reply_text(update, "Usage: /timeout [minutes] [hard_minutes] | reset")
        return

    hard_min = None
    if len(args) >= 2:
        try:
            hard_min = int(args[1])
            if hard_min <= 0:
                raise ValueError
        except ValueError:
            await runtime._reply_text(update, "Usage: /timeout [minutes] [hard_minutes] | reset")
            return

    if backend and hasattr(backend, "config"):
        if backend.config.extra is None:
            backend.config.extra = {}
        backend.config.extra["idle_timeout_sec"] = idle_min * 60
        backend.config.extra.pop("process_timeout", None)
        if hard_min is not None:
            backend.config.extra["hard_timeout_sec"] = hard_min * 60

    hard_str = f", hard={hard_min} min" if hard_min is not None else ""
    await runtime._reply_text(update, f"⏱ Timeout updated: idle={idle_min} min{hard_str}")
