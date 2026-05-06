from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass
from typing import Callable


SendHChatCallable = Callable[..., bool]

_TARGET_RE = re.compile(r"^(?:all|@?[A-Za-z0-9_][A-Za-z0-9_-]*)(?:@[A-Za-z0-9_-]+)?$")
_FENCED_JSON_RE = re.compile(r"^\s*```(?:json)?\s*(?P<body>.*?)\s*```\s*$", re.DOTALL | re.IGNORECASE)
_COMMAND_START_RE = re.compile(r"^\s*(?:/[\w./-]+|(?:python|python3|bash|sh)\b)", re.IGNORECASE)


@dataclass(frozen=True)
class HChatDraft:
    target: str
    message: str
    user_report: str | None = None


@dataclass(frozen=True)
class HChatDraftParseError(ValueError):
    user_message: str

    def __str__(self) -> str:
        return self.user_message


@dataclass(frozen=True)
class HChatDeliveryResult:
    success: bool
    target: str
    from_agent: str
    message: str
    attempt_id: str
    retry_count: int
    delivery_status: str
    error: str | None
    latency_ms: float
    user_report: str | None = None


def parse_hchat_draft(raw: str) -> HChatDraft:
    text = (raw or "").strip()
    if not text:
        raise _parse_error('empty draft')
    if _is_raw_command_shaped(text):
        raise _parse_error("draft looks like a shell command")

    payload_text = _extract_fenced_json(text)
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        raise _parse_error(f"invalid JSON: {exc.msg}") from exc

    if not isinstance(payload, dict):
        raise _parse_error("draft must be a JSON object")

    target = _required_string(payload, "target")
    message = _required_string(payload, "message")
    user_report = _optional_string(payload, "user_report")

    validate_hchat_target_format(target)
    if _is_command_shaped(message):
        raise _parse_error("message looks like a shell command")
    if user_report is not None and _is_command_shaped(user_report):
        raise _parse_error("user_report looks like a shell command")

    return HChatDraft(target=target, message=message, user_report=user_report)


def validate_hchat_target_format(target: str) -> str:
    normalized = (target or "").strip()
    if not normalized:
        raise _parse_error('missing required field "target"')
    if any(ch.isspace() for ch in normalized):
        raise _parse_error('invalid "target": whitespace is not allowed')
    if not _TARGET_RE.match(normalized):
        raise _parse_error('invalid "target": expected agent, @group, all, or agent@INSTANCE')
    return normalized


def deliver_hchat_draft(
    draft: HChatDraft,
    *,
    from_agent: str,
    sender: SendHChatCallable | None = None,
    attempt_id: str | None = None,
) -> HChatDeliveryResult:
    from tools.hchat_send import send_hchat

    sender_fn = sender or send_hchat
    clean_from = (from_agent or "").strip()
    if not clean_from:
        raise ValueError("from_agent is required")
    target = validate_hchat_target_format(draft.target)
    message = (draft.message or "").strip()
    if not message:
        raise _parse_error('missing required field "message"')

    logical_attempt_id = attempt_id or str(uuid.uuid4())
    start = time.perf_counter()
    error: str | None = None
    try:
        success = bool(sender_fn(target, clean_from, message))
    except Exception as exc:
        success = False
        error = f"{type(exc).__name__}: {exc}"
    latency_ms = (time.perf_counter() - start) * 1000
    status = "delivered" if success else "failed"

    return HChatDeliveryResult(
        success=success,
        target=target,
        from_agent=clean_from,
        message=message,
        attempt_id=logical_attempt_id,
        retry_count=0,
        delivery_status=status,
        error=error,
        latency_ms=latency_ms,
        user_report=draft.user_report,
    )


def draft_parse_error_text(exc: HChatDraftParseError) -> str:
    return f"[hchat] Draft parse error: {exc.user_message}. Message not sent."


def hchat_draft_parsed_log_fields(draft: HChatDraft) -> dict[str, object]:
    return {
        "hchat_draft_parsed": {
            "target": draft.target,
            "message": draft.message,
            "user_report": draft.user_report,
        }
    }


def hchat_delivery_log_fields(result: HChatDeliveryResult) -> dict[str, object]:
    return {
        "hchat_target": result.target,
        "hchat_delivery_attempt_id": result.attempt_id,
        "hchat_payload_final": result.message,
        "hchat_delivery_status": result.delivery_status,
        "hchat_delivery_error": result.error,
        "hchat_delivery_latency_ms": result.latency_ms,
        "hchat_retry_count": result.retry_count,
    }


def _required_string(payload: dict[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise _parse_error(f'missing required field "{field}"')
    return value.strip()


def _optional_string(payload: dict[str, object], field: str) -> str | None:
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise _parse_error(f'field "{field}" must be a string')
    return value.strip() or None


def _extract_fenced_json(text: str) -> str:
    match = _FENCED_JSON_RE.match(text)
    return match.group("body") if match else text


def _is_command_shaped(text: str) -> bool:
    stripped = (text or "").strip()
    if not stripped:
        return False
    lowered = stripped.lower()
    if "hchat_send.py" in lowered:
        return True
    return bool(_COMMAND_START_RE.match(stripped) and (" --to " in lowered or " --text " in lowered))


def _is_raw_command_shaped(text: str) -> bool:
    stripped = (text or "").strip()
    if not stripped or stripped.startswith("{") or stripped.startswith("[") or stripped.startswith("```"):
        return False
    return _is_command_shaped(stripped)


def _parse_error(message: str) -> HChatDraftParseError:
    return HChatDraftParseError(message)


__all__ = [
    "HChatDeliveryResult",
    "HChatDraft",
    "HChatDraftParseError",
    "deliver_hchat_draft",
    "draft_parse_error_text",
    "hchat_delivery_log_fields",
    "hchat_draft_parsed_log_fields",
    "parse_hchat_draft",
    "validate_hchat_target_format",
]
