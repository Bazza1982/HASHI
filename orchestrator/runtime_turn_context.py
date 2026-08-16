"""Canonical, enqueue-time context for one user-visible HER turn.

The HER persistent session owns model conversation history, while HASHI owns
transport visibility, delivery order, and runtime model/effort switches.  This
module freezes the latest final turn visible to the user when a request enters
the queue and exposes a bounded, structured envelope to HER.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

FORMAT = "hashi-turn-context-v1"
SECTION_TITLE = "HASHI TURN CONTEXT"
MAX_USER_CHARS = 5_000
MAX_ASSISTANT_CHARS = 8_000

_DIRECT_SOURCES = frozenset(
    {
        "audio",
        "document",
        "multimodal",
        "photo",
        "sticker",
        "telegram",
        "text",
        "video",
        "voice",
        "voice_transcript",
    }
)


def _value(candidate: Any, key: str, default: Any = None) -> Any:
    if isinstance(candidate, Mapping):
        return candidate.get(key, default)
    return getattr(candidate, key, default)


def _bounded_text(value: Any, limit: int) -> str:
    text = str(value or "").replace("\x00", "").strip()
    if len(text) <= limit:
        return text
    marker = "\n…[HASHI turn context truncated]…\n"
    head = max(1, (limit - len(marker)) // 2)
    tail = max(1, limit - len(marker) - head)
    return text[:head].rstrip() + marker + text[-tail:].lstrip()


def _backend(runtime: Any) -> Any:
    return getattr(getattr(runtime, "backend_manager", None), "current_backend", None)


def _call_text(resolver: Any) -> str:
    if not callable(resolver):
        return ""
    try:
        return str(resolver() or "").strip()
    except Exception:  # noqa: BLE001 - optional backend compatibility probe
        return ""


def _current_model(runtime: Any) -> str:
    backend = _backend(runtime)
    model = _call_text(getattr(backend, "_claw_model", None))
    if model:
        return model
    model = _call_text(getattr(runtime, "get_current_model", None))
    if model:
        return model
    return str(getattr(backend, "model", "") or "").strip()


def _current_effort(runtime: Any) -> str:
    backend = _backend(runtime)
    return str(getattr(backend, "effort", "") or "").strip().lower()


def _permission_mode(runtime: Any) -> str:
    backend = _backend(runtime)
    mode = _call_text(getattr(backend, "_permission_mode", None))
    if mode:
        return mode
    return str(getattr(backend, "permission_mode", "") or "").strip()


def _is_her(runtime: Any) -> bool:
    return (
        str(getattr(getattr(runtime, "config", None), "active_backend", "") or "")
        .strip()
        .lower()
        == "her"
    )


def _state(runtime: Any) -> dict[str, dict[str, Any]]:
    state = getattr(runtime, "_last_delivered_turn_by_chat", None)
    if not isinstance(state, dict):
        state = {}
        runtime._last_delivered_turn_by_chat = state
    return state


def _has_earlier_pending_direct_turn(runtime: Any, item: Any) -> bool:
    ordering = getattr(runtime, "_direct_delivery_order", None)
    requests = getattr(ordering, "requests", None)
    chats = getattr(ordering, "chats", None)
    if not isinstance(requests, dict) or not isinstance(chats, dict):
        return False
    current = requests.get(str(_value(item, "request_id", "") or ""))
    if current is None:
        return False
    chat = chats.get(str(_value(item, "chat_id", "")))
    turns = getattr(chat, "turns", None)
    current_sequence = getattr(current, "sequence", None)
    if not isinstance(turns, dict) or not isinstance(current_sequence, int):
        return False
    return any(
        isinstance(sequence, int) and sequence < current_sequence for sequence in turns
    )


def _cross_session_previous(item: Any) -> dict[str, Any] | None:
    receipt = getattr(item, "_cross_session_receipt_snapshot", None)
    if not isinstance(receipt, Mapping):
        return None
    user_text = _bounded_text(
        receipt.get("last_user_text") or receipt.get("task_prompt"), MAX_USER_CHARS
    )
    assistant_text = _bounded_text(receipt.get("assistant_text"), MAX_ASSISTANT_CHARS)
    if not assistant_text:
        return None
    return {
        "request_id": str(receipt.get("request_id") or ""),
        "source": str(receipt.get("source") or "unknown"),
        "user_text": user_text,
        "assistant_text": assistant_text,
        "model": str(receipt.get("model") or ""),
        "effort": str(receipt.get("effort") or ""),
    }


def capture_at_enqueue(runtime: Any, item: Any) -> dict[str, Any] | None:
    """Freeze the user-visible referent before later deliveries can replace it."""

    existing = getattr(item, "_hashi_turn_context", None)
    if isinstance(existing, Mapping):
        return dict(existing)
    if not _is_her(runtime):
        return None

    chat_key = str(_value(item, "chat_id", ""))
    previous = _cross_session_previous(item) or _state(runtime).get(chat_key)
    previous = dict(previous) if isinstance(previous, Mapping) else None
    model = _current_model(runtime)
    effort = _current_effort(runtime)
    current = {
        "request_id": str(_value(item, "request_id", "") or ""),
        "source": str(_value(item, "source", "") or ""),
        "model": model,
        "effort": effort,
        "permission_mode": _permission_mode(runtime),
    }
    payload: dict[str, Any] = {
        "format": FORMAT,
        "captured_at_enqueue": True,
        "previous_turn_status": (
            "captured"
            if previous is not None
            else (
                "captured_no_prior_final"
                if _has_earlier_pending_direct_turn(runtime, item)
                else "unavailable"
            )
        ),
        "current": current,
        "reply_target": {"kind": "none", "request_id": ""},
        "previous_turn": None,
        "transition": {
            "model_changed": False,
            "effort_changed": False,
            "previous_model": "",
            "previous_effort": "",
        },
    }
    if previous is not None:
        previous_model = str(previous.get("model") or "")
        previous_effort = str(previous.get("effort") or "")
        cross_session = isinstance(
            getattr(item, "_cross_session_receipt_snapshot", None), Mapping
        )
        payload["reply_target"] = {
            "kind": (
                "captured_cross_session_receipt"
                if cross_session
                else "latest_delivered_final"
            ),
            "request_id": str(previous.get("request_id") or ""),
        }
        payload["previous_turn"] = {
            "request_id": str(previous.get("request_id") or ""),
            "source": str(previous.get("source") or "unknown"),
            "user_text": _bounded_text(previous.get("user_text"), MAX_USER_CHARS),
            "assistant_text": _bounded_text(
                previous.get("assistant_text"), MAX_ASSISTANT_CHARS
            ),
            "model": previous_model,
            "effort": previous_effort,
        }
        payload["transition"] = {
            "model_changed": bool(previous_model and model and previous_model != model),
            "effort_changed": bool(
                previous_effort and effort and previous_effort != effort
            ),
            "previous_model": previous_model,
            "previous_effort": previous_effort,
        }
    item._hashi_turn_context = payload
    return dict(payload)


def context_section(runtime: Any, item: Any) -> list[tuple[str, str]]:
    """Return the structured envelope consumed by the native HER controller."""

    source = str(_value(item, "source", "") or "").strip().lower()
    if source not in _DIRECT_SOURCES or bool(_value(item, "silent", False)):
        return []
    payload = capture_at_enqueue(runtime, item)
    if payload is None:
        return []
    return [
        (
            SECTION_TITLE,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        )
    ]


def record_delivered_turn(
    runtime: Any,
    item: Any,
    assistant_text: str,
) -> dict[str, Any] | None:
    """Remember one final message only after HASHI confirms user delivery."""

    text = _bounded_text(assistant_text, MAX_ASSISTANT_CHARS)
    if not _is_her(runtime) or not text:
        return None
    captured = capture_at_enqueue(runtime, item) or {}
    current = captured.get("current") if isinstance(captured, Mapping) else None
    current = current if isinstance(current, Mapping) else {}
    record = {
        "request_id": str(_value(item, "request_id", "") or ""),
        "source": str(_value(item, "source", "") or ""),
        "user_text": _bounded_text(_value(item, "prompt", ""), MAX_USER_CHARS),
        "assistant_text": text,
        "model": str(current.get("model") or _current_model(runtime)),
        "effort": str(current.get("effort") or _current_effort(runtime)),
    }
    _state(runtime)[str(_value(item, "chat_id", ""))] = record
    return dict(record)
