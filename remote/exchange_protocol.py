"""HASHI-side codec for the independent Exchange wire protocol v1."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from typing import Any, Mapping

from remote.internet_address import (
    AddressError,
    ExchangeAddress,
    PublicAddress,
    exchange_identifier,
    normalize_public_agent,
)


SUBPROTOCOL = "hashi-exchange.v1"
WIRE_VERSION = 1
REMOTE_PROTOCOL_VERSION = "2.0"
AUTHORIZED_ROUTES_CAPABILITY = "authorized_routes_v1"
REQUIRED_CAPABILITIES = (
    "hchat_v1",
    "receipt_v1",
    "agent_reply_v1",
    "public_address_v1",
)
CAPABILITIES = REQUIRED_CAPABILITIES + (AUTHORIZED_ROUTES_CAPABILITY,)
MESSAGE_KINDS = frozenset({"agent_message", "agent_reply"})
MAX_FRAME_BYTES = 65536
MAX_JSON_DEPTH = 8
MAX_JSON_ITEMS = 2048
_TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?Z$",
    re.ASCII,
)


class ExchangeProtocolError(ValueError):
    def __init__(self, code: str):
        self.code = str(code or "INVALID_MESSAGE")
        super().__init__(self.code)


def _require(condition: bool, code: str = "INVALID_MESSAGE") -> None:
    if not condition:
        raise ExchangeProtocolError(code)


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def _bad_constant(_value: str) -> None:
    raise ExchangeProtocolError("INVALID_MESSAGE")


def strict_json(raw: Any, *, max_bytes: int = MAX_FRAME_BYTES) -> Any:
    _require(isinstance(raw, str))
    try:
        _require(len(raw.encode("utf-8")) <= max_bytes, "PAYLOAD_TOO_LARGE")
        value = json.loads(
            raw,
            object_pairs_hook=_unique_pairs,
            parse_constant=_bad_constant,
        )
    except ExchangeProtocolError:
        raise
    except (ValueError, RecursionError, UnicodeError) as exc:
        raise ExchangeProtocolError("INVALID_MESSAGE") from exc
    pending = [(value, 0)]
    count = 0
    while pending:
        item, depth = pending.pop()
        count += 1
        _require(depth <= MAX_JSON_DEPTH and count <= MAX_JSON_ITEMS)
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
        elif isinstance(item, str):
            _require(
                not any(0xD800 <= ord(character) <= 0xDFFF for character in item)
            )
        elif isinstance(item, float):
            _require(math.isfinite(item))
    return value


def canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ExchangeProtocolError("INVALID_MESSAGE") from exc


def _keys(
    value: Any,
    *,
    required: set[str],
    optional: set[str] = frozenset(),
) -> dict[str, Any]:
    _require(isinstance(value, dict))
    _require(required <= set(value) <= (required | optional))
    return value


def _text(value: Any, *, limit: int = 128) -> str:
    _require(isinstance(value, str) and 0 < len(value) <= limit)
    _require(not any(0xD800 <= ord(character) <= 0xDFFF for character in value))
    return value


def parse_timestamp(value: Any) -> float:
    _require(isinstance(value, str) and _TIMESTAMP_RE.fullmatch(value) is not None)
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
        offset = parsed.utcoffset()
        _require(offset is not None and offset.total_seconds() == 0)
        return parsed.timestamp()
    except (ValueError, OverflowError, AttributeError) as exc:
        raise ExchangeProtocolError("INVALID_MESSAGE") from exc


def utc_timestamp(value: float) -> str:
    return (
        datetime.fromtimestamp(value, timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _identifier(value: Any, *, field: str) -> str:
    try:
        return exchange_identifier(value, field=field)
    except AddressError as exc:
        raise ExchangeProtocolError("INVALID_MESSAGE") from exc


def _address(value: Any) -> dict[str, str]:
    try:
        return ExchangeAddress.from_mapping(value).to_mapping()
    except AddressError as exc:
        raise ExchangeProtocolError("INVALID_ADDRESS") from exc


def _content(value: Any) -> dict[str, Any]:
    content = _keys(
        value,
        required={
            "format",
            "text",
            "authorization_message_id",
            "authorization_resources",
            "private_authorization_proofs",
        },
    )
    _require(content["format"] == "hchat")
    text = _text(content["text"], limit=MAX_FRAME_BYTES)
    _require(len(text.encode("utf-8")) <= MAX_FRAME_BYTES, "PAYLOAD_TOO_LARGE")
    # Exchange v1 has no negotiated private-proof capability.  Dropping a
    # non-empty proof would turn a private request into a public one.
    _require(
        content["authorization_message_id"] is None
        and content["authorization_resources"] == []
        and content["private_authorization_proofs"] == [],
        "UNSUPPORTED_CAPABILITY",
    )
    return {
        "format": "hchat",
        "text": text,
        "authorization_message_id": None,
        "authorization_resources": [],
        "private_authorization_proofs": [],
    }


def decode_server_frame(raw: Any) -> dict[str, Any]:
    frame = strict_json(raw)
    _require(isinstance(frame, dict))
    _require(type(frame.get("v")) is int and frame["v"] == WIRE_VERSION)
    kind = frame.get("type")
    _require(
        kind
        in {
            "welcome",
            "published",
            "resolved",
            "receipt",
            "delivery",
            "authorized_routes",
            "error",
        },
        "UNSUPPORTED_CAPABILITY",
    )
    if kind == "welcome":
        _keys(
            frame,
            required={
                "v",
                "type",
                "connection_id",
                "epoch",
                "authority_id",
                "actor_id",
                "registered_instance_id",
                "instance_address",
                "capabilities",
                "limits",
                "heartbeat_seconds",
                "lease_expires_at",
            },
            optional={"mode"},
        )
        for key in (
            "connection_id",
            "epoch",
            "authority_id",
            "actor_id",
            "registered_instance_id",
        ):
            frame[key] = _identifier(frame[key], field=key)
        try:
            parsed_instance = PublicAddress.parse(
                f"x@{_text(frame['instance_address'])}"
            )
        except AddressError as exc:
            raise ExchangeProtocolError("INVALID_ADDRESS") from exc
        frame["instance_address"] = parsed_instance.instance_address
        capabilities = frame["capabilities"]
        _require(
            isinstance(capabilities, list)
            and len(capabilities) <= 16
            and all(isinstance(item, str) and len(item) <= 64 for item in capabilities)
        )
        # New relay features are optional.  The original v1 messaging baseline
        # remains sufficient for compatibility with an older Exchange.
        _require(
            set(REQUIRED_CAPABILITIES) <= set(capabilities),
            "UNSUPPORTED_CAPABILITY",
        )
        limits = _keys(
            frame["limits"],
            required={"max_message_bytes", "max_published_agents"},
        )
        _require(
            type(limits["max_message_bytes"]) is int
            and 0 < limits["max_message_bytes"] <= MAX_FRAME_BYTES
            and type(limits["max_published_agents"]) is int
            and 0 < limits["max_published_agents"] <= 100
        )
        _require(
            type(frame["heartbeat_seconds"]) in {int, float}
            and math.isfinite(frame["heartbeat_seconds"])
            and 0 < frame["heartbeat_seconds"] <= 60
        )
        parse_timestamp(frame["lease_expires_at"])
    elif kind == "published":
        _keys(
            frame,
            required={"v", "type", "request_id", "revision"},
        )
        frame["request_id"] = _identifier(
            frame["request_id"], field="request_id"
        )
        _require(
            type(frame["revision"]) is int and 0 < frame["revision"] <= 2**53
        )
    elif kind == "resolved":
        _keys(
            frame,
            required={"v", "type", "request_id", "to", "grant_revision"},
        )
        frame["request_id"] = _identifier(
            frame["request_id"], field="request_id"
        )
        frame["to"] = _address(frame["to"])
        _require(
            type(frame["grant_revision"]) is int
            and 0 <= frame["grant_revision"] <= 2**53
        )
    elif kind == "authorized_routes":
        _keys(
            frame,
            required={
                "v",
                "type",
                "request_id",
                "grant_revision",
                "refreshed_at",
                "routes",
            },
        )
        frame["request_id"] = _identifier(
            frame["request_id"], field="request_id"
        )
        _require(
            type(frame["grant_revision"]) is int
            and 0 <= frame["grant_revision"] <= 2**53
        )
        parse_timestamp(frame["refreshed_at"])
        _require(isinstance(frame["routes"], list) and len(frame["routes"]) <= 100)
        normalized_routes: list[dict[str, Any]] = []
        seen: set[str] = set()
        for value in frame["routes"]:
            route = _keys(
                value,
                required={"to", "message_kinds", "available"},
            )
            target = _address(route["to"])
            _require(target["address"] not in seen)
            seen.add(target["address"])
            kinds = route["message_kinds"]
            _require(
                isinstance(kinds, list)
                and 0 < len(kinds) <= len(MESSAGE_KINDS)
                and len(set(kinds)) == len(kinds)
                and all(kind in MESSAGE_KINDS for kind in kinds)
            )
            _require(type(route["available"]) is bool)
            normalized_routes.append({
                "to": target,
                "message_kinds": list(kinds),
                "available": route["available"],
            })
        frame["routes"] = normalized_routes
    elif kind == "receipt":
        _keys(
            frame,
            required={"v", "type", "message_id", "status"},
            optional={"request_id", "code"},
        )
        frame["message_id"] = _identifier(
            frame["message_id"], field="message_id"
        )
        if "request_id" in frame:
            frame["request_id"] = _identifier(
                frame["request_id"], field="request_id"
            )
        _require(
            frame["status"]
            in {"accepted", "delivered", "rejected", "delivery_unknown", "unknown"}
        )
        if "code" in frame:
            frame["code"] = _identifier(frame["code"], field="code")
    elif kind == "delivery":
        frame = validate_delivery_frame(frame)
    else:
        _keys(
            frame,
            required={"v", "type", "code", "retryable"},
            optional={"request_id", "message_id"},
        )
        frame["code"] = _identifier(frame["code"], field="code")
        _require(type(frame["retryable"]) is bool)
        for key in ("request_id", "message_id"):
            if frame.get(key) is not None:
                frame[key] = _identifier(frame[key], field=key)
    return frame


def validate_delivery_frame(value: Any) -> dict[str, Any]:
    frame = _keys(
        value,
        required={
            "v",
            "type",
            "delivery_id",
            "recipient_epoch",
            "sender",
            "recipient",
            "grant_revision",
            "authorization_expires_at",
            "message_id",
            "conversation_id",
            "created_at",
            "expires_at",
            "message_type",
            "in_reply_to",
            "content",
        },
    )
    _require(frame["v"] == WIRE_VERSION and frame["type"] == "delivery")
    normalized = dict(frame)
    for key in (
        "delivery_id",
        "recipient_epoch",
        "message_id",
        "conversation_id",
    ):
        normalized[key] = _identifier(frame[key], field=key)
    normalized["sender"] = _address(frame["sender"])
    normalized["recipient"] = _address(frame["recipient"])
    _require(
        normalized["sender"]["authority_id"]
        == normalized["recipient"]["authority_id"]
    )
    _require(
        type(frame["grant_revision"]) is int
        and 0 <= frame["grant_revision"] <= 2**53
    )
    for key in ("created_at", "expires_at", "authorization_expires_at"):
        parse_timestamp(frame[key])
    _require(frame["message_type"] in MESSAGE_KINDS)
    if frame["message_type"] == "agent_reply":
        normalized["in_reply_to"] = _identifier(
            frame["in_reply_to"], field="in_reply_to"
        )
    else:
        _require(frame["in_reply_to"] is None)
    normalized["content"] = _content(frame["content"])
    _require(len(canonical_json(normalized)) <= MAX_FRAME_BYTES, "PAYLOAD_TOO_LARGE")
    return normalized


def validate_delivery_deadline(
    delivery: Mapping[str, Any],
    *,
    now: float,
) -> None:
    created = parse_timestamp(delivery.get("created_at"))
    expires = parse_timestamp(delivery.get("expires_at"))
    authorization_expires = parse_timestamp(
        delivery.get("authorization_expires_at")
    )
    _require(0 < expires - created <= 600)
    _require(created <= now + 30)
    _require(now < expires, "MESSAGE_EXPIRED")
    _require(now < authorization_expires, "MESSAGE_EXPIRED")


def hello_frame() -> dict[str, Any]:
    return {
        "v": WIRE_VERSION,
        "type": "hello",
        "capabilities": list(CAPABILITIES),
        "remote_protocol_version": REMOTE_PROTOCOL_VERSION,
    }


def publish_frame(
    *,
    request_id: str,
    revision: int,
    agents: list[dict[str, Any]],
) -> dict[str, Any]:
    _identifier(request_id, field="request_id")
    _require(type(revision) is int and 0 < revision <= 2**53)
    _require(isinstance(agents, list) and len(agents) <= 100)
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in agents:
        item = _keys(
            raw,
            required={"agent_id", "display_name", "message_kinds"},
        )
        try:
            agent = normalize_public_agent(item["agent_id"])
        except AddressError as exc:
            raise ExchangeProtocolError("INVALID_ADDRESS") from exc
        _require(agent not in seen)
        seen.add(agent)
        display_name = _text(item["display_name"])
        kinds = item["message_kinds"]
        _require(
            isinstance(kinds, list)
            and 0 < len(kinds) <= 2
            and all(kind in MESSAGE_KINDS for kind in kinds)
        )
        normalized.append(
            {
                "agent_id": agent,
                "display_name": display_name,
                "message_kinds": list(dict.fromkeys(kinds)),
            }
        )
    return {
        "v": WIRE_VERSION,
        "type": "publish",
        "request_id": request_id,
        "revision": revision,
        "agents": normalized,
    }


def resolve_frame(
    *,
    request_id: str,
    from_agent: str,
    address: str,
) -> dict[str, Any]:
    return {
        "v": WIRE_VERSION,
        "type": "resolve",
        "request_id": _identifier(request_id, field="request_id"),
        "from_agent": normalize_public_agent(from_agent),
        "address": PublicAddress.parse(address).canonical,
    }


def routes_frame(*, request_id: str) -> dict[str, Any]:
    return {
        "v": WIRE_VERSION,
        "type": "routes",
        "request_id": _identifier(request_id, field="request_id"),
    }


def send_frame(
    *,
    message_id: str,
    conversation_id: str,
    from_agent: str,
    to: Mapping[str, Any],
    created_at: str,
    expires_at: str,
    message_type: str,
    in_reply_to: str | None,
    text: str,
) -> dict[str, Any]:
    _require(message_type in MESSAGE_KINDS)
    if message_type == "agent_reply":
        normalized_reply = _identifier(in_reply_to, field="in_reply_to")
    else:
        _require(in_reply_to is None)
        normalized_reply = None
    parse_timestamp(created_at)
    parse_timestamp(expires_at)
    content = _content(
        {
            "format": "hchat",
            "text": text,
            "authorization_message_id": None,
            "authorization_resources": [],
            "private_authorization_proofs": [],
        }
    )
    frame = {
        "v": WIRE_VERSION,
        "type": "send",
        "message_id": _identifier(message_id, field="message_id"),
        "conversation_id": _identifier(
            conversation_id, field="conversation_id"
        ),
        "from_agent": normalize_public_agent(from_agent),
        "to": _address(to),
        "created_at": created_at,
        "expires_at": expires_at,
        "message_type": message_type,
        "in_reply_to": normalized_reply,
        "content": content,
    }
    _require(len(canonical_json(frame)) <= MAX_FRAME_BYTES, "PAYLOAD_TOO_LARGE")
    return frame


def ack_frame(
    *,
    delivery_id: str,
    message_id: str,
    status: str,
    code: str | None = None,
) -> dict[str, Any]:
    _require(status in {"delivered", "rejected"})
    frame = {
        "v": WIRE_VERSION,
        "type": "ack",
        "delivery_id": _identifier(delivery_id, field="delivery_id"),
        "message_id": _identifier(message_id, field="message_id"),
        "status": status,
    }
    if status == "delivered":
        _require(code is None)
    elif code is not None:
        _require(
            code
            in {"PERMISSION_DENIED", "PRIVATE_AUTH_FAILED", "INVALID_MESSAGE"}
        )
        frame["code"] = code
    return frame


def status_frame(
    *,
    request_id: str,
    message_id: str,
    from_agent: str,
) -> dict[str, Any]:
    return {
        "v": WIRE_VERSION,
        "type": "status",
        "request_id": _identifier(request_id, field="request_id"),
        "message_id": _identifier(message_id, field="message_id"),
        "from_agent": normalize_public_agent(from_agent),
    }


def delivery_identity_key(delivery: Mapping[str, Any]) -> str:
    sender = ExchangeAddress.from_mapping(delivery["sender"])
    message_id = _identifier(delivery.get("message_id"), field="message_id")
    return "|".join(
        (
            sender.authority_id,
            sender.registered_instance_id,
            sender.agent_id,
            message_id,
        )
    )


def delivery_payload_digest(delivery: Mapping[str, Any]) -> str:
    """Digest immutable message semantics, excluding retry/epoch metadata."""

    normalized = validate_delivery_frame(dict(delivery))
    immutable = {
        key: normalized[key]
        for key in (
            "sender",
            "recipient",
            "message_id",
            "conversation_id",
            "created_at",
            "expires_at",
            "message_type",
            "in_reply_to",
            "content",
        )
    }
    return hashlib.sha256(canonical_json(immutable)).hexdigest()


__all__ = [
    "AUTHORIZED_ROUTES_CAPABILITY",
    "CAPABILITIES",
    "ExchangeProtocolError",
    "MAX_FRAME_BYTES",
    "MESSAGE_KINDS",
    "REMOTE_PROTOCOL_VERSION",
    "SUBPROTOCOL",
    "WIRE_VERSION",
    "ack_frame",
    "canonical_json",
    "decode_server_frame",
    "delivery_identity_key",
    "delivery_payload_digest",
    "hello_frame",
    "parse_timestamp",
    "publish_frame",
    "resolve_frame",
    "routes_frame",
    "send_frame",
    "status_frame",
    "strict_json",
    "utc_timestamp",
    "validate_delivery_deadline",
    "validate_delivery_frame",
]
