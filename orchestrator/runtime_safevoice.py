from __future__ import annotations

from typing import Any

from orchestrator.command_registry import RuntimeCallback


async def cmd_safevoice(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return

    args = [arg.strip().lower() for arg in (context.args or []) if arg.strip()]
    if not args:
        status = "ON 🛡️" if runtime._safevoice_enabled else "OFF"
        await runtime._reply_text(update, f"Safe Voice: {status}\nUsage: /safevoice on | off")
        return

    if args[0] == "on":
        runtime._safevoice_enabled = True
        runtime._set_skill_state("safevoice", True)
        await runtime._reply_text(update, "🛡️ Safe Voice ON — voice messages will require confirmation before sending to agent.")
        return

    if args[0] == "off":
        runtime._safevoice_enabled = False
        runtime._set_skill_state("safevoice", False)
        runtime._pending_voice.clear()
        await runtime._reply_text(update, "Safe Voice OFF — voice messages go directly to agent.")
        return

    await runtime._reply_text(update, "Usage: /safevoice on | off")


async def callback_safevoice(runtime: Any, update: Any, context: Any) -> None:
    query = update.callback_query
    if not runtime._is_authorized_user(query.from_user.id):
        return
    parts = (query.data or "").split(":", 2)
    action = parts[1] if len(parts) > 1 else ""
    chat_key = parts[2] if len(parts) > 2 else ""
    pending = runtime._pending_voice.pop(chat_key, None)
    if action == "yes" and pending:
        await query.edit_message_text(f"✅ Confirmed. Sending to agent:\n\n_{pending['transcript']}_", parse_mode="Markdown")
        await query.answer("Sending...")
        await runtime.enqueue_request(int(chat_key), pending["prompt"], "voice_transcript", pending["summary"])
    elif action == "no":
        await query.edit_message_text("❌ Voice message discarded.")
        await query.answer("Discarded")
    else:
        await query.edit_message_text("⏰ Voice confirmation expired.")
        await query.answer("Expired")


CALLBACKS = [
    RuntimeCallback(pattern=r"^safevoice:", callback=callback_safevoice),
]
