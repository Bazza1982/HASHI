"""Authoritative, connector-neutral catalog of HASHI frontend boundaries."""
from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any

REGISTRY_TYPE = "hashi.frontend-connector-registry"
REGISTRY_VERSION = 2

_CONNECTORS: tuple[dict[str, Any], ...] = (
    {
        "id": "telegram",
        "class": "messaging",
        "ingress": ["message", "command", "callback", "media", "voice"],
        "egress": ["text", "media", "voice", "command_result"],
        "canonical_feed": "persistent_session_events",
    },
    {
        "id": "tui",
        "class": "local_ui",
        "ingress": ["message", "command", "media", "voice"],
        "egress": ["text", "media", "voice", "status", "approval"],
        "canonical_feed": "persistent_session_events",
    },
    {
        "id": "backend_api",
        "class": "api",
        "ingress": ["message", "command", "media"],
        "egress": ["text", "media", "status", "approval"],
        "canonical_feed": "persistent_session_events",
    },
    {
        "id": "session_api",
        "class": "api",
        "ingress": ["message", "command", "media", "voice"],
        "egress": ["text", "media", "voice", "status", "approval"],
        "canonical_feed": "persistent_session_events",
    },
    {
        "id": "hchat",
        "class": "agent_messaging",
        "ingress": ["message", "command", "media"],
        "egress": ["text", "media", "receipt"],
        "canonical_feed": "persistent_session_events",
    },
    {
        "id": "remote",
        "class": "authenticated_relay",
        "ingress": ["message", "command", "media", "receipt"],
        "egress": ["text", "media", "receipt"],
        "canonical_feed": "persistent_session_events",
    },
    {
        "id": "exchange",
        "class": "authenticated_relay",
        "ingress": ["message", "command", "media", "receipt"],
        "egress": ["text", "media", "receipt"],
        "canonical_feed": "persistent_session_events",
    },
    {
        "id": "whatsapp",
        "class": "messaging",
        "ingress": ["message", "command", "media"],
        "egress": ["text", "media", "receipt"],
        "canonical_feed": "persistent_session_events",
    },
    {
        "id": "external",
        "class": "external_client",
        "ingress": ["message", "command", "media"],
        "egress": ["text", "media", "status", "approval"],
        "canonical_feed": "persistent_session_events",
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
}


def canonical_connector_id(
    source_id: str,
    *,
    ingress_transport: str = "",
    surface: str = "",
) -> str:
    """Map legacy transport/source labels to one stable connector identity."""

    source = str(source_id or "").strip().casefold()
    transport = str(ingress_transport or "").strip().casefold()
    normalized_surface = str(surface or "").strip().casefold()
    if source in _INTERNAL_SOURCE_IDS or source.startswith(("hashi.internal", "scheduler:", "cron:", "heartbeat:", "proactive:", "bridge:")):
        return "internal"
    if source == "telegram" or source.startswith("telegram.") or normalized_surface == "telegram":
        return "telegram"
    if source == "whatsapp" or normalized_surface == "whatsapp":
        return "whatsapp"
    if source == "tui" or normalized_surface == "tui":
        return "tui"
    if source == "hchat":
        if "exchange" in transport or normalized_surface == "exchange":
            return "exchange"
        if "remote" in transport or normalized_surface == "remote":
            return "remote"
        return "hchat"
    if source in {"remote", "remote-api"} or normalized_surface == "remote":
        return "remote"
    if source in {"exchange", "hchat-exchange"} or normalized_surface == "exchange":
        return "exchange"
    if source in {"api", "api_chat", "workbench", "backend-api"}:
        if "session-api" in transport or normalized_surface == "session-api":
            return "session_api"
        return "backend_api"
    if source in _DYNAMIC_CONNECTORS:
        return source
    if source == "reference" or normalized_surface == "reference":
        return "reference"
    if source in {"session-api", "session_api"}:
        return "session_api"
    return "external"


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
    descriptor = None
    if cid in _DYNAMIC_CONNECTORS:
        descriptor = _DYNAMIC_CONNECTORS[cid]
    else:
        for item in _CONNECTORS:
            if item["id"] == cid:
                descriptor = item
                break
    if descriptor is None:
        raise ValueError(f"unknown connector: {cid}")
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
        "protocol_version": 2,
        "ingress": list(descriptor.get("ingress", [])),
        "egress": list(descriptor.get("egress", [])),
        "canonical_feed": descriptor.get("canonical_feed", "persistent_session_events"),
        "ready": bool(endpoint_registered and runtime["ready"]),
        "health": runtime["health"] if endpoint_registered else "unobserved",
        "generation": runtime["generation"],
    }


def connector_registry_snapshot() -> dict[str, Any]:
    """Return a read-only, client-neutral capability snapshot."""

    all_connectors = [
        {
            **item,
            "ingress": list(item["ingress"]),
            "egress": list(item["egress"]),
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
            }
        )
    return {
        "type": REGISTRY_TYPE,
        "version": REGISTRY_VERSION,
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
    "canonical_connector_id",
    "connector_registry_snapshot",
    "endpoint_id_for",
    "get_connector_capabilities",
    "register_connector",
    "unregister_connector",
]
