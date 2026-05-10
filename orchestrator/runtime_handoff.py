from __future__ import annotations

from typing import Any


async def cmd_handoff(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return

    if runtime._backend_busy():
        await runtime._reply_text(update, "Handoff is blocked while a request is running or queued.")
        return

    await runtime._reply_text(update, "Starting a fresh session with recent bridge history...")
    runtime.handoff_builder.refresh_recent_context()
    runtime.handoff_builder.build_handoff()
    prompt, exchange_count, word_count = runtime.handoff_builder.build_session_restore_prompt(
        max_rounds=10,
        max_words=6000,
    )
    if exchange_count <= 0:
        await runtime._send_text(update.effective_chat.id, "No recent bridge transcript was available for handoff.")
        return

    runtime._arm_session_primer(
        "This is a bridge-managed handoff restore. Review AGENT FYI, then use the recent transcript as continuity context."
    )
    backend = runtime.backend_manager.current_backend
    if backend and getattr(backend.capabilities, "supports_sessions", False):
        await backend.handle_new_session()
        await runtime.enqueue_startup_bootstrap(update.effective_chat.id)

    await runtime._send_text(
        update.effective_chat.id,
        f"Handoff prepared from {exchange_count} recent exchanges ({word_count} words). Restoring continuity now...",
    )
    await runtime.enqueue_request(
        update.effective_chat.id,
        prompt,
        "handoff",
        f"Handoff restore [{exchange_count} exchanges]",
    )
