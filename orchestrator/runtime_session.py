from __future__ import annotations

from typing import Any

from orchestrator.flexible_backend_registry import is_cli_backend


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


def _uses_cli_session_semantics(engine: str) -> bool:
    # HER v2 may route individual stage calls through CLI-capable providers,
    # but the HASHI-owned runtime itself is stateless across user turns.  Its
    # context reset contract is therefore /fresh, never /new.
    return str(engine or "").strip().lower() != "her-v2" and is_cli_backend(engine)


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
    if _uses_cli_session_semantics(engine):
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
    if not _uses_cli_session_semantics(runtime.config.active_backend):
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
    if _uses_cli_session_semantics(runtime.config.active_backend):
        await runtime._reply_text(
            update,
            "This agent is using a CLI backend. Use /new to reset the CLI session.",
        )
        return

    is_her_v2 = _active_engine(runtime) == "her-v2"
    if is_her_v2:
        try:
            from orchestrator.context_compaction import (
                cancel_runtime_compaction,
                reset_for_fresh_context,
            )
            from orchestrator.fresh_context import start_boundary
            from orchestrator.memory_search_mode import set_memory_search_enabled

            boundary = start_boundary(runtime)
            _prepare_clean_context(
                runtime,
                disable_saved_memory=True,
                clear_session_primer=True,
            )
            reset_for_fresh_context(
                runtime,
                boundary_generation=int(boundary["generation"]),
                cutoff_epoch=float(boundary["cutoff_epoch"]),
            )
            await cancel_runtime_compaction(runtime)

            workspace_dir = getattr(runtime, "workspace_dir", None)
            if workspace_dir is not None:
                set_memory_search_enabled(workspace_dir, False)
        except Exception as exc:
            logger = getattr(runtime, "logger", None)
            if logger is not None:
                logger.exception("Could not establish HER v2 /fresh boundary: %s", exc)
            await runtime._reply_text(
                update,
                "HER v2 could not establish a verified fresh-context boundary. "
                "No clean-context guarantee is being reported; stored logs and memories were not deleted.",
            )
            return
    else:
        _prepare_clean_context(
            runtime,
            disable_saved_memory=True,
            clear_session_primer=True,
        )

    await runtime._reply_text(update, "✨ Fresh session started.")
