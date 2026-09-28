"""Registered compatibility adapters into the standard Frontend Connector API.

Legacy URLs and transport callbacks may keep their wire shape, but they must
declare one adapter here before a command is allowed to reach runtime command
execution.  Connector-local commands are explicit FC customizations and must
be consumed by that Connector, never executed as HASHI runtime commands.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from orchestrator.frontend_connector_registry import (
    canonical_connector_id,
    get_compatibility_adapter,
    get_connector_customization,
    require_connector_operation,
)
from orchestrator.frontend_contracts import normalize_frontend_operation
from orchestrator.slash_command_audit import split_slash_command_words


class ConnectorLocalCommand(ValueError):
    """Raised when a registered local command leaks past its Connector."""


def normalize_compatibility_operation(
    adapter_id: str,
    operation: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate one legacy route and its transport-neutral FC operation."""

    adapter = get_compatibility_adapter(adapter_id)
    normalized = normalize_frontend_operation(operation)
    if normalized["kind"] != adapter["operation"]:
        raise ValueError(
            "compatibility adapter operation mismatch: "
            f"{adapter['operation']} != {normalized['kind']}"
        )
    require_connector_operation(
        adapter["connector_id"],
        adapter["direction"],
        normalized["kind"],
    )
    return {
        "adapter": adapter,
        "connector_id": adapter["connector_id"],
        "operation": normalized,
    }


def _command_parts(command_line: str) -> tuple[str, list[str]]:
    raw = str(command_line or "").strip()
    if raw.startswith("/"):
        raw = raw[1:]
    parts = split_slash_command_words(raw)
    if not parts:
        raise ValueError("empty command")
    return parts[0].split("@", 1)[0].casefold(), list(parts[1:])


def _connector_id(
    source_channel: str,
    session_metadata: Mapping[str, Any] | None,
) -> str:
    metadata = dict(session_metadata or {})
    declared = str(metadata.get("connector_id") or "").strip().casefold()
    surface = str(metadata.get("session_surface") or "").strip().casefold()
    source = str(source_channel or "").strip().casefold()
    if declared:
        # Passing the value through the registry rejects invented IDs.
        return canonical_connector_id(
            declared,
            ingress_transport=declared,
            surface=declared,
        )
    if source.startswith("whatsapp"):
        surface = surface or "whatsapp"
    elif source.startswith("telegram"):
        surface = surface or "telegram"
    elif source.startswith("tui"):
        surface = surface or "tui"
    elif source.startswith("session") or source == "workbench_command_ui":
        surface = surface or "session-api"
    else:
        surface = surface or "backend-api"
    return canonical_connector_id(
        source,
        ingress_transport=source,
        surface=surface,
    )


def _adapter_id(
    connector_id: str,
    source_channel: str,
    session_metadata: Mapping[str, Any] | None,
) -> str:
    metadata = dict(session_metadata or {})
    explicit = str(
        metadata.get("fc_compatibility_adapter_id") or ""
    ).strip().casefold()
    if explicit:
        return explicit
    source = str(source_channel or "").strip().casefold()
    if connector_id == "backend_api":
        if source == "api_chat":
            return "backend_api.chat_command"
        return "backend_api.agent_command"
    return {
        "session_api": "session_api.command",
        "tui": "tui.command",
        "telegram": "telegram.native_command",
        "whatsapp": "whatsapp.native_command",
        "hchat": "hchat.command",
        "remote": "remote.command",
        "exchange": "exchange.command",
    }[connector_id]


def normalize_compatibility_command(
    command_line: str,
    *,
    source_channel: str,
    session_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Convert one legacy command call into a registered standard operation."""

    command_name, arguments = _command_parts(command_line)
    connector_id = _connector_id(source_channel, session_metadata)
    customization = get_connector_customization(
        connector_id,
        kind="command_override",
        key=command_name,
    )
    if customization and customization.get("route") == "connector_local":
        raise ConnectorLocalCommand(
            f"registered Connector-local command /{command_name} "
            f"must be handled by {connector_id}"
        )
    normalized = normalize_compatibility_operation(
        _adapter_id(connector_id, source_channel, session_metadata),
        {
            "kind": "command",
            "name": command_name,
            "arguments": arguments,
        },
    )
    adapter = normalized["adapter"]
    if adapter["connector_id"] != connector_id:
        raise ValueError("compatibility adapter connector mismatch")
    return {
        "adapter": adapter,
        "connector_id": connector_id,
        "operation": normalized["operation"],
    }


def require_connector_local_command(
    connector_id: str,
    command_name: str,
) -> dict[str, str]:
    """Require one explicit FC registration before a Connector handles locally."""

    normalized_connector = str(connector_id or "").strip().casefold()
    normalized_command = str(command_name or "").strip().casefold().lstrip("/")
    adapter = get_compatibility_adapter(
        f"{normalized_connector}.local_command"
    )
    if (
        adapter["connector_id"] != normalized_connector
        or adapter["operation"] != "command"
        or adapter["route"] != "registered_connector_local"
    ):
        raise ValueError("invalid Connector-local compatibility adapter")
    customization = get_connector_customization(
        normalized_connector,
        kind="command_override",
        key=normalized_command,
    )
    if customization is None or customization.get("route") != "connector_local":
        raise ValueError(
            "unregistered Connector-local command: "
            f"{normalized_connector}/{normalized_command or '<empty>'}"
        )
    return customization


__all__ = [
    "ConnectorLocalCommand",
    "normalize_compatibility_command",
    "normalize_compatibility_operation",
    "require_connector_local_command",
]
