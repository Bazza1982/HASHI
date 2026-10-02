"""Authoritative, connector-neutral catalog of HASHI frontend boundaries."""
from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any

REGISTRY_TYPE = "hashi.frontend-connector-registry"
REGISTRY_VERSION = 3

# These are FC semantic operations, not claims about a transport's native
# widgets.  A Connector may render a standard display or button as text when
# its transport has no richer primitive, but it may not change the operation's
# meaning or bypass admission.
STANDARD_INGRESS_OPERATIONS = frozenset(
    {"message", "command", "action", "control", "ack"}
)
STANDARD_EGRESS_SEMANTICS = frozenset(
    {
        "message",
        "display",
        "button",
        "media",
        "state",
        "notification",
        "receipt",
    }
)

_TUI_COMMAND_CUSTOMIZATIONS: tuple[dict[str, str], ...] = (
    {
        "kind": "command_override",
        "key": "agents",
        "route": "connector_local",
        "semantic": "agent_directory",
        "reason": "local_navigation",
    },
    {
        "kind": "command_override",
        "key": "to",
        "route": "connector_local",
        "semantic": "target_selection",
        "reason": "local_navigation",
    },
    {
        "kind": "command_override",
        "key": "instance",
        "route": "connector_local",
        "semantic": "instance_selection",
        "reason": "local_navigation",
    },
    {
        "kind": "command_override",
        "key": "attach",
        "route": "connector_local",
        "semantic": "attachment_staging",
        "reason": "local_input",
    },
    {
        "kind": "command_override",
        "key": "say",
        "route": "connector_local",
        "semantic": "local_speech_playback",
        "reason": "local_presentation",
    },
    {
        "kind": "command_override",
        "key": "voice",
        "route": "connector_local",
        "semantic": "local_voice_controls",
        "reason": "local_presentation",
    },
    {
        "kind": "command_override",
        "key": "sidepanel",
        "route": "connector_local",
        "semantic": "local_information_panel",
        "reason": "local_presentation",
    },
    {
        "kind": "command_override",
        "key": "tui",
        "route": "connector_local",
        "semantic": "local_connector_preferences",
        "reason": "local_presentation",
    },
    {
        "kind": "command_override",
        "key": "help",
        "route": "connector_local",
        "semantic": "local_command_discovery",
        "reason": "local_navigation",
    },
    {
        "kind": "command_override",
        "key": "connect",
        "route": "connector_local",
        "semantic": "local_connection_setup",
        "reason": "local_control",
    },
    {
        "kind": "command_override",
        "key": "theme",
        "route": "connector_local",
        "semantic": "local_theme",
        "reason": "local_presentation",
    },
    {
        "kind": "command_override",
        "key": "layout",
        "route": "connector_local",
        "semantic": "local_layout",
        "reason": "local_presentation",
    },
    {
        "kind": "command_override",
        "key": "clear",
        "route": "connector_local",
        "semantic": "local_view_clear",
        "reason": "local_presentation",
    },
    {
        "kind": "command_override",
        "key": "quit",
        "route": "connector_local",
        "semantic": "local_client_exit",
        "reason": "local_control",
    },
    {
        "kind": "command_override",
        "key": "log",
        "route": "connector_local",
        "semantic": "local_log_view",
        "reason": "local_presentation",
    },
)

_TELEGRAM_PRESENTATION_CUSTOMIZATIONS: tuple[dict[str, str], ...] = (
    {
        "kind": "presentation_override",
        "key": "reply_prompt",
        "route": "connector_local",
        "semantic": "reply_prompt",
        "reason": "local_interaction",
    },
    {
        "kind": "presentation_override",
        "key": "reply_keyboard",
        "route": "connector_local",
        "semantic": "reply_keyboard",
        "reason": "local_interaction",
    },
    {
        "kind": "presentation_override",
        "key": "voice_rendition",
        "route": "connector_local",
        "semantic": "message_voice_rendition",
        "reason": "local_presentation",
    },
    {
        "kind": "presentation_override",
        "key": "voice_profile_preview",
        "route": "connector_local",
        "semantic": "voice_profile_preview",
        "reason": "local_presentation",
    },
    {
        "kind": "presentation_override",
        "key": "ephemeral_progress",
        "route": "connector_local",
        "semantic": "progress_state",
        "reason": "local_presentation",
    },
    {
        "kind": "presentation_override",
        "key": "delivery_failover_notice",
        "route": "connector_local",
        "semantic": "delivery_health_notice",
        "reason": "operational_fallback",
    },
    {
        "kind": "presentation_override",
        "key": "callback_interaction_rendering",
        "route": "connector_local",
        "semantic": "command_interaction_update",
        "reason": "local_interaction",
    },
)

