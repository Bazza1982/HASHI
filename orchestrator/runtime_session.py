from __future__ import annotations

from typing import Any

from orchestrator.flexible_backend_registry import is_cli_backend
from orchestrator.memory_plus_mode import is_memory_plus_enabled


def _active_engine(runtime: Any) -> str:
    config = getattr(runtime, "config", None)
    return str(
        getattr(config, "active_backend", None)
        or getattr(config, "engine", "")
        or ""
    )


def _active_backend(runtime: Any) -> Any:
    backend_manager = getattr(runtime, "backend_manager", None)
    if backend_manager is not None:
        return getattr(backend_manager, "current_backend", None)
    return getattr(runtime, "backend", None)


def _prepare_clean_context(
    runtime: Any,
    *,
    disable_saved_memory: bool,
    clear_session_primer: bool = False,
) -> None:
    clear_transfer_state = getattr(runtime, "_clear_transfer_state", None)
    if callable(clear_transfer_state):
        clear_transfer_state()
    runtime._pending_auto_recall_context = None
    if clear_session_primer:
        runtime._pending_session_primer = None

    assembler = getattr(runtime, "context_assembler", None)
    memory_store = getattr(assembler, "memory_store", None)
    if memory_store is None:
        memory_store = getattr(runtime, "memory_store", None)
    if memory_store is not None and hasattr(memory_store, "clear_turns"):
        memory_store.clear_turns()

    if assembler is not None and disable_saved_memory:
        assembler.turns_injection_enabled = True
        assembler.saved_memory_injection_enabled = False


async def _reset_cli_backend(runtime: Any, *, reason: str) -> str:
    backend = _active_backend(runtime)
    if backend is None:
        return "unavailable"

    backend_manager = getattr(runtime, "backend_manager", None)
    agent_mode = getattr(backend_manager, "agent_mode", None)
    supports_sessions = bool(
        getattr(getattr(backend, "capabilities", None), "supports_sessions", False)
    )

    if agent_mode == "fixed":
        if hasattr(backend, "handle_new_session"):
            await backend.handle_new_session()
        if hasattr(backend, "current_proc") and backend.current_proc:
            await backend.force_kill_process_tree(
                backend.current_proc,
                logger=runtime.logger,
                reason=reason,
            )
            backend.current_proc = None
        return "fixed"

    if supports_sessions and hasattr(backend, "handle_new_session"):
        await backend.handle_new_session()
        return "session"
    return "stateless"


async def reset_for_retry(runtime: Any) -> str:
    """Apply /new or /fresh context semantics without queuing a visible reset turn."""
    engine = _active_engine(runtime)
    if is_cli_backend(engine):
        _prepare_clean_context(
            runtime,
            disable_saved_memory=False,
            clear_session_primer=True,
        )
        await _reset_cli_backend(runtime, reason="cmd_retry_cli_reset")
        return "new"

    _prepare_clean_context(
        runtime,
        disable_saved_memory=True,
        clear_session_primer=True,
    )
    return "fresh"


async def cmd_new(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    if not runtime.backend_manager.current_backend:
        return
    if not is_cli_backend(runtime.config.active_backend):
        await runtime._reply_text(
            update,
            "This agent is using a non-CLI backend. Use /fresh for a clean API context; /new is reserved for CLI session reset.",
        )
        return
    _prepare_clean_context(runtime, disable_saved_memory=False)
    reset_mode = await _reset_cli_backend(runtime, reason="cmd_new_fixed_mode")
    if reset_mode == "fixed":
        await runtime._reply_text(update, "Fixed mode: session terminated. Starting fresh...")
    elif reset_mode == "session":
        await runtime._reply_text(update, "Starting a fresh session...")
    else:
        await runtime._reply_text(update, "Starting a fresh stateless session...")


async def cmd_fresh(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    if not runtime.backend_manager.current_backend:
        return
    if is_cli_backend(runtime.config.active_backend):
        await runtime._reply_text(
            update,
            "This agent is using a CLI backend. Use /new to reset the CLI session.",
        )
        return

    _prepare_clean_context(runtime, disable_saved_memory=True)

    workspace_dir = getattr(runtime, "workspace_dir", None)
    continuity_enabled = bool(workspace_dir and is_memory_plus_enabled(workspace_dir))
    await runtime._reply_text(
        update,
        "Starting a fresh API context. Recent turns were cleared; saved memories are preserved but will not be auto-injected."
        + (
            " Memory+ continuity remains enabled; use `/memory plus off` if a turn must exclude the work card."
            if continuity_enabled
            else ""
        ),
        parse_mode="Markdown",
    )
