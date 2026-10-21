"""Owner-scoped AgentPresence Connector controls.

This public RuntimeCommand is discovered by the shared command registry, so it
is available through every command-capable Frontend Connector without a
transport-specific registration or Backend/API special case.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from orchestrator import runtime_menu_views, runtime_session, ui_language
from orchestrator.agent_presence_notify import get_hashi_instance_binding
from orchestrator.agent_presence_settings import (
    get_default_agent,
    set_default_agent,
)
from orchestrator.command_registry import RuntimeCommand
from orchestrator.config_json import ConfigConflictError, read_config_json
from orchestrator.connector_delivery_preferences import (
    get_connector_preference,
    set_connector_preference,
)


CONNECTOR_ID = "agentpresence"


def _instance_id(runtime: Any) -> str:
    return str(
        getattr(getattr(runtime, "global_config", None), "instance_id", None)
        or "HASHI"
    ).strip().upper()


def _configured_agents(bridge_home: Any) -> set[str]:
    document = read_config_json(Path(bridge_home) / "agents.json")
    agents = document.get("agents")
    if not isinstance(agents, list):
        return set()
    return {
        str(item.get("name") or "").strip().casefold()
        for item in agents
        if isinstance(item, dict)
        and item.get("is_active") is not False
        and str(item.get("name") or "").strip()
    }


def _parse_agent(bridge_home: Any, value: str) -> str:
    raw = str(value or "").strip()
    normalized_agent = raw.casefold()
    if normalized_agent not in _configured_agents(bridge_home):
        raise ValueError("agent")
    return normalized_agent


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
        str(arg).strip()
        for arg in (getattr(context, "args", None) or ())
        if str(arg).strip()
    ]
    owner_id = _owner_id(runtime, update)
    enabled = get_connector_preference(
        bridge_home,
        owner_id,
        CONNECTOR_ID,
        "mirror",
        default=False,
    )
    notice = None
    if args:
        action = args[0].casefold()
        if len(args) == 1 and action in {"on", "off"}:
            enabled = action == "on"
            set_connector_preference(
                bridge_home,
                owner_id,
                CONNECTOR_ID,
                "mirror",
                enabled,
            )
            notice = ui_language.tr(f"menu.agentpresence.notice.{action}")
        elif len(args) == 2 and action == "default":
            if args[1].casefold() in {"off", "none", "clear"}:
                try:
                    await asyncio.to_thread(set_default_agent, bridge_home)
                except (OSError, TypeError, ValueError, ConfigConflictError):
                    await _send(
                        runtime,
                        update,
                        ui_language.tr("menu.agentpresence.config_unavailable"),
                    )
                    return
                notice = ui_language.tr(
                    "menu.agentpresence.notice.default_cleared"
                )
            else:
                try:
                    agent_id = _parse_agent(bridge_home, args[1])
                except (OSError, TypeError, ValueError):
                    await _send(
                        runtime,
                        update,
                        ui_language.tr("menu.agentpresence.invalid_target"),
                    )
                    return
                try:
                    await asyncio.to_thread(
                        set_default_agent,
                        bridge_home,
                        agent_id,
                    )
                except (OSError, TypeError, ValueError, ConfigConflictError):
                    await _send(
                        runtime,
                        update,
                        ui_language.tr("menu.agentpresence.config_unavailable"),
                    )
                    return
                notice = ui_language.tr(
                    "menu.agentpresence.notice.default_set",
                    agent=agent_id,
                    instance=_instance_id(runtime),
                )
        else:
            await _send(runtime, update, ui_language.tr("menu.agentpresence.usage"))
            return

    try:
        default_agent = await asyncio.to_thread(get_default_agent, bridge_home)
        settings_available = True
    except (OSError, TypeError, ValueError):
        default_agent = ""
        settings_available = False
    binding = await asyncio.to_thread(get_hashi_instance_binding)
    await _send(
        runtime,
        update,
        runtime_menu_views.agentpresence_menu_text(
            enabled=enabled,
            ap_instance_id=(
                str(binding.get("instance_id") or "")
                if binding is not None
                else ""
            ),
            binding_available=binding is not None,
            default_agent=default_agent,
            settings_available=settings_available,
            notice=notice,
        ),
    )


COMMANDS = [
    RuntimeCommand(
        name="agentpresence",
        description="Control AgentPresence output and default Agent",
        callback=agentpresence_command,
    )
]


__all__ = ["COMMANDS", "CONNECTOR_ID", "agentpresence_command"]
