from __future__ import annotations

from typing import Any

from orchestrator.flexible_backend_registry import is_cli_backend
from orchestrator.wrapper_mode import SESSION_RESET_SOURCE


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
    runtime._clear_transfer_state()
    runtime._pending_auto_recall_context = None

    memory_store = getattr(getattr(runtime, "context_assembler", None), "memory_store", None)
    if memory_store is not None and hasattr(memory_store, "clear_turns"):
        memory_store.clear_turns()

    backend = runtime.backend_manager.current_backend
    if runtime.backend_manager.agent_mode == "fixed":
        if hasattr(backend, "handle_new_session"):
            await backend.handle_new_session()
        if hasattr(backend, "current_proc") and backend.current_proc:
            await backend.force_kill_process_tree(
                backend.current_proc,
                logger=runtime.logger,
                reason="cmd_new_fixed_mode",
            )
            backend.current_proc = None
        await runtime._reply_text(update, "Fixed mode: session terminated. Starting fresh...")
    elif getattr(backend.capabilities, "supports_sessions", False):
        await backend.handle_new_session()
        await runtime._reply_text(update, "Starting a fresh session...")
    else:
        await runtime._reply_text(update, "Starting a fresh stateless session...")

    prompt = (
        "SYSTEM: Fresh session started. Do not reference any previous chat. "
        "Follow ONLY your agent.md instructions. Ask the user what they want to do next."
    )
    await runtime.enqueue_request(
        update.effective_chat.id,
        prompt,
        SESSION_RESET_SOURCE,
        "New session",
        skip_memory_injection=True,
    )


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

    runtime._clear_transfer_state()
    runtime._pending_auto_recall_context = None
    assembler = getattr(runtime, "context_assembler", None)
    memory_store = getattr(assembler, "memory_store", None)
    if memory_store is not None and hasattr(memory_store, "clear_turns"):
        memory_store.clear_turns()
    if assembler is not None:
        assembler.turns_injection_enabled = True
        assembler.saved_memory_injection_enabled = False

    await runtime._reply_text(
        update,
        "Starting a fresh API context. Recent turns were cleared; saved memories are preserved but will not be auto-injected.",
    )
    prompt = (
        "SYSTEM: Fresh API context started. Do not reference previous chat or saved memories unless the user explicitly asks. "
        "Follow ONLY your agent.md instructions. Ask the user what they want to do next."
    )
    await runtime.enqueue_request(
        update.effective_chat.id,
        prompt,
        SESSION_RESET_SOURCE,
        "Fresh API context",
        skip_memory_injection=True,
    )