_WHATSAPP_COMMAND_CUSTOMIZATIONS: tuple[dict[str, str], ...] = (
    {
        "kind": "command_override",
        "key": "agent",
        "route": "connector_local",
        "semantic": "target_selection",
        "reason": "local_navigation",
    },
    {
        "kind": "command_override",
        "key": "all",
        "route": "connector_local",
        "semantic": "target_broadcast",
        "reason": "local_navigation",
    },
    {
        "kind": "command_override",
        "key": "restart",
        "route": "connector_local",
        "semantic": "watchtower_restart",
        "reason": "local_control",
    },
    {
        "kind": "command_override",
        "key": "terminate",
        "route": "connector_local",
        "semantic": "named_agent_termination",
        "reason": "local_control",
    },
    {
        "kind": "command_override",
        "key": "start",
        "route": "connector_local",
        "semantic": "named_agent_start",
        "reason": "local_control",
    },
    {
        "kind": "command_override",
        "key": "stop",
        "route": "connector_local",
        "semantic": "named_agent_stop",
        "reason": "local_control",
    },
)

# Existing public routes remain wire-compatible, but their implementation is
# explicitly registered here as a thin conversion into the standard FC
# operation.  This is the allow-list for compatibility; adding an HTTP route,
# transport callback, or local override does not grant an untracked bypass.
_COMPATIBILITY_ADAPTERS: tuple[dict[str, str], ...] = (
    {
        "id": "telegram.message",
        "connector_id": "telegram",
        "direction": "ingress",
        "operation": "message",
        "route": "standard_fc",
    },
    {
        "id": "telegram.native_command",
        "connector_id": "telegram",
        "direction": "ingress",
        "operation": "command",
        "route": "standard_fc",
    },
    {
        "id": "telegram.native_callback",
        "connector_id": "telegram",
        "direction": "ingress",
        "operation": "action",
        "route": "standard_fc",
    },
    {
        "id": "telegram.explicit_notification",
        "connector_id": "telegram",
        "direction": "egress",
        "operation": "notification",
        "route": "standard_fc",
    },
    {
        "id": "telegram.explicit_media",
        "connector_id": "telegram",
        "direction": "egress",
        "operation": "media",
        "route": "standard_fc",
    },
    {
        "id": "telegram.inline_keyboard",
        "connector_id": "telegram",
        "direction": "egress",
        "operation": "button",
        "route": "standard_fc",
    },
    {
        "id": "backend_api.chat",
        "connector_id": "backend_api",
        "direction": "ingress",
        "operation": "message",
        "route": "standard_fc",
    },
    {
        "id": "backend_api.chat_command",
        "connector_id": "backend_api",
        "direction": "ingress",
        "operation": "command",
        "route": "standard_fc",
    },
    {
        "id": "backend_api.agent_command",
        "connector_id": "backend_api",
        "direction": "ingress",
        "operation": "command",
        "route": "standard_fc",
    },
    {
        "id": "backend_api.admin_command",
        "connector_id": "backend_api",
        "direction": "ingress",
        "operation": "command",
        "route": "standard_fc",
    },
    {
        "id": "session_api.run",
        "connector_id": "session_api",
        "direction": "ingress",
        "operation": "message",
        "route": "standard_fc",
    },
    {
        "id": "session_api.command",
        "connector_id": "session_api",
        "direction": "ingress",
        "operation": "command",
        "route": "standard_fc",
    },
    {
        "id": "session_api.control",
        "connector_id": "session_api",
        "direction": "ingress",
        "operation": "control",
        "route": "standard_fc",
    },
    {
        "id": "session_api.action",
        "connector_id": "session_api",
        "direction": "ingress",
        "operation": "action",
        "route": "standard_fc",
    },
    {
        "id": "session_api.ack",
        "connector_id": "session_api",
        "direction": "ingress",
        "operation": "ack",
        "route": "standard_fc",
    },
    {
        "id": "tui.proxy",
        "connector_id": "tui",
        "direction": "ingress",
        "operation": "message",
        "route": "standard_fc",
    },
    {
        "id": "tui.command",
        "connector_id": "tui",
        "direction": "ingress",
        "operation": "command",
        "route": "standard_fc",
    },
    {
        "id": "tui.local_command",
        "connector_id": "tui",
        "direction": "ingress",
        "operation": "command",
        "route": "registered_connector_local",
    },
    {
        "id": "hchat.legacy_relay",
        "connector_id": "hchat",
        "direction": "ingress",
        "operation": "message",
        "route": "standard_fc",
    },
    {
        "id": "remote.protocol",
        "connector_id": "remote",
        "direction": "ingress",
        "operation": "message",
        "route": "standard_fc",
    },
    {
        "id": "exchange.wss",
        "connector_id": "exchange",
        "direction": "ingress",
        "operation": "message",
        "route": "standard_fc",
    },
    {
        "id": "whatsapp.transport",
        "connector_id": "whatsapp",
        "direction": "ingress",
        "operation": "message",
        "route": "standard_fc",
    },
    {
        "id": "whatsapp.native_command",
        "connector_id": "whatsapp",
        "direction": "ingress",
        "operation": "command",
        "route": "standard_fc",
    },
    {
        "id": "whatsapp.local_command",
        "connector_id": "whatsapp",
        "direction": "ingress",
        "operation": "command",
        "route": "registered_connector_local",
    },
    {
        "id": "hchat.command",
        "connector_id": "hchat",
        "direction": "ingress",
        "operation": "command",
        "route": "standard_fc",
    },
    {
        "id": "remote.command",
        "connector_id": "remote",
        "direction": "ingress",
        "operation": "command",
        "route": "standard_fc",
    },
    {
        "id": "exchange.command",
        "connector_id": "exchange",
        "direction": "ingress",
        "operation": "command",
        "route": "standard_fc",
    },
)

