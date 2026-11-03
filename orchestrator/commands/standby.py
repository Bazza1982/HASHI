"""Read-only Standby command on the shared native command registry."""

from __future__ import annotations

import asyncio
import html

from orchestrator import runtime_session, ui_language
from orchestrator.command_registry import RuntimeCommand
from orchestrator.command_ui import setting_card


async def standby_command(runtime, update, context):
    checker = getattr(runtime, "_is_authorized_user", None)
    actor = getattr(getattr(update, "effective_user", None), "id", None)
    if not callable(checker) or not checker(actor):
        await runtime._reply_text(update, ui_language.tr("standby.auth_required"))
        return
    args = list(getattr(context, "args", None) or [])
    if args not in ([], ["status"]):
        await runtime._reply_text(update, ui_language.tr("standby.usage"))
        return
    try:
        store = runtime_session.ensure_store(runtime)
        session = runtime_session.current_session_for_update(runtime, update)
        scope = {key: session[key] for key in ("agent_id", "session_id", "context_generation")}
        rows = await asyncio.to_thread(store.standby_recent,
                                       owner_id=session["owner_id"], binding=scope)
        text = setting_card(
            "👁", "Standby", current=html.escape(ui_language.tr("standby.config_only")),
            facts=[html.escape(ui_language.tr("standby.no_capture")),
                   html.escape(ui_language.tr("standby.unqualified")),
                   html.escape(ui_language.tr("standby.control_count", count=len(rows)))],
            consequence=html.escape(ui_language.tr("standby.no_actions")),
        )
    except Exception:
        text = html.escape(ui_language.tr("standby.control_unavailable"))
    await runtime._reply_text(update, text, parse_mode="HTML")


def get_commands():
    return [RuntimeCommand("standby", "Standby status and readiness", standby_command)]
