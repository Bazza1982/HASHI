from __future__ import annotations

from typing import Any

from adapters.timeout_policy import (
    refresh_timeout_extra,
    timeout_policy_snapshot,
    validate_timeout_pair,
)
from orchestrator import runtime_menu_views
from orchestrator.backend_timeout import (
    clear_timeout_override,
    set_timeout_override,
)
from orchestrator.workspace_state import WorkspaceStateStore


USAGE = "Usage: /timeout [idle_minutes] [hard_minutes] | reset"


def _active_backend(runtime: Any) -> Any | None:
    manager = getattr(runtime, "backend_manager", None)
    if manager is not None:
        backend = getattr(manager, "current_backend", None)
        if backend is not None:
            return backend
    return getattr(runtime, "backend", None)


async def _reply(runtime: Any, update: Any, text: str, **kwargs: Any) -> None:
    reply = getattr(runtime, "_reply_text", None)
    if callable(reply):
        await reply(update, text, **kwargs)
        return
    await update.message.reply_text(text, **kwargs)


def _minutes(seconds: int) -> int:
    return max(1, int(seconds) // 60)


def _set_runtime_override(
    runtime: Any,
    backend: Any,
    *,
    idle_seconds: int,
    hard_seconds: int | None,
) -> Any:
    manager = getattr(runtime, "backend_manager", None)
    if manager is not None and hasattr(manager, "set_active_timeout_override"):
        return manager.set_active_timeout_override(
            idle_seconds=idle_seconds,
            hard_seconds=hard_seconds,
        )

    engine = str(backend.config.engine)
    store = WorkspaceStateStore(backend.config.workspace_dir)
    override = set_timeout_override(
        store,
        engine,
        idle_seconds=idle_seconds,
        hard_seconds=hard_seconds,
    )
    if backend.config.extra is None:
        backend.config.extra = {}
    refresh_timeout_extra(
        backend.config.extra,
        engine=engine,
        persisted_override=override,
    )
    backend._validate_timeout_configuration()
    return timeout_policy_snapshot(backend)


def _reset_runtime_override(runtime: Any, backend: Any) -> Any:
    manager = getattr(runtime, "backend_manager", None)
    if manager is not None and hasattr(manager, "clear_active_timeout_override"):
        return manager.clear_active_timeout_override()

    engine = str(backend.config.engine)
    store = WorkspaceStateStore(backend.config.workspace_dir)
    clear_timeout_override(store, engine)
    if backend.config.extra is None:
        backend.config.extra = {}
    refresh_timeout_extra(
        backend.config.extra,
        engine=engine,
        persisted_override={},
    )
    backend._validate_timeout_configuration()
    return timeout_policy_snapshot(backend)


async def cmd_timeout(runtime: Any, update: Any, context: Any) -> None:
    args = [str(value).strip() for value in (getattr(context, "args", None) or []) if str(value).strip()]
    backend = _active_backend(runtime)
    if backend is None:
        await _reply(runtime, update, "No active execution backend is available.")
        return

    try:
        policy = timeout_policy_snapshot(backend)
    except (TypeError, ValueError) as exc:
        await _reply(runtime, update, f"Timeout configuration is invalid: {exc}")
        return

    if not args:
        text = runtime_menu_views.timeout_menu_text(
            agent_name=str(getattr(runtime, "name", backend.config.name)),
            backend_name=policy.engine,
            idle_minutes=_minutes(policy.idle_seconds),
            hard_minutes=_minutes(policy.hard_seconds),
            default_idle_minutes=_minutes(policy.default_idle_seconds),
            default_hard_minutes=_minutes(policy.default_hard_seconds),
            idle_source=policy.idle_source,
            hard_source=policy.hard_source,
        )
        await _reply(runtime, update, text, parse_mode="HTML")
        return

    if args[0].lower() == "reset":
        if len(args) != 1:
            await _reply(runtime, update, USAGE)
            return
        try:
            reset_policy = _reset_runtime_override(runtime, backend)
        except (OSError, TypeError, ValueError, RuntimeError) as exc:
            await _reply(runtime, update, f"Could not reset timeout configuration: {exc}")
            return
        await _reply(
            runtime,
            update,
            "⏱ Backend timeout user override cleared "
            f"for {reset_policy.engine}: idle={_minutes(reset_policy.idle_seconds)} min "
            f"({reset_policy.idle_source}), hard={_minutes(reset_policy.hard_seconds)} min "
            f"({reset_policy.hard_source}).",
        )
        return

    if len(args) > 2:
        await _reply(runtime, update, USAGE)
        return
    try:
        idle_minutes = int(args[0])
        hard_minutes = int(args[1]) if len(args) == 2 else None
        if idle_minutes <= 0 or (hard_minutes is not None and hard_minutes <= 0):
            raise ValueError
    except ValueError:
        await _reply(runtime, update, USAGE)
        return

    idle_seconds = idle_minutes * 60
    hard_seconds = hard_minutes * 60 if hard_minutes is not None else None
    try:
        validate_timeout_pair(
            idle_seconds,
            hard_seconds if hard_seconds is not None else policy.hard_seconds,
        )
    except ValueError:
        await _reply(
            runtime,
            update,
            "Hard timeout must be greater than or equal to idle timeout. "
            "Use /timeout <idle_minutes> <hard_minutes>.",
        )
        return

    try:
        saved_policy = _set_runtime_override(
            runtime,
            backend,
            idle_seconds=idle_seconds,
            hard_seconds=hard_seconds,
        )
    except (OSError, TypeError, ValueError, RuntimeError) as exc:
        await _reply(runtime, update, f"Could not save timeout configuration: {exc}")
        return

    await _reply(
        runtime,
        update,
        f"⏱ Backend timeout saved for {saved_policy.engine}: "
        f"idle={_minutes(saved_policy.idle_seconds)} min, "
        f"hard={_minutes(saved_policy.hard_seconds)} min. "
        "This user override remains active until /timeout reset.",
    )