_TERMINAL_LOCAL_COMMAND_CUSTOMIZATIONS: tuple[dict[str, str], ...] = (
    {
        "kind": "command_override",
        "key": "logo",
        "route": "connector_local",
        "semantic": "terminal_logo_display",
        "reason": "local_presentation",
    },
)

_CONNECTORS: tuple[dict[str, Any], ...] = (
    {
        "id": "telegram",
        "class": "messaging",
        "ingress": ["message", "command", "callback", "media", "voice"],
        "egress": ["text", "media", "voice", "command_result"],
        "running_media_delivery": "push",
        "canonical_feed": "persistent_session_events",
        "customizations": (
            *_TELEGRAM_PRESENTATION_CUSTOMIZATIONS,
            *_TERMINAL_LOCAL_COMMAND_CUSTOMIZATIONS,
        ),
    },
    {
        "id": "tui",
        "class": "local_ui",
        "ingress": ["message", "command", "media", "voice"],
        "egress": ["text", "media", "voice", "status", "approval"],
        "running_media_delivery": "pull",
        "canonical_feed": "persistent_session_events",
        "customizations": _TUI_COMMAND_CUSTOMIZATIONS,
    },
    {
        "id": "backend_api",
        "class": "api",
        "ingress": ["message", "command", "media"],
        "egress": ["text", "media", "status", "approval"],
        "running_media_delivery": "pull",
        "canonical_feed": "persistent_session_events",
        "customizations": _TERMINAL_LOCAL_COMMAND_CUSTOMIZATIONS,
    },
    {
        "id": "session_api",
        "class": "api",
        "ingress": ["message", "command", "media", "voice"],
        "egress": ["text", "media", "voice", "status", "approval"],
        "running_media_delivery": "pull",
        "canonical_feed": "persistent_session_events",
        "customizations": _TERMINAL_LOCAL_COMMAND_CUSTOMIZATIONS,
    },
    {
        "id": "hchat",
        "class": "agent_messaging",
        "ingress": ["message", "command", "media"],
        "egress": ["text", "media", "receipt"],
        "canonical_feed": "persistent_session_events",
        "customizations": _TERMINAL_LOCAL_COMMAND_CUSTOMIZATIONS,
    },
    {
        "id": "remote",
        "class": "authenticated_relay",
        "ingress": ["message", "command", "media", "receipt"],
        "egress": ["text", "media", "receipt"],
        "canonical_feed": "persistent_session_events",
        "customizations": _TERMINAL_LOCAL_COMMAND_CUSTOMIZATIONS,
    },
    {
        "id": "exchange",
        "class": "authenticated_relay",
        "ingress": ["message", "command", "media", "receipt"],
        "egress": ["text", "media", "receipt"],
        "canonical_feed": "persistent_session_events",
        "customizations": _TERMINAL_LOCAL_COMMAND_CUSTOMIZATIONS,
    },
    {
        "id": "whatsapp",
        "class": "messaging",
        "ingress": ["message", "command", "media"],
        "egress": ["text", "receipt"],
        "canonical_feed": "persistent_session_events",
        "customizations": (
            *_WHATSAPP_COMMAND_CUSTOMIZATIONS,
            *_TERMINAL_LOCAL_COMMAND_CUSTOMIZATIONS,
        ),
    },
    {
        "id": "external",
        "class": "external_client",
        "ingress": ["message", "command", "media"],
        "egress": ["text", "media", "status", "approval"],
        "running_media_delivery": "pull",
        "canonical_feed": "persistent_session_events",
        "customizations": _TERMINAL_LOCAL_COMMAND_CUSTOMIZATIONS,
    },
    {
        "id": "internal",
        "class": "internal_event_source",
        "ingress": ["event", "command", "media"],
        "egress": ["text", "media", "status", "error"],
        "canonical_feed": "persistent_session_events",
    },
    {
        "id": "reference",
        "class": "reference",
        "ingress": ["message", "command", "callback", "media", "voice", "control", "approval", "ack"],
        "egress": ["text", "media", "voice", "command_result", "status", "card", "receipt"],
        "canonical_feed": "persistent_session_events",
        "customizations": _TERMINAL_LOCAL_COMMAND_CUSTOMIZATIONS,
    },
)

