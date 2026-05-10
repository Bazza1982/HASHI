from __future__ import annotations

from typing import Any


async def cmd_park(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return

    args = [arg.strip() for arg in (context.args or []) if arg.strip()]
    if not args:
        await runtime._reply_text(update, runtime._format_parked_topics_text())
        return

    action = args[0].lower()
    if action == "delete":
        if len(args) < 2 or not args[1].isdigit():
            await runtime._reply_text(update, "Usage: /park delete <slot>")
            return
        slot_id = int(args[1])
        removed = runtime.parked_topics.delete_topic(slot_id)
        if not removed:
            await runtime._reply_text(update, f"Parked topic [{slot_id}] was not found.")
            return
        await runtime._reply_text(update, f"Deleted parked topic [{slot_id}] {removed.get('title') or ''}".strip())
        return

    if action != "chat":
        await runtime._reply_text(
            update,
            "Usage:\n"
            "/park - list parked topics\n"
            "/park chat [optional title] - park the current topic\n"
            "/park delete <slot> - delete a parked topic",
        )
        return

    if runtime._backend_busy():
        await runtime._reply_text(update, "Parking is blocked while a request is running or queued.")
        return

    title_override = " ".join(args[1:]).strip() or None
    await runtime._reply_text(update, "Parking the current topic and writing a resume summary...")
    summary = await runtime._summarize_current_topic_for_parking(title_override=title_override)
    if not summary:
        await runtime._reply_text(update, "No recent bridge transcript was available to park.")
        return

    topic = runtime.parked_topics.create_topic(
        title=summary["title"],
        summary_short=summary["summary_short"],
        summary_long=summary["summary_long"],
        recent_context=summary["recent_context"],
        last_user_text=summary["last_user_text"],
        last_assistant_text=summary["last_assistant_text"],
        last_exchange_text=summary["last_exchange_text"],
        source_session=runtime.session_id_dt,
        title_user_override=title_override,
    )
    slot_id = int(topic["slot_id"])
    await runtime._reply_text(
        update,
        f"Parked as [{slot_id}] {topic['title']}\n"
        f"{topic['summary_short']}\n\n"
        f"Follow-up reminders are scheduled for this parked topic (up to 3 attempts).\n"
        f"Use /load {slot_id} to resume or /park delete {slot_id} to remove it.",
    )
