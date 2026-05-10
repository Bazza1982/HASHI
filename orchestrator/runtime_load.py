from __future__ import annotations

from typing import Any


async def cmd_load(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return

    args = [arg.strip() for arg in (context.args or []) if arg.strip()]
    if len(args) != 1 or not args[0].isdigit():
        await runtime._reply_text(update, "Usage: /load <slot>")
        return

    if runtime._backend_busy():
        await runtime._reply_text(update, "Load is blocked while a request is running or queued.")
        return

    slot_id = int(args[0])
    topic = runtime.parked_topics.get_topic(slot_id)
    if not topic:
        await runtime._reply_text(update, f"Parked topic [{slot_id}] was not found.")
        return

    runtime.parked_topics.mark_loaded(slot_id)
    title = topic.get("title") or f"Topic {slot_id}"
    summary_short = topic.get("summary_short") or ""
    summary_long = topic.get("summary_long") or ""
    recent_context = topic.get("recent_context") or ""
    last_exchange = topic.get("last_exchange_text") or ""
    runtime._pending_auto_recall_context = (
        "Restore the parked topic below as active continuity context. "
        "Use it as current working context for this session.\n\n"
        f"--- PARKED TOPIC [{slot_id}] ---\n"
        f"Title: {title}\n"
        f"Short Summary: {summary_short}\n\n"
        f"Long Summary:\n{summary_long}\n\n"
        f"Last Exchange:\n{last_exchange or '(none)'}\n\n"
        f"{recent_context}"
    )
    runtime._arm_session_primer(
        f"Loading parked topic [{slot_id}] {title}. Resume it as the active working context."
    )
    await runtime._reply_text(update, f"Loading parked topic [{slot_id}] {title} and restoring continuity...")
    await runtime.enqueue_request(
        update.effective_chat.id,
        (
            "SYSTEM: Resume the parked topic that was just restored into context. "
            "Continue naturally from the most relevant unfinished point. "
            "Do not explain the restore process at length.\n\n"
            "Resume the topic now."
        ),
        "park-load",
        f"Parked topic load [{slot_id}]",
    )