_DYNAMIC_CONNECTORS: dict[str, dict[str, Any]] = {}
_CONNECTOR_ID = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_CAPABILITY_ID = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_BUILTIN_CONNECTOR_IDS = frozenset(item["id"] for item in _CONNECTORS)
_HEALTH_STATES = frozenset({"unobserved", "online", "degraded", "offline", "draining"})

_INTERNAL_SOURCE_IDS = {
    "scheduler",
    "scheduler-retry",
    "scheduler-skill",
    "heartbeat",
    "cron",
    "proactive",
    "background-job-event",
    "background_job_event",
    "startup",
    "system",
    "session_reset",
    "handoff",
}


def canonical_connector_id(
    source_id: str,
    *,
    ingress_transport: str = "",
    surface: str = "",
) -> str:
    """Map registered transport labels to one stable connector identity.

    Transport and surface win over a user-declared message-source label.  An
    unknown label fails closed: callers must register a Connector instead of
    being silently admitted through a generic bucket.
    """

    source = str(source_id or "").strip().casefold()
    transport = str(ingress_transport or "").strip().casefold()
    normalized_surface = str(surface or "").strip().casefold()

    def _matches(value: str, *prefixes: str) -> bool:
        return any(value == prefix or value.startswith(prefix + ".") for prefix in prefixes)

    # The actual connection surface is authoritative when a Backend API client
    # supplies a custom public message_source identifier.
    if source in {"session-api", "session_api"}:
        return "session_api"
    if _matches(normalized_surface, "telegram") or _matches(transport, "telegram"):
        return "telegram"
    if _matches(normalized_surface, "whatsapp") or _matches(transport, "whatsapp"):
        return "whatsapp"
    if _matches(normalized_surface, "tui") or _matches(transport, "tui"):
        return "tui"
    if normalized_surface in {"session-api", "session_api"} or "session-api" in transport:
        return "session_api"
    if normalized_surface in {"workbench", "backend-api", "backend_api", "api"}:
        return "backend_api"
    if _matches(normalized_surface, "exchange") or "exchange" in transport:
        return "exchange"
    if _matches(normalized_surface, "remote") or "remote" in transport:
        return "remote"
    if _matches(normalized_surface, "hchat") or _matches(transport, "hchat"):
        return "hchat"
    if normalized_surface in _DYNAMIC_CONNECTORS:
        return normalized_surface

    if source.startswith(("protocol:message", "protocol:reply", "hchat-reply:")):
        return "hchat"
    if source in _INTERNAL_SOURCE_IDS or source.startswith(("hashi.internal", "scheduler:", "cron:", "heartbeat:", "proactive:", "bridge:", "browser:")):
        return "internal"
    if source == "telegram" or source.startswith("telegram."):
        return "telegram"
    if source == "whatsapp":
        return "whatsapp"
    if source == "tui":
        return "tui"
    if source == "hchat":
        return "hchat"
    if source in {"remote", "remote-api"}:
        return "remote"
    if source in {"exchange", "hchat-exchange"}:
        return "exchange"
    if source in {"api", "api_chat", "workbench", "backend-api"}:
        return "backend_api"
    if source in _DYNAMIC_CONNECTORS:
        return source
    if source == "reference" or normalized_surface == "reference":
        return "reference"
    if source in {"session-api", "session_api"}:
        return "session_api"
    if source == "external" or normalized_surface == "external":
        return "external"
    raise ValueError(
        "unregistered frontend connector: "
        f"source={source or '<empty>'} transport={transport or '<empty>'} "
        f"surface={normalized_surface or '<empty>'}"
    )


