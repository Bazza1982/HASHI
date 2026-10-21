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
from orchestrator.agent_presence_notify import get_hashi_binding, set_hashi_binding
from orchestrator.command_registry import RuntimeCommand
from orchestrator.config_json import read_config_json
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


def _workbench_base_url(runtime: Any) -> str:
    port = int(
        getattr(getattr(runtime, "global_config", None), "workbench_port", 0) or 0
    )
    if port < 1 or port > 65535:
        raise ValueError("workbench port is unavailable")
    return f"http://127.0.0.1:{port}"


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


def _parse_target(runtime: Any, bridge_home: Any, value: str) -> tuple[str, str]:
    raw = str(value or "").strip()
    if raw.count("@") != 1:
        raise ValueError("target")
    agent_id, instance_id = (part.strip() for part in raw.split("@", 1))
    expected_instance = _instance_id(runtime)
    if not agent_id or instance_id.upper() != expected_instance:
        raise ValueError("instance")
    normalized_agent = agent_id.casefold()
    if normalized_agent not in _configured_agents(bridge_home):
        raise ValueError("agent")
    return normalized_agent, expected_instance


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
    notice = None
    binding: dict[str, Any] | None = None
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
                binding = await asyncio.to_thread(set_hashi_binding)
                notice_key = "menu.agentpresence.notice.default_cleared"
            else:
                try:
                    agent_id, instance_id = _parse_target(runtime, bridge_home, args[1])
                    base_url = _workbench_base_url(runtime)
                except (OSError, TypeError, ValueError):
                    await _send(
                        runtime,
                        update,
                        ui_language.tr("menu.agentpresence.invalid_target"),
                    )
                    return
                binding = await asyncio.to_thread(
                    set_hashi_binding,
                    instance_id=instance_id,
                    base_url=base_url,
                    default_agent=agent_id,
                )
                notice_key = "menu.agentpresence.notice.default_set"
            if binding is None:
                await _send(
                    runtime,
                    update,
                    ui_language.tr("menu.agentpresence.api_unavailable"),
                )
                return
            notice = ui_language.tr(
                notice_key,
                target=str(binding.get("target") or ""),
            )
            enabled = get_connector_preference(
                bridge_home,
                owner_id,
                CONNECTOR_ID,
                "mirror",
                default=False,
            )
        else:
            await _send(runtime, update, ui_language.tr("menu.agentpresence.usage"))
            return
    else:
        enabled = get_connector_preference(
            bridge_home,
            owner_id,
            CONNECTOR_ID,
            "mirror",
            default=False,
        )
    if binding is None:
        binding = await asyncio.to_thread(get_hashi_binding)
    await _send(
        runtime,
        update,
        runtime_menu_views.agentpresence_menu_text(
            enabled=enabled,
            binding_target=(
                str(binding.get("target") or "") if binding is not None else ""
            ),
            binding_available=binding is not None,
            notice=notice,
        ),
    )


COMMANDS = [
    RuntimeCommand(
        name="agentpresence",
        description="Control AgentPresence output and default HASHI Agent",
        callback=agentpresence_command,
    )
]


__all__ = ["COMMANDS", "CONNECTOR_ID", "agentpresence_command"]
