from __future__ import annotations

from typing import Any


async def cmd_wa_on(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    orchestrator = getattr(runtime, "orchestrator", None)
    if orchestrator is None:
        await runtime._reply_text(update, "WhatsApp lifecycle control is unavailable.")
        return
    _, message = await orchestrator.start_whatsapp_transport(persist_enabled=True)
    await runtime._reply_text(update, message)


async def cmd_wa_off(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    orchestrator = getattr(runtime, "orchestrator", None)
    if orchestrator is None:
        await runtime._reply_text(update, "WhatsApp lifecycle control is unavailable.")
        return
    _, message = await orchestrator.stop_whatsapp_transport(persist_enabled=True)
    await runtime._reply_text(update, message)


async def cmd_wa_send(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    orchestrator = getattr(runtime, "orchestrator", None)
    if orchestrator is None:
        await runtime._reply_text(update, "WhatsApp send control is unavailable.")
        return
    args = context.args or []
    if len(args) < 2:
        await runtime._reply_text(update, "Usage: /wa_send <+number> <message>")
        return
    phone_number = args[0].strip()
    text = " ".join(args[1:]).strip()
    if not text:
        await runtime._reply_text(update, "Usage: /wa_send <+number> <message>")
        return
    _, message = await orchestrator.send_whatsapp_text(phone_number, text)
    await runtime._reply_text(update, message)