def endpoint_id_for(
    connector_id: str,
    *,
    ingress_transport: str,
    channel_key: str,
) -> str:
    """Create an opaque, stable endpoint key without exposing its address."""

    connector = str(connector_id or "").strip().casefold()
    material = "\n".join(
        (connector, str(ingress_transport or "").strip().casefold(), str(channel_key or ""))
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
    return f"ep_{digest}"


def _normalize_capability_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, (list, tuple)) or not value or len(value) > 64:
        raise ValueError(f"connector {field} must be a non-empty list")
    normalized = []
    for item in value:
        capability = str(item or "").strip().casefold()
        if not _CAPABILITY_ID.fullmatch(capability):
            raise ValueError(f"connector {field} contains an invalid capability")
        if capability not in normalized:
            normalized.append(capability)
    return normalized


def _normalize_customizations(value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)) or len(value) > 64:
        raise ValueError("connector customizations must be a list")
    normalized: list[dict[str, str]] = []
    identities: set[tuple[str, str]] = set()
    for raw in value:
        if not isinstance(raw, Mapping):
            raise ValueError("connector customization must be an object")
        kind = str(raw.get("kind") or "").strip().casefold()
        key = str(raw.get("key") or "").strip().casefold().lstrip("/")
        route = str(raw.get("route") or "").strip().casefold()
        semantic = str(raw.get("semantic") or "").strip().casefold()
        reason = str(raw.get("reason") or "").strip().casefold()
        if kind not in {"command_override", "presentation_override"}:
            raise ValueError("connector customization kind is unsupported")
        if not _CAPABILITY_ID.fullmatch(key):
            raise ValueError("connector customization key is invalid")
        if route not in {"connector_local", "standard"}:
            raise ValueError("connector customization route is invalid")
        if not _CAPABILITY_ID.fullmatch(semantic):
            raise ValueError("connector customization semantic is invalid")
        if not _CAPABILITY_ID.fullmatch(reason):
            raise ValueError("connector customization reason is invalid")
        identity = (kind, key)
        if identity in identities:
            raise ValueError("connector customization is duplicated")
        identities.add(identity)
        normalized.append(
            {
                "kind": kind,
                "key": key,
                "route": route,
                "semantic": semantic,
                "reason": reason,
            }
        )
    return normalized


def _descriptor(connector_id: str) -> Mapping[str, Any]:
    cid = str(connector_id or "").strip().casefold()
    if cid in _DYNAMIC_CONNECTORS:
        return _DYNAMIC_CONNECTORS[cid]
    for item in _CONNECTORS:
        if item["id"] == cid:
            return item
    raise ValueError(f"unknown connector: {cid}")


def _effective_customizations(descriptor: Mapping[str, Any]) -> list[dict[str, str]]:
    """Project mandatory terminal restrictions for every non-TUI Connector."""

    rules = {
        (item["kind"], item["key"]): item
        for item in _normalize_customizations(descriptor.get("customizations"))
    }
    if descriptor["id"] != "tui":
        # Third-party registration cannot opt back into server-terminal effects.
        for item in _TERMINAL_LOCAL_COMMAND_CUSTOMIZATIONS:
            rules[(item["kind"], item["key"])] = dict(item)
    return list(rules.values())


