"""Versioned, connector-neutral contracts owned by Frontend Connector Functions.

These validators carry connector facts into PAO and presentation facts back to
connectors. They never grant authority: PAO still resolves identity,
authorization, Session ownership, Run state, and tool execution.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any

_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}$")
_CONNECTOR = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_INSTANCE = re.compile(r"^[A-Z][A-Z0-9_-]{0,31}$")

FRONTEND_INGRESS_TYPE = "hashi.frontend-ingress"
FRONTEND_INGRESS_VERSION = 2
FRONTEND_REQUEST_TYPE = "hashi.frontend-request"
FRONTEND_REQUEST_VERSION = 1
FRONTEND_EVENT_TYPE = "hashi.frontend-event"
FRONTEND_EVENT_VERSION = 2
ADMISSION_RECEIPT_TYPE = "hashi.admission-receipt"
ADMISSION_RECEIPT_VERSION = 1
DELIVERY_INTENT_TYPE = "hashi.delivery-intent"
DELIVERY_INTENT_VERSION = 2
DELIVERY_RECEIPT_TYPE = "hashi.delivery-receipt"
DELIVERY_RECEIPT_VERSION = 1
MEDIA_GROUP_TYPE = "hashi.media-group"
MEDIA_GROUP_VERSION = 1
COMMAND_INVOCATION_TYPE = "hashi.frontend-command"
COMMAND_INVOCATION_VERSION = 2
RELAY_ENVELOPE_TYPE = "hashi.frontend-relay"
RELAY_ENVELOPE_VERSION = 1
TOOL_INTERACTION_TYPE = "hashi.frontend-tool-interaction"
TOOL_INTERACTION_VERSION = 1
MAX_CONTENT_BLOCKS = 128


def _object(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value


def _string(value: Any, name: str, *, maximum: int = 512) -> str:
    normalized = str(value or "").strip()
    if (
        not normalized
        or len(normalized) > maximum
        or any(ord(char) < 32 for char in normalized)
    ):
        raise ValueError(f"{name} is invalid")
    return normalized


def _token(value: Any, name: str, *, maximum: int = 256) -> str:
    normalized = _string(value, name, maximum=maximum)
    if not _TOKEN.fullmatch(normalized):
        raise ValueError(f"{name} is invalid")
    return normalized


def _issued_action_id(value: Any) -> str:
    """Accept normal tokens and URL-safe IDs emitted by command-menu buttons."""
    normalized = _string(value, "issued_action_id", maximum=96)
    if _TOKEN.fullmatch(normalized) or re.fullmatch(r"[A-Za-z0-9_-]{16,96}", normalized):
        return normalized
    raise ValueError("issued_action_id is invalid")


def _connector(value: Any, name: str = "connector_id") -> str:
    normalized = _string(value, name, maximum=64).casefold()
    if not _CONNECTOR.fullmatch(normalized):
        raise ValueError(f"{name} is invalid")
    return normalized


def _version(value: Mapping[str, Any], expected_type: str, expected_version: int) -> None:
    if str(value.get("type") or "") != expected_type:
        raise ValueError(f"unsupported {expected_type} type")
    if value.get("version") != expected_version:
        raise ValueError(f"unsupported {expected_type} version")


def _digest(value: Any, name: str) -> str:
    normalized = _string(value, name, maximum=71).casefold()
    if not _DIGEST.fullmatch(normalized):
        raise ValueError(f"{name} must be a sha256 digest")
    return normalized


def normalize_frontend_ingress_envelope(value: Any) -> dict[str, Any]:
    """Validate the server-normalized, content-free facts for one admission."""

    raw = _object(value, "frontend ingress")
    _version(raw, FRONTEND_INGRESS_TYPE, FRONTEND_INGRESS_VERSION)
    if raw.get("scope") != "current_message":
        raise ValueError("frontend ingress scope must be current_message")
    instance = _string(raw.get("instance_id"), "instance_id", maximum=32).upper()
    if not _INSTANCE.fullmatch(instance):
        raise ValueError("instance_id is invalid")
    connector_raw = _object(raw.get("connector"), "connector")
    connector_id = _connector(connector_raw.get("id"))
    endpoint_id = _token(connector_raw.get("endpoint_id"), "endpoint_id")
    principal_raw = _object(raw.get("principal"), "principal")
    principal_kind = str(principal_raw.get("kind") or "").casefold()
    if principal_kind not in {"human_or_client", "agent", "system"}:
        raise ValueError("principal kind is invalid")
    assurance = str(principal_raw.get("assurance") or "").casefold()
    if assurance not in {
        "declared",
        "connector_asserted",
        "runtime_observed",
        "exchange_verified",
        "unknown",
    }:
        raise ValueError("principal assurance is invalid")
    message_raw = _object(raw.get("message"), "message")
    request_id = _token(message_raw.get("request_id"), "request_id")
    idempotency_digest = _digest(
        message_raw.get("idempotency_digest"), "idempotency_digest"
    )
    target_raw = _object(raw.get("target"), "target")
    target = {
        "session_id": _token(target_raw.get("session_id"), "session_id"),
        "agent_id": _token(target_raw.get("agent_id"), "agent_id").casefold(),
    }
    network_authentication = str(
        raw.get("network_authentication") or "not_applicable"
    ).casefold()
    if not _CONNECTOR.fullmatch(network_authentication):
        raise ValueError("network_authentication is invalid")
    relay_raw = raw.get("relay_chain", [])
    if not isinstance(relay_raw, (list, tuple)) or len(relay_raw) > 8:
        raise ValueError("relay_chain must contain at most 8 hops")
    relay_chain = [
        _string(item, "relay_chain item", maximum=32).upper()
        for item in relay_raw
    ]
    if any(not _INSTANCE.fullmatch(item) for item in relay_chain):
        raise ValueError("relay_chain contains an invalid instance")
    return {
        "type": FRONTEND_INGRESS_TYPE,
        "version": FRONTEND_INGRESS_VERSION,
        "scope": "current_message",
        "instance_id": instance,
        "connector": {"id": connector_id, "endpoint_id": endpoint_id},
        "principal": {"kind": principal_kind, "assurance": assurance},
        "message": {
            "request_id": request_id,
            "idempotency_digest": idempotency_digest,
        },
        "target": target,
        "network_authentication": network_authentication,
        "relay_chain": relay_chain,
    }


def build_frontend_ingress_envelope(
    *,
    source_id: str,
    ingress_transport: str,
    surface: str,
    channel_key: str,
    instance_id: str,
    principal: Mapping[str, Any],
    network_authentication: str,
    relay_chain: list[str] | tuple[str, ...],
    request_id: str,
    idempotency_key: str,
    session_id: str,
    agent_id: str,
) -> dict[str, Any]:
    """Build the connector envelope from server-resolved admission facts."""

    from orchestrator.frontend_connector_registry import (
        canonical_connector_id,
        endpoint_id_for,
    )

    connector_id = canonical_connector_id(
        source_id,
        ingress_transport=ingress_transport,
        surface=surface,
    )
    digest = hashlib.sha256(str(idempotency_key).encode("utf-8")).hexdigest()
    return normalize_frontend_ingress_envelope(
        {
            "type": FRONTEND_INGRESS_TYPE,
            "version": FRONTEND_INGRESS_VERSION,
            "scope": "current_message",
            "instance_id": instance_id,
            "connector": {
                "id": connector_id,
                "endpoint_id": endpoint_id_for(
                    connector_id,
                    ingress_transport=ingress_transport,
                    channel_key=channel_key,
                ),
            },
            "principal": {
                "kind": principal.get("kind"),
                "assurance": principal.get("assurance"),
            },
            "message": {
                "request_id": request_id,
                "idempotency_digest": f"sha256:{digest}",
            },
            "target": {"session_id": session_id, "agent_id": agent_id},
            "network_authentication": network_authentication,
            "relay_chain": list(relay_chain),
        }
    )


def normalize_frontend_operation(value: Any) -> dict[str, Any]:
    """Validate the transport-neutral meaning of one frontend request."""

    raw = _object(value, "frontend operation")
    kind = str(raw.get("kind") or "").strip().casefold()
    if kind == "message":
        content_raw = raw.get("content")
        if not isinstance(content_raw, (list, tuple)) or not content_raw:
            raise ValueError("message operation requires content")
        if len(content_raw) > 64:
            raise ValueError("message operation content exceeds the limit")
        content: list[dict[str, Any]] = []
        for ordinal, entry in enumerate(content_raw):
            item = _object(entry, "message content")
            item_type = str(item.get("type") or "").strip().casefold()
            if item_type == "text":
                text = str(item.get("text") or "")
                if not text or len(text) > 1_000_000:
                    raise ValueError("message text is invalid")
                content.append({"type": "text", "text": text})
            elif item_type == "attachment_ref":
                declared_ordinal = item.get("ordinal", ordinal)
                if declared_ordinal != ordinal:
                    raise ValueError("message attachment ordinals must preserve order")
                content.append(
                    {
                        "type": "attachment_ref",
                        "attachment_id": _token(
                            item.get("attachment_id"), "attachment_id"
                        ),
                        "ordinal": ordinal,
                        "caption": (
                            _string(item.get("caption"), "caption", maximum=4096)
                            if item.get("caption")
                            else None
                        ),
                    }
                )
            elif item_type == "reply_ref":
                content.append(
                    {
                        "type": "reply_ref",
                        "event_id": _token(item.get("event_id"), "event_id"),
                    }
                )
            else:
                raise ValueError(f"unsupported message content type: {item_type}")
        return {"kind": "message", "content": content}

    if kind == "command":
        arguments = raw.get("arguments", [])
        if not isinstance(arguments, (list, tuple)) or len(arguments) > 64:
            raise ValueError("command arguments must be a list of at most 64 values")
        return {
            "kind": "command",
            "name": _token(
                str(raw.get("name") or "").lstrip("/"),
                "command name",
                maximum=128,
            ).casefold(),
            "arguments": [
                _string(argument, "command argument", maximum=4096)
                for argument in arguments
            ],
        }

    if kind == "action":
        revision = raw.get("revision")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise ValueError("action revision is invalid")
        return {
            "kind": "action",
            "action_id": _issued_action_id(raw.get("action_id")),
            "revision": revision,
        }

    if kind == "control":
        action = str(raw.get("action") or "").strip().casefold()
        if action not in {
            "cancel",
            "stop",
            "steer",
            "retry",
            "approve",
            "reject",
            "fresh",
        }:
            raise ValueError("control action is invalid")
        return {
            "kind": "control",
            "action": action,
            "target_run_id": (
                _token(raw.get("target_run_id"), "target_run_id")
                if raw.get("target_run_id")
                else None
            ),
            "text": (
                _string(raw.get("text"), "control text", maximum=65536)
                if raw.get("text")
                else None
            ),
        }

    if kind == "ack":
        state = str(raw.get("state") or "received").strip().casefold()
        if state not in {"received", "displayed", "read"}:
            raise ValueError("ack state is invalid")
        return {
            "kind": "ack",
            "event_id": _token(raw.get("event_id"), "event_id"),
            "state": state,
        }

    raise ValueError("frontend operation kind is invalid")


def normalize_frontend_request(value: Any) -> dict[str, Any]:
    """Validate one complete Connector request before it reaches PAO."""

    raw = _object(value, "frontend request")
    _version(raw, FRONTEND_REQUEST_TYPE, FRONTEND_REQUEST_VERSION)
    ingress = normalize_frontend_ingress_envelope(raw.get("ingress"))
    operation = normalize_frontend_operation(raw.get("operation"))
    from orchestrator.frontend_connector_registry import require_connector_operation

    require_connector_operation(
        ingress["connector"]["id"], "ingress", operation["kind"]
    )
    return {
        "type": FRONTEND_REQUEST_TYPE,
        "version": FRONTEND_REQUEST_VERSION,
        "ingress": ingress,
        "operation": operation,
    }


def normalize_delivery_intent(value: Any) -> dict[str, Any]:
    """Validate one immutable event-to-endpoint routing decision."""

    raw = _object(value, "delivery intent")
    _version(raw, DELIVERY_INTENT_TYPE, DELIVERY_INTENT_VERSION)
    if raw.get("scope") not in {"run", "event"}:
        raise ValueError("delivery intent scope is invalid")
    event_id = _token(raw.get("event_id"), "event_id")
    session_id = _token(raw.get("session_id"), "session_id")
    digest = _digest(raw.get("idempotency_digest"), "idempotency_digest")
    destinations_raw = raw.get("destinations")
    if not isinstance(destinations_raw, (list, tuple)) or len(destinations_raw) > 32:
        raise ValueError("delivery destinations must be a list of at most 32 entries")
    destinations: list[dict[str, Any]] = []
    endpoint_ids: set[str] = set()
    primary_count = 0
    for entry in destinations_raw:
        item = _object(entry, "delivery destination")
        connector_id = _connector(item.get("connector_id"))
        endpoint_id = _token(item.get("endpoint_id"), "endpoint_id")
        if endpoint_id in endpoint_ids:
            raise ValueError("delivery endpoint ids must be unique")
        endpoint_ids.add(endpoint_id)
        role = str(item.get("role") or "").casefold()
        if role not in {"primary", "mirror", "subscriber"}:
            raise ValueError("delivery destination role is invalid")
        primary_count += role == "primary"
        channel_key = _string(item.get("channel_key"), "channel_key")
        modes_raw = item.get("content_modes")
        if not isinstance(modes_raw, (list, tuple)) or not modes_raw:
            raise ValueError("content_modes must be a non-empty list")
        content_modes = sorted(
            {_token(mode, "content_mode", maximum=32).casefold() for mode in modes_raw}
        )
        retry_class = str(item.get("retry_class") or "").casefold()
        if retry_class not in {"never", "idempotent", "query_before_retry"}:
            raise ValueError("retry_class is invalid")
        destinations.append(
            {
                "connector_id": connector_id,
                "endpoint_id": endpoint_id,
                "role": role,
                "content_modes": content_modes,
                "retry_class": retry_class,
            }
        )
    if primary_count > 1:
        raise ValueError("delivery intent may contain at most one primary endpoint")
    return {
        "type": DELIVERY_INTENT_TYPE,
        "version": DELIVERY_INTENT_VERSION,
        "scope": str(raw["scope"]),
        "event_id": event_id,
        "session_id": session_id,
        "idempotency_digest": digest,
        "destinations": destinations,
    }


def normalize_delivery_receipt(value: Any) -> dict[str, Any]:
    raw = _object(value, "delivery receipt")
    _version(raw, DELIVERY_RECEIPT_TYPE, DELIVERY_RECEIPT_VERSION)
    status = str(raw.get("status") or "").casefold()
    if status not in {
        "queued",
        "accepted",
        "delivered",
        "failed",
        "unknown",
        "duplicate",
        "skipped",
    }:
        raise ValueError("delivery receipt status is invalid")
    proof_raw = raw.get("proof")
    proof = None
    if proof_raw is not None:
        proof_obj = _object(proof_raw, "delivery receipt proof")
        proof = {
            "type": _token(proof_obj.get("type"), "proof type", maximum=64),
            "value": _token(proof_obj.get("value"), "proof value"),
        }
    if status == "delivered" and proof is None:
        raise ValueError("delivered receipt requires proof")
    return {
        "type": DELIVERY_RECEIPT_TYPE,
        "version": DELIVERY_RECEIPT_VERSION,
        "event_id": _token(raw.get("event_id"), "event_id"),
        "endpoint_id": _token(raw.get("endpoint_id"), "endpoint_id"),
        "status": status,
        "proof": proof,
    }


def normalize_media_group(value: Any) -> dict[str, Any]:
    raw = _object(value, "media group")
    _version(raw, MEDIA_GROUP_TYPE, MEDIA_GROUP_VERSION)
    retention_class = str(raw.get("retention_class") or "").casefold()
    if retention_class not in {
        "ephemeral",
        "spool",
        "session",
        "durable",
        "cache",
        "staging_unbound",
        "message_bound",
        "delivery_spool",
        "preview_cache",
        "quarantine",
        "correlation_marker",
        "audit_metadata",
    }:
        raise ValueError("media retention_class is invalid")
    attachments_raw = raw.get("attachments")
    if not isinstance(attachments_raw, (list, tuple)) or not attachments_raw:
        raise ValueError("media group attachments must be a non-empty list")
    attachments: list[dict[str, Any]] = []
    for expected_ordinal, entry in enumerate(attachments_raw):
        item = _object(entry, "media attachment")
        if item.get("ordinal") != expected_ordinal:
            raise ValueError("media group ordinals must be contiguous and ordered")
        digest = str(item.get("sha256") or "").casefold()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("media attachment sha256 is invalid")
        normalized = {
            "attachment_id": _token(item.get("attachment_id"), "attachment_id"),
            "ordinal": expected_ordinal,
            "sha256": digest,
        }
        if item.get("filename") is not None:
            normalized["filename"] = _string(
                item.get("filename"), "filename", maximum=255
            )
        attachments.append(normalized)
    retention_indefinite = raw.get("retention_indefinite", False)
    if not isinstance(retention_indefinite, bool):
        raise ValueError("retention_indefinite must be boolean")
    if retention_indefinite and retention_class != "durable":
        raise ValueError("indefinite retention requires durable class")
    expires_at = raw.get("expires_at")
    if expires_at is not None and (
        not isinstance(expires_at, int) or isinstance(expires_at, bool) or expires_at < 0
    ):
        raise ValueError("media expires_at must be a non-negative timestamp")
    if retention_indefinite and expires_at is not None:
        raise ValueError("indefinite media cannot have expires_at")
    return {
        "type": MEDIA_GROUP_TYPE,
        "version": MEDIA_GROUP_VERSION,
        "group_id": _token(raw.get("group_id"), "group_id"),
        "retention_class": retention_class,
        "retention_indefinite": retention_indefinite,
        "expires_at": expires_at,
        "attachments": attachments,
    }


def normalize_command_invocation(value: Any) -> dict[str, Any]:
    raw = _object(value, "command invocation")
    _version(raw, COMMAND_INVOCATION_TYPE, COMMAND_INVOCATION_VERSION)
    arguments = raw.get("arguments", [])
    if not isinstance(arguments, (list, tuple)) or len(arguments) > 64:
        raise ValueError("command arguments must be a list of at most 64 values")
    authorization_raw = _object(raw.get("authorization"), "authorization")
    decision = str(authorization_raw.get("decision") or "").casefold()
    if decision not in {"allowed", "denied", "pending", "unknown"}:
        raise ValueError("command authorization decision is invalid")
    context_generation = raw.get("context_generation")
    if (
        not isinstance(context_generation, int)
        or isinstance(context_generation, bool)
        or context_generation < 1
    ):
        raise ValueError("command context_generation is invalid")
    revision = raw.get("revision")
    if revision is not None and (
        not isinstance(revision, int) or isinstance(revision, bool) or revision < 1
    ):
        raise ValueError("command revision is invalid")
    issued_action_id = raw.get("issued_action_id")
    return {
        "type": COMMAND_INVOCATION_TYPE,
        "version": COMMAND_INVOCATION_VERSION,
        "invocation_id": _token(raw.get("invocation_id"), "invocation_id"),
        "request_id": _token(raw.get("request_id"), "request_id"),
        "connector_id": _connector(raw.get("connector_id")),
        "endpoint_id": _token(raw.get("endpoint_id"), "endpoint_id"),
        "session_id": _token(raw.get("session_id"), "session_id"),
        "context_generation": context_generation,
        "command": _token(raw.get("command"), "command", maximum=128).casefold(),
        "issued_action_id": (
            _issued_action_id(issued_action_id)
            if issued_action_id is not None
            else None
        ),
        "revision": revision,
        "arguments": [
            _string(argument, "command argument", maximum=4096)
            for argument in arguments
        ],
        "actor_digest": _digest(raw.get("actor_digest"), "actor_digest"),
        "idempotency_digest": _digest(
            raw.get("idempotency_digest"), "idempotency_digest"
        ),
        "authorization": {
            "decision": decision,
            "scope": _token(
                authorization_raw.get("scope"), "authorization scope", maximum=128
            ),
        },
    }


def normalize_relay_envelope(value: Any) -> dict[str, Any]:
    raw = _object(value, "relay envelope")
    _version(raw, RELAY_ENVELOPE_TYPE, RELAY_ENVELOPE_VERSION)
    origin = _string(raw.get("origin_instance"), "origin_instance", maximum=32).upper()
    target = _string(raw.get("target_instance"), "target_instance", maximum=32).upper()
    if not _INSTANCE.fullmatch(origin) or not _INSTANCE.fullmatch(target):
        raise ValueError("relay instance id is invalid")
    hops = raw.get("hop_limit")
    if not isinstance(hops, int) or isinstance(hops, bool) or not 1 <= hops <= 8:
        raise ValueError("relay hop_limit must be between 1 and 8")
    chain_raw = raw.get("relay_chain")
    if not isinstance(chain_raw, (list, tuple)) or len(chain_raw) > hops:
        raise ValueError("relay chain exceeds hop limit")
    chain = [_string(item, "relay hop", maximum=32).upper() for item in chain_raw]
    if any(not _INSTANCE.fullmatch(item) for item in chain) or len(set(chain)) != len(chain):
        raise ValueError("relay chain is invalid or contains a loop")
    payload_raw = _object(raw.get("payload_ref"), "payload_ref")
    return {
        "type": RELAY_ENVELOPE_TYPE,
        "version": RELAY_ENVELOPE_VERSION,
        "correlation_id": _token(raw.get("correlation_id"), "correlation_id"),
        "origin_instance": origin,
        "target_instance": target,
        "relay_chain": chain,
        "hop_limit": hops,
        "payload_ref": {
            "type": _token(payload_raw.get("type"), "payload type", maximum=96),
            "id": _token(payload_raw.get("id"), "payload id"),
        },
    }


def normalize_tool_interaction(value: Any) -> dict[str, Any]:
    raw = _object(value, "tool interaction")
    _version(raw, TOOL_INTERACTION_TYPE, TOOL_INTERACTION_VERSION)
    tool_class = str(raw.get("tool_class") or "").casefold()
    if tool_class not in {"browser", "computer", "ordinary"}:
        raise ValueError("tool_class is invalid")
    state = str(raw.get("state") or "").casefold()
    if state not in {"started", "completed", "failed", "cancelled"}:
        raise ValueError("tool interaction state is invalid")
    scopes = raw.get("authorization_scope")
    if not isinstance(scopes, (list, tuple)) or not scopes:
        raise ValueError("tool interaction requires authorization_scope")
    side_effects_possible = raw.get("side_effects_possible")
    if not isinstance(side_effects_possible, bool):
        raise ValueError("side_effects_possible must be boolean")
    artifact_refs = raw.get("artifact_refs", [])
    if not isinstance(artifact_refs, (list, tuple)) or len(artifact_refs) > 32:
        raise ValueError("artifact_refs must contain at most 32 items")
    return {
        "type": TOOL_INTERACTION_TYPE,
        "version": TOOL_INTERACTION_VERSION,
        "invocation_id": _token(raw.get("invocation_id"), "invocation_id"),
        "request_id": _token(raw.get("request_id"), "request_id"),
        "session_id": _token(raw.get("session_id"), "session_id"),
        "tool_class": tool_class,
        "tool_name": _token(raw.get("tool_name"), "tool_name", maximum=128),
        "state": state,
        "authorization_scope": sorted(
            {_token(scope, "authorization scope", maximum=128) for scope in scopes}
        ),
        "side_effects_possible": side_effects_possible,
        "artifact_refs": [
            _token(item, "artifact reference") for item in artifact_refs
        ],
    }


def normalize_content_component(value: Any) -> dict[str, Any]:
    raw = _object(value, "content component")
    component_type = _token(raw.get("type"), "component type", maximum=32).casefold()
    if component_type == "text":
        text = str(raw.get("text") or "")
        fmt = str(raw.get("format") or "plain").casefold()
        if fmt == "html":
            raise ValueError(
                "standard frontend text must be transport-neutral; HTML belongs in an adapter"
            )
        if fmt not in {"plain", "markdown"}:
            raise ValueError("text component format is invalid")
        return {"type": "text", "text": text, "format": fmt}
    elif component_type in {"key_value", "kv"}:
        items_raw = raw.get("items")
        if not isinstance(items_raw, (list, tuple)):
            raise ValueError("key_value component requires items list")
        items = []
        for entry in items_raw:
            entry_obj = _object(entry, "key_value item")
            items.append({
                "key": _string(entry_obj.get("key"), "key_value key", maximum=128),
                "value": _string(entry_obj.get("value"), "key_value value", maximum=4096),
            })
        return {"type": "key_value", "items": items}
    elif component_type == "table":
        headers_raw = raw.get("headers", [])
        if not isinstance(headers_raw, (list, tuple)):
            raise ValueError("table headers must be a list")
        headers = [_string(h, "table header", maximum=128) for h in headers_raw]
        rows_raw = raw.get("rows", [])
        if not isinstance(rows_raw, (list, tuple)):
            raise ValueError("table rows must be a list")
        rows = []
        for r in rows_raw:
            if not isinstance(r, (list, tuple)):
                raise ValueError("table row must be a list")
            rows.append([_string(cell, "table cell", maximum=4096) for cell in r])
        return {"type": "table", "headers": headers, "rows": rows}
    elif component_type == "action":
        action_id = _token(raw.get("action_id"), "action_id")
        label = _string(raw.get("label"), "action label", maximum=128)
        style = str(raw.get("style") or "primary").casefold()
        if style not in {"primary", "secondary", "danger"}:
            raise ValueError("action component style is invalid")
        payload = dict(raw.get("payload") or {}) if isinstance(raw.get("payload"), Mapping) else {}
        return {
            "type": "action",
            "interface_kind": "button",
            "action_id": action_id,
            "label": label,
            "style": style,
            "payload": payload,
        }
    elif component_type == "hint":
        level = str(raw.get("level") or "info").casefold()
        if level not in {"info", "warning", "error"}:
            raise ValueError("hint component level is invalid")
        return {"type": "hint", "level": level, "text": _string(raw.get("text"), "hint text", maximum=1024)}
    elif component_type == "media_ref":
        return {
            "type": "media_ref",
            "group_id": _token(raw.get("group_id"), "group_id"),
            "attachment_id": _token(raw.get("attachment_id"), "attachment_id"),
            "role": str(raw.get("role") or "").casefold(),
            "caption": _string(raw.get("caption"), "caption", maximum=512) if raw.get("caption") else None,
        }
    else:
        raise ValueError(f"unsupported content component type: {component_type}")


def normalize_content_blocks(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("content_blocks must be a list")
    if len(value) > MAX_CONTENT_BLOCKS:
        raise ValueError(
            f"content_blocks exceeds maximum limit of {MAX_CONTENT_BLOCKS}"
        )
    return [normalize_content_component(item) for item in value]


def normalize_frontend_event(value: Any) -> dict[str, Any]:
    raw = _object(value, "frontend event")
    _version(raw, FRONTEND_EVENT_TYPE, FRONTEND_EVENT_VERSION)
    event_id = _token(raw.get("event_id"), "event_id")
    session_id = _token(raw.get("session_id"), "session_id")
    durability = str(raw.get("durability") or "durable").casefold()
    if durability not in {"durable", "ephemeral"}:
        raise ValueError("frontend event durability is invalid")

    sequence = raw.get("sequence")
    if durability == "durable":
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
            raise ValueError("durable event requires non-negative integer sequence")
    else:
        if sequence is not None and (
            not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0
        ):
            raise ValueError("ephemeral event sequence must be non-negative integer or null")

    epoch = raw.get("epoch")
    if epoch is not None and (
        not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 0
    ):
        raise ValueError("frontend event epoch must be non-negative integer")

    ephemeral_sequence = raw.get("ephemeral_sequence")
    if ephemeral_sequence is not None and (
        not isinstance(ephemeral_sequence, int)
        or isinstance(ephemeral_sequence, bool)
        or ephemeral_sequence < 0
    ):
        raise ValueError("ephemeral_sequence must be non-negative integer")

    audience = str(raw.get("audience") or "user").casefold()
    if audience not in {"user", "internal", "admin"}:
        raise ValueError("frontend event audience is invalid")

    visibility = str(raw.get("visibility") or "public").casefold()
    if visibility not in {"public", "ephemeral_preview", "debug"}:
        raise ValueError("frontend event visibility is invalid")

    semantic_kind = _token(
        raw.get("semantic_kind") or "final", "semantic_kind", maximum=64
    ).casefold()
    presentation_channel = _token(
        raw.get("presentation_channel") or "final", "presentation_channel", maximum=64
    ).casefold()

    interface_kind = str(raw.get("interface_kind") or "").strip().casefold()
    if not interface_kind:
        if semantic_kind in {"final", "message", "commentary"}:
            interface_kind = "message"
        elif semantic_kind in {
            "meter",
            "herv2",
            "command",
            "command_result",
            "approval",
            "status",
            "error",
            "display",
        }:
            interface_kind = "display"
        elif semantic_kind in {
            "reasoning",
            "technical",
            "answer_preview",
            "progress",
        }:
            interface_kind = "state"
        else:
            interface_kind = "display"
    if interface_kind not in {"message", "display", "state", "notification"}:
        raise ValueError("frontend event interface_kind is invalid")

    content_blocks = normalize_content_blocks(raw.get("content_blocks", []))
    created_at = _string(raw.get("created_at") or "", "created_at", maximum=64)

    return {
        "type": FRONTEND_EVENT_TYPE,
        "version": FRONTEND_EVENT_VERSION,
        "event_id": event_id,
        "message_id": _token(raw.get("message_id"), "message_id")
        if raw.get("message_id")
        else None,
        "session_id": session_id,
        "sequence": sequence,
        "run_id": _token(raw.get("run_id"), "run_id") if raw.get("run_id") else None,
        "request_id": _token(raw.get("request_id"), "request_id")
        if raw.get("request_id")
        else None,
        "durability": durability,
        "epoch": epoch,
        "ephemeral_sequence": ephemeral_sequence,
        "audience": audience,
        "visibility": visibility,
        "interface_kind": interface_kind,
        "semantic_kind": semantic_kind,
        "presentation_channel": presentation_channel,
        "content_blocks": content_blocks,
        "delivery_intent_ref": _token(raw.get("delivery_intent_ref"), "delivery_intent_ref")
        if raw.get("delivery_intent_ref")
        else None,
        "replaces_event_id": _token(raw.get("replaces_event_id"), "replaces_event_id")
        if raw.get("replaces_event_id")
        else None,
        "superseded_by": _token(raw.get("superseded_by"), "superseded_by")
        if raw.get("superseded_by")
        else None,
        "reply_to_event_id": _token(raw.get("reply_to_event_id"), "reply_to_event_id")
        if raw.get("reply_to_event_id")
        else None,
        "created_at": created_at,
    }


def normalize_admission_receipt(value: Any) -> dict[str, Any]:
    raw = _object(value, "admission receipt")
    _version(raw, ADMISSION_RECEIPT_TYPE, ADMISSION_RECEIPT_VERSION)
    status = str(raw.get("status") or "").casefold()
    if status not in {"accepted", "duplicate", "rejected", "conflict"}:
        raise ValueError("admission receipt status is invalid")
    session_id = _token(raw.get("session_id"), "session_id")
    request_id = _token(raw.get("request_id"), "request_id")
    idempotency_digest = _digest(raw.get("idempotency_digest"), "idempotency_digest")
    replayed = raw.get("replayed", False)
    if not isinstance(replayed, bool):
        raise ValueError("replayed must be a boolean")
    run_id = _token(raw.get("run_id"), "run_id") if raw.get("run_id") else None
    reason = _string(raw.get("reason"), "reason", maximum=256) if raw.get("reason") else None
    return {
        "type": ADMISSION_RECEIPT_TYPE,
        "version": ADMISSION_RECEIPT_VERSION,
        "status": status,
        "session_id": session_id,
        "run_id": run_id,
        "request_id": request_id,
        "idempotency_digest": idempotency_digest,
        "replayed": replayed,
        "reason": reason,
    }


__all__ = [
    "ADMISSION_RECEIPT_TYPE",
    "ADMISSION_RECEIPT_VERSION",
    "COMMAND_INVOCATION_TYPE",
    "COMMAND_INVOCATION_VERSION",
    "DELIVERY_INTENT_TYPE",
    "DELIVERY_INTENT_VERSION",
    "DELIVERY_RECEIPT_TYPE",
    "DELIVERY_RECEIPT_VERSION",
    "FRONTEND_EVENT_TYPE",
    "FRONTEND_EVENT_VERSION",
    "FRONTEND_INGRESS_TYPE",
    "FRONTEND_INGRESS_VERSION",
    "FRONTEND_REQUEST_TYPE",
    "FRONTEND_REQUEST_VERSION",
    "MEDIA_GROUP_TYPE",
    "MEDIA_GROUP_VERSION",
    "RELAY_ENVELOPE_TYPE",
    "RELAY_ENVELOPE_VERSION",
    "TOOL_INTERACTION_TYPE",
    "TOOL_INTERACTION_VERSION",
    "build_frontend_ingress_envelope",
    "normalize_admission_receipt",
    "normalize_command_invocation",
    "normalize_content_blocks",
    "normalize_content_component",
    "normalize_delivery_intent",
    "normalize_delivery_receipt",
    "normalize_frontend_event",
    "normalize_frontend_ingress_envelope",
    "normalize_frontend_operation",
    "normalize_frontend_request",
    "normalize_media_group",
    "normalize_relay_envelope",
    "normalize_tool_interaction",
]
