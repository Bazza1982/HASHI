from __future__ import annotations
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

PROTOCOL_NAME = "bridge-agent-v1"
REQUEST_KINDS = {"request"}
REPLY_KINDS = {"reply"}
ALL_KINDS = REQUEST_KINDS | REPLY_KINDS
ALLOWED_INTENTS = {"ask", "notify"}
ALLOWED_REPLY_STATUS = {"ok", "partial", "failed", "refused"}
INTENT_SCOPE = {
    "ask": "conversation",
    "notify": "conversation",
}


def generate_message_id() -> str:
    return f"msg-{uuid.uuid4().hex}"


def generate_thread_id() -> str:
    return f"thr-{uuid.uuid4().hex}"


def required_scope_for_intent(intent: str) -> str:
    if intent not in INTENT_SCOPE:
        raise ValueError(f"unsupported intent: {intent}")
    return INTENT_SCOPE[intent]


@dataclass(slots=True)
class BridgeMessage:
    from_agent: str
    to_agent: str
    text: str
    intent: str = "ask"
    reply_required: bool = True
    thread_id: str | None = None
    message_id: str = field(default_factory=generate_message_id)
    protocol: str = PROTOCOL_NAME
    kind: str = "request"
    permission_scope: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if not self.permission_scope:
            data["permission_scope"] = required_scope_for_intent(self.intent)
        if not self.thread_id:
            data["thread_id"] = generate_thread_id()
        return data


def validate_request_payload(payload: dict[str, Any]) -> dict[str, Any]:
    protocol = (payload.get("protocol") or PROTOCOL_NAME).strip()
    if protocol != PROTOCOL_NAME:
        raise ValueError(f"unsupported protocol: {protocol}")

    kind = (payload.get("kind") or "request").strip().lower()
    if kind not in REQUEST_KINDS:
        raise ValueError(f"unsupported request kind: {kind}")

    intent = (payload.get("intent") or "ask").strip().lower()
    if intent not in ALLOWED_INTENTS:
        raise ValueError(f"unsupported intent: {intent}")

    text = str(payload.get("text") or "").strip()
    if not text:
        raise ValueError("text is required")

    from_agent = str(payload.get("from_agent") or "").strip()
    to_agent = str(payload.get("to_agent") or "").strip()
    if not from_agent:
        raise ValueError("from_agent is required")
    if not to_agent:
        raise ValueError("to_agent is required")

    permission_scope = str(payload.get("permission_scope") or required_scope_for_intent(intent)).strip()
    if permission_scope != required_scope_for_intent(intent):
        raise ValueError(
            f"permission_scope {permission_scope!r} does not match required scope "
            f"{required_scope_for_intent(intent)!r} for intent {intent!r}"
        )

    meta = payload.get("meta") or {}
    if not isinstance(meta, dict):
        raise ValueError("meta must be an object")

    return {
        "protocol": protocol,
        "message_id": str(payload.get("message_id") or generate_message_id()).strip(),
        "thread_id": str(payload.get("thread_id") or generate_thread_id()).strip(),
        "kind": kind,
        "intent": intent,
        "permission_scope": permission_scope,
        "from_agent": from_agent,
        "to_agent": to_agent,
        "reply_required": bool(payload.get("reply_required", intent == "ask")),
        "text": text,
        "meta": meta,
    }


def validate_reply_payload(payload: dict[str, Any]) -> dict[str, Any]:
    protocol = (payload.get("protocol") or PROTOCOL_NAME).strip()
    if protocol != PROTOCOL_NAME:
        raise ValueError(f"unsupported protocol: {protocol}")

    kind = (payload.get("kind") or "reply").strip().lower()
    if kind not in REPLY_KINDS:
        raise ValueError(f"unsupported reply kind: {kind}")

    from_agent = str(payload.get("from_agent") or "").strip()
    to_agent = str(payload.get("to_agent") or "").strip()
    thread_id = str(payload.get("thread_id") or "").strip()
    in_reply_to = str(payload.get("in_reply_to") or "").strip()
    if not from_agent:
        raise ValueError("from_agent is required")
    if not to_agent:
        raise ValueError("to_agent is required")
    if not thread_id:
        raise ValueError("thread_id is required")
    if not in_reply_to:
        raise ValueError("in_reply_to is required")

    status = str(payload.get("status") or "ok").strip().lower()
    if status not in ALLOWED_REPLY_STATUS:
        raise ValueError(f"unsupported reply status: {status}")

    result_text = payload.get("result_text")
    error_text = payload.get("error_text")
    if not (str(result_text or "").strip() or str(error_text or "").strip()):
        raise ValueError("reply requires result_text or error_text")

    return {
        "protocol": protocol,
        "message_id": str(payload.get("message_id") or generate_message_id()).strip(),
        "thread_id": thread_id,
        "in_reply_to": in_reply_to,
        "kind": kind,
        "from_agent": from_agent,
        "to_agent": to_agent,
        "status": status,
        "result_text": str(result_text or "").strip() or None,
        "error_text": str(error_text or "").strip() or None,
        "meta": payload.get("meta") if isinstance(payload.get("meta"), dict) else {},
    }


def build_result_reply(request_message: dict[str, Any], success: bool, text: str | None, error: str | None) -> dict[str, Any]:
    return {
        "protocol": PROTOCOL_NAME,
        "message_id": generate_message_id(),
        "thread_id": request_message["thread_id"],
        "in_reply_to": request_message["message_id"],
        "kind": "reply",
        "from_agent": request_message["to_agent"],
        "to_agent": request_message["from_agent"],
        "status": "ok" if success else "failed",
        "result_text": text if success else None,
        "error_text": error if not success else None,
        "meta": {},
    }