def get_connector_customization(
    connector_id: str,
    *,
    kind: str,
    key: str,
) -> dict[str, str] | None:
    """Return one registered semantic exception, never an inferred bypass."""

    wanted_kind = str(kind or "").strip().casefold()
    wanted_key = str(key or "").strip().casefold().lstrip("/")
    descriptor = _descriptor(connector_id)
    for item in _effective_customizations(descriptor):
        if item["kind"] == wanted_kind and item["key"] == wanted_key:
            return dict(item)
    return None


def require_connector_presentation_override(
    connector_id: str,
    key: str,
) -> dict[str, str]:
    """Require an explicit FC registration for connector-local rendering."""

    customization = get_connector_customization(
        connector_id,
        kind="presentation_override",
        key=key,
    )
    if customization is None or customization.get("route") != "connector_local":
        raise ValueError(
            "unregistered Connector-local presentation override: "
            f"{connector_id}/{str(key or '').strip() or '<empty>'}"
        )
    return customization


def require_connector_operation(
    connector_id: str,
    direction: str,
    operation: str,
) -> str:
    """Validate a standard FC operation for a registered Connector."""

    _descriptor(connector_id)
    normalized_direction = str(direction or "").strip().casefold()
    normalized_operation = str(operation or "").strip().casefold()
    allowed = (
        STANDARD_INGRESS_OPERATIONS
        if normalized_direction == "ingress"
        else STANDARD_EGRESS_SEMANTICS
        if normalized_direction == "egress"
        else None
    )
    if allowed is None:
        raise ValueError("connector operation direction is invalid")
    if normalized_operation not in allowed:
        raise ValueError(
            f"unsupported standard connector operation: {normalized_operation}"
        )
    return normalized_operation


def get_compatibility_adapter(adapter_id: str) -> dict[str, str]:
    """Return one explicitly registered legacy-to-FC conversion boundary."""

    wanted = str(adapter_id or "").strip().casefold()
    for item in _COMPATIBILITY_ADAPTERS:
        if item["id"] != wanted:
            continue
        require_connector_operation(
            item["connector_id"], item["direction"], item["operation"]
        )
        return dict(item)
    raise ValueError(f"unregistered compatibility adapter: {wanted or '<empty>'}")


def require_connector_event(
    connector_id: str,
    event: Mapping[str, Any],
) -> set[str]:
    """Validate every standard output primitive before an adapter renders it."""

    if not isinstance(event, Mapping):
        raise ValueError("frontend event must be an object")
    if str(event.get("type") or "") != "hashi.frontend-event":
        raise ValueError("connector adapter requires a standard frontend event")
    interface_kind = str(event.get("interface_kind") or "").strip().casefold()
    required = {require_connector_operation(connector_id, "egress", interface_kind)}
    blocks = event.get("content_blocks")
    if not isinstance(blocks, (list, tuple)):
        raise ValueError("frontend event content_blocks must be a list")
    for block in blocks:
        if not isinstance(block, Mapping):
            raise ValueError("frontend event content block must be an object")
        block_type = str(block.get("type") or "").strip().casefold()
        if block_type == "action":
            required.add(
                require_connector_operation(connector_id, "egress", "button")
            )
        elif block_type == "media_ref":
            required.add(
                require_connector_operation(connector_id, "egress", "media")
            )
    return required


def _normalize_runtime_facts(value: Any) -> dict[str, Any]:
    if value is None:
        return {
            "endpoint_id": None,
            "ready": False,
            "health": "unobserved",
            "generation": None,
        }
    if not isinstance(value, Mapping):
        raise ValueError("connector runtime facts must be an object")
    ready = value.get("ready", False)
    if not isinstance(ready, bool):
        raise ValueError("connector runtime ready must be boolean")
    health = str(value.get("health") or "unobserved").strip().casefold()
    if health not in _HEALTH_STATES:
        raise ValueError("connector runtime health is invalid")
    generation_raw = value.get("generation")
    generation = None
    if generation_raw is not None:
        generation = str(generation_raw).strip()
        if (
            not generation
            or len(generation) > 128
            or any(ord(char) < 32 for char in generation)
        ):
            raise ValueError("connector runtime generation is invalid")
    if ready and health not in {"online", "degraded"}:
        raise ValueError("ready connector runtime must be online or degraded")
    endpoint_raw = value.get("endpoint_id")
    endpoint_id = None
    if endpoint_raw is not None:
        endpoint_id = str(endpoint_raw).strip()
        if (
            not endpoint_id
            or len(endpoint_id) > 256
            or any(ord(char) < 32 for char in endpoint_id)
        ):
            raise ValueError("connector runtime endpoint_id is invalid")
    if ready and endpoint_id is None:
        raise ValueError("ready connector runtime requires an endpoint_id")
    return {
        "endpoint_id": endpoint_id,
        "ready": ready,
        "health": health,
        "generation": generation,
    }


