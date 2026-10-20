"""Owner-scoped AgentPresence Connector controls.

This public RuntimeCommand is discovered by the shared command registry, so it
is available through every command-capable Frontend Connector without a
transport-specific registration or Backend/API special case.
"""

from __future__ import annotations

from typing import Any

from orchestrator import runtime_menu_views, runtime_session, ui_language
from orchestrator.command_registry import RuntimeCommand
from orchestrator.connector_delivery_preferences import (
    get_connector_preference,
    set_connector_preference,
)


CONNECTOR_ID = "agentpresence"


def _is_authorized(runtime: Any, update: Any) -> bool:
    checker = getattr(runtime, "_is_authorized_user", None)
    user = getattr(update, "effective_user", None)
    user_id = getattr(user, "id", None)
    if callable(checker):
        return bool(checker(user_id))
    authorized_id = getattr(
        getattr(runtime, "global_config", None), "authorized_id", None
    )
    return authorized_id is None or user_id == authorized_id


def _owner_id(runtime: Any, update: Any) -> str:
    explicit = str(getattr(update, "_hashi_session_owner_id", None) or "").strip()
    return runtime_session.owner_id(runtime, explicit or None)


async def _send(runtime: Any, update: Any, text: str) -> None:
    if hasattr(runtime, "_reply_text"):
        await runtime._reply_text(update, text, parse_mode="HTML")
        return
    message = getattr(update, "message", None)
    if message is not None and hasattr(message, "reply_text"):
        await message.reply_text(text, parse_mode="HTML")
        return
    chat_id = getattr(getattr(update, "effective_chat", None), "id", None)
    if chat_id is not None and hasattr(runtime, "send_long_message"):
        await runtime.send_long_message(
            chat_id,
            text,
            request_id="agentpresence-command",
            purpose="command",
        )


async def agentpresence_command(runtime: Any, update: Any, context: Any) -> None:
    if not _is_authorized(runtime, update):
        return
    bridge_home = getattr(getattr(runtime, "global_config", None), "bridge_home", None)
    if bridge_home is None:
        await _send(runtime, update, ui_language.tr("menu.agentpresence.unavailable"))
        return
    args = [
        str(arg).strip().casefold()
        for arg in (getattr(context, "args", None) or ())
        if str(arg).strip()
    ]
    owner_id = _owner_id(runtime, update)
    notice = None
    if args:
        if len(args) != 1 or args[0] not in {"on", "off"}:
            await _send(runtime, update, ui_language.tr("menu.agentpresence.usage"))
            return
        enabled = args[0] == "on"
        set_connector_preference(
            bridge_home,
            owner_id,
            CONNECTOR_ID,
            "mirror",
            enabled,
        )
        notice = ui_language.tr(f"menu.agentpresence.notice.{args[0]}")
    else:
        enabled = get_connector_preference(
            bridge_home,
            owner_id,
            CONNECTOR_ID,
            "mirror",
            default=False,
        )
    await _send(
        runtime,
        update,
        runtime_menu_views.agentpresence_menu_text(
            enabled=enabled,
            notice=notice,
        ),
    )


COMMANDS = [
    RuntimeCommand(
        name="agentpresence",
        description="Control AgentPresence call and voice output [on|off]",
        callback=agentpresence_command,
    )
]


__all__ = ["COMMANDS", "CONNECTOR_ID", "agentpresence_command"]