def register_connector(connector: dict[str, Any]) -> None:
    """Register a validated third-party descriptor without claiming runtime readiness."""
    if not isinstance(connector, Mapping):
        raise ValueError("connector descriptor must be an object")
    connector_id = str(connector.get("id") or "").strip().casefold()
    if not _CONNECTOR_ID.fullmatch(connector_id):
        raise ValueError("connector id is invalid")
    if connector_id in _BUILTIN_CONNECTOR_IDS:
        raise ValueError("built-in connector ids cannot be overridden")
    connector_class = str(connector.get("class") or "").strip().casefold()
    if not _CAPABILITY_ID.fullmatch(connector_class):
        raise ValueError("connector class is invalid")
    canonical_feed = str(
        connector.get("canonical_feed") or "persistent_session_events"
    ).strip().casefold()
    if canonical_feed != "persistent_session_events":
        raise ValueError("connector canonical_feed is unsupported")
    _DYNAMIC_CONNECTORS[connector_id] = {
        "id": connector_id,
        "class": connector_class,
        "ingress": _normalize_capability_list(connector.get("ingress"), "ingress"),
        "egress": _normalize_capability_list(connector.get("egress"), "egress"),
        "canonical_feed": canonical_feed,
        "customizations": _normalize_customizations(
            connector.get("customizations")
        ),
        "runtime": _normalize_runtime_facts(connector.get("runtime")),
    }


def unregister_connector(connector_id: str) -> bool:
    """Unregister a dynamic connector."""
    return _DYNAMIC_CONNECTORS.pop(str(connector_id or "").strip().casefold(), None) is not None


def get_connector_capabilities(
    connector_id: str,
    endpoint_id: str | None = None,
) -> dict[str, Any]:
    """Retrieve the declared and verified capabilities for a connector endpoint."""
    cid = str(connector_id or "").strip().casefold()
    descriptor = _descriptor(cid)
    runtime = _normalize_runtime_facts(descriptor.get("runtime"))
    requested_endpoint = str(endpoint_id or "").strip() or runtime["endpoint_id"]
    endpoint_registered = bool(
        runtime["endpoint_id"]
        and requested_endpoint == runtime["endpoint_id"]
    )
    return {
        "connector_id": cid,
        "endpoint_id": requested_endpoint,
        "endpoint_registered": endpoint_registered,
        "protocol_version": 3,
        "ingress": list(descriptor.get("ingress", [])),
        "egress": list(descriptor.get("egress", [])),
        "standard_ingress": sorted(STANDARD_INGRESS_OPERATIONS),
        "standard_egress": sorted(STANDARD_EGRESS_SEMANTICS),
        "customizations": _effective_customizations(descriptor),
        "canonical_feed": descriptor.get("canonical_feed", "persistent_session_events"),
        "ready": bool(endpoint_registered and runtime["ready"]),
        "health": runtime["health"] if endpoint_registered else "unobserved",
        "generation": runtime["generation"],
    }


def supports_running_media_delivery(connector_id: str) -> bool:
    """Whether this Connector has a sender or feed consumer during a Run."""
    descriptor = _descriptor(str(connector_id or "").strip().casefold())
    return (
        "media" in descriptor.get("egress", ())
        and descriptor.get("running_media_delivery") in {"push", "pull"}
    )


def connector_registry_snapshot() -> dict[str, Any]:
    """Return a read-only, client-neutral capability snapshot."""

    all_connectors = [
        {
            **item,
            "ingress": list(item["ingress"]),
            "egress": list(item["egress"]),
            "customizations": _effective_customizations(item),
            "runtime": _normalize_runtime_facts(item.get("runtime")),
        }
        for item in _CONNECTORS
    ]
    for dyn in _DYNAMIC_CONNECTORS.values():
        all_connectors.append(
            {
                **dyn,
                "ingress": list(dyn.get("ingress", [])),
                "egress": list(dyn.get("egress", [])),
                "customizations": _effective_customizations(dyn),
            }
        )
    return {
        "type": REGISTRY_TYPE,
        "version": REGISTRY_VERSION,
        "standard_interface": {
            "ingress": sorted(STANDARD_INGRESS_OPERATIONS),
            "egress": sorted(STANDARD_EGRESS_SEMANTICS),
        },
        "compatibility_adapters": [dict(item) for item in _COMPATIBILITY_ADAPTERS],
        "connectors": all_connectors,
    }


class ReferenceConnectorAdapter:
    """Reference connector adapter implementing the standard FC contract."""

    def __init__(
        self,
        connector_id: str = "reference",
        endpoint_id: str | None = None,
    ):
        self.connector_id = connector_id
        self.endpoint_id = endpoint_id or f"{connector_id}:primary"
        self.received_events: list[dict[str, Any]] = []
        self.simulate_mode: str = "delivered"  # "delivered" | "timeout" | "rejected" | "disconnected"

    def render(self, event: Any, target_format: str = "text") -> str:
        blocks = event.get("content_blocks") if isinstance(event, dict) else []
        lines = []
        for block in blocks or []:
            btype = block.get("type")
            if btype == "text":
                lines.append(str(block.get("text") or ""))
            elif btype in {"key_value", "kv"}:
                for item in block.get("items", []):
                    lines.append(f"{item.get('key')}: {item.get('value')}")
            elif btype == "table":
                headers = block.get("headers", [])
                if headers:
                    lines.append(" | ".join(headers))
                for row in block.get("rows", []):
                    lines.append(" | ".join(row))
            elif btype == "action":
                lines.append(f"[{block.get('label')}] ({block.get('action_id')})")
            elif btype == "hint":
                lines.append(f"({block.get('level')}): {block.get('text')}")
            elif btype == "media_ref":
                lines.append(f"[Media: {block.get('attachment_id')}]")
        return "\n".join(lines)

    async def dispatch(
        self,
        event: Any,
        endpoint_id: str | None = None,
    ) -> dict[str, Any]:
        ep_id = endpoint_id or self.endpoint_id
        if isinstance(event, dict):
            self.received_events.append(dict(event))
            event_id = str(event.get("event_id") or "")
        else:
            event_id = "unknown"

        if self.simulate_mode == "delivered":
            return {
                "type": "hashi.delivery-receipt",
                "version": 1,
                "event_id": event_id,
                "endpoint_id": ep_id,
                "status": "delivered",
                "proof": {
                    "type": "reference_ack",
                    "value": f"ack_{event_id}_{ep_id}",
                },
            }
        elif self.simulate_mode == "timeout":
            return {
                "type": "hashi.delivery-receipt",
                "version": 1,
                "event_id": event_id,
                "endpoint_id": ep_id,
                "status": "unknown",
                "proof": None,
            }
        elif self.simulate_mode == "rejected":
            return {
                "type": "hashi.delivery-receipt",
                "version": 1,
                "event_id": event_id,
                "endpoint_id": ep_id,
                "status": "failed",
                "proof": None,
            }
        elif self.simulate_mode == "disconnected":
            raise ConnectionError(f"Endpoint {ep_id} disconnected")
        else:
            return {
                "type": "hashi.delivery-receipt",
                "version": 1,
                "event_id": event_id,
                "endpoint_id": ep_id,
                "status": "unknown",
                "proof": None,
            }


__all__ = [
    "REGISTRY_TYPE",
    "REGISTRY_VERSION",
    "ReferenceConnectorAdapter",
    "STANDARD_EGRESS_SEMANTICS",
    "STANDARD_INGRESS_OPERATIONS",
    "canonical_connector_id",
    "connector_registry_snapshot",
    "endpoint_id_for",
    "get_compatibility_adapter",
    "get_connector_capabilities",
    "get_connector_customization",
    "register_connector",
    "require_connector_event",
    "require_connector_operation",
    "unregister_connector",
]
