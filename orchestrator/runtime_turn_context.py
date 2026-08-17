"""Canonical, enqueue-time context for one user-visible HER turn.

The HER persistent session owns model conversation history, while HASHI owns
transport visibility, delivery order, and runtime model/effort switches.  This
module freezes the latest final turn visible to the user when a request enters
the queue and exposes a bounded, structured envelope to HER.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

FORMAT = "hashi-turn-context-v1"
SECTION_TITLE = "HASHI TURN CONTEXT"
MAX_USER_CHARS = 5_000
MAX_ASSISTANT_CHARS = 8_000
DELIVERY_STATE_VERSION = 1
DELIVERY_STATE_FILENAME = "her_turn_context.json"
MAX_PERSISTED_CHATS = 64

# importlib.reload() reuses the module dictionary. Preserve the lock so a
# minimal reboot cannot split writers between the old and new module objects.
_DELIVERY_STATE_LOCK = globals().get("_DELIVERY_STATE_LOCK") or threading.RLock()

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


def _workspace_dir(runtime: Any) -> Path | None:
    value = getattr(runtime, "workspace_dir", None)
    if value is None:
        value = getattr(getattr(runtime, "config", None), "workspace_dir", None)
    if value is None:
        return None
    try:
        return Path(value)
    except TypeError:
        return None


def delivery_state_path(runtime: Any) -> Path | None:
    workspace = _workspace_dir(runtime)
    if workspace is None:
        return None
    return workspace / "state" / DELIVERY_STATE_FILENAME


def _empty_delivery_state() -> dict[str, Any]:
    return {"version": DELIVERY_STATE_VERSION, "chats": {}}


def _read_delivery_state_unlocked(path: Path) -> dict[str, Any]:
    try:
        if not path.exists():
            return _empty_delivery_state()
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return _empty_delivery_state()
    chats = payload.get("chats") if isinstance(payload, Mapping) else None
    if not isinstance(chats, Mapping):
        return _empty_delivery_state()
    return {
        "version": DELIVERY_STATE_VERSION,
        "chats": {
            str(chat_key): dict(entry)
            for chat_key, entry in chats.items()
            if isinstance(entry, Mapping)
        },
    }


def _warn_persistence(runtime: Any, action: str, exc: Exception) -> None:
    logger = getattr(runtime, "logger", None)
    if logger is not None:
        logger.warning("Could not %s HER delivered-turn context: %s", action, exc)


def _write_delivery_state_unlocked(
    runtime: Any,
    path: Path,
    payload: Mapping[str, Any],
) -> bool:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{time.time_ns()}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        os.replace(temporary, path)
        path.chmod(0o600)
        return True
    except Exception as exc:
        _warn_persistence(runtime, "persist", exc)
        return False
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except Exception:
            pass


def _persist_previous_turn(
    runtime: Any,
    chat_key: str,
    previous: Mapping[str, Any],
) -> bool:
    path = delivery_state_path(runtime)
    if path is None or not chat_key:
        return False
    entry = {
        "request_id": str(previous.get("request_id") or ""),
        "source": str(previous.get("source") or "unknown"),
        "user_text": _bounded_text(previous.get("user_text"), MAX_USER_CHARS),
        "assistant_text": _bounded_text(
            previous.get("assistant_text"), MAX_ASSISTANT_CHARS
        ),
        "model": str(previous.get("model") or ""),
        "effort": str(previous.get("effort") or ""),
        "updated_at": time.time(),
    }
    if not entry["user_text"] or not entry["assistant_text"]:
        return False
    try:
        with _DELIVERY_STATE_LOCK:
            payload = _read_delivery_state_unlocked(path)
            chats = payload["chats"]
            chats.pop(chat_key, None)
            chats[chat_key] = entry
            if len(chats) > MAX_PERSISTED_CHATS:
                newest = list(chats.items())[-MAX_PERSISTED_CHATS:]
                payload["chats"] = dict(newest)
            return _write_delivery_state_unlocked(runtime, path, payload)
    except Exception as exc:
        _warn_persistence(runtime, "persist", exc)
        return False


def _persisted_previous_turn(runtime: Any, chat_key: str) -> dict[str, Any] | None:
    path = delivery_state_path(runtime)
    if path is None or not chat_key:
        return None
    try:
        with _DELIVERY_STATE_LOCK:
            entry = _read_delivery_state_unlocked(path)["chats"].get(chat_key)
    except Exception as exc:
        _warn_persistence(runtime, "read", exc)
        return None
    if not isinstance(entry, Mapping):
        return None
    previous = {
        "request_id": str(entry.get("request_id") or ""),
        "source": str(entry.get("source") or "unknown"),
        "user_text": _bounded_text(entry.get("user_text"), MAX_USER_CHARS),
        "assistant_text": _bounded_text(
            entry.get("assistant_text"), MAX_ASSISTANT_CHARS
        ),
        "model": str(entry.get("model") or ""),
        "effort": str(entry.get("effort") or ""),
    }
    if not previous["user_text"] or not previous["assistant_text"]:
        return None
    return previous


def _bridge_memory_previous_turn(runtime: Any) -> dict[str, Any] | None:
    """One-time compatibility fallback for workspaces created before this state."""

    store = getattr(runtime, "memory_store", None)
    if store is None:
        store = getattr(getattr(runtime, "context_assembler", None), "memory_store", None)
    resolver = getattr(store, "get_recent_turns", None)
    if not callable(resolver):
        return None
    try:
        turns = list(resolver(limit=6) or [])
    except TypeError:
        try:
            turns = list(resolver(6) or [])
        except Exception:
            return None
    except Exception:
        return None

    for assistant_index in range(len(turns) - 1, -1, -1):
        assistant = turns[assistant_index]
        if str(_value(assistant, "role", "")).strip().lower() != "assistant":
            continue
        assistant_text = _bounded_text(
            _value(assistant, "text", ""), MAX_ASSISTANT_CHARS
        )
        if not assistant_text:
            continue
        for user_index in range(assistant_index - 1, -1, -1):
            user = turns[user_index]
            if str(_value(user, "role", "")).strip().lower() != "user":
                continue
            user_text = _bounded_text(_value(user, "text", ""), MAX_USER_CHARS)
            if not user_text:
                continue
            return {
                "request_id": "",
                "source": str(_value(user, "source", "unknown") or "unknown"),
                "user_text": user_text,
                "assistant_text": assistant_text,
                "model": "",
                "effort": "",
            }
    return None


def clear_delivered_turn_context(runtime: Any) -> None:
    """Clear process and durable referent state for explicit fresh-session flows."""

    _state(runtime).clear()
    path = delivery_state_path(runtime)
    if path is None:
        return
    with _DELIVERY_STATE_LOCK:
        try:
            path.unlink(missing_ok=True)
        except Exception as exc:
            _warn_persistence(runtime, "clear", exc)


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
    previous = _cross_session_previous(item)
    previous_source = "cross_session_receipt" if previous is not None else "none"
    if previous is None:
        previous = _state(runtime).get(chat_key)
        if previous is not None:
            previous_source = "process_delivery_state"
    if previous is None:
        previous = _persisted_previous_turn(runtime, chat_key)
        if previous is not None:
            previous_source = "durable_delivery_state"
    if previous is None:
        previous = _bridge_memory_previous_turn(runtime)
        if previous is not None:
            previous_source = "bridge_memory_fallback"
            _persist_previous_turn(runtime, chat_key, previous)
    previous = dict(previous) if isinstance(previous, Mapping) else None
    if previous is not None and previous_source in {
        "durable_delivery_state",
        "bridge_memory_fallback",
    }:
        _state(runtime)[chat_key] = dict(previous)
        logger = getattr(runtime, "logger", None)
        if logger is not None:
            logger.info(
                "Recovered HER previous-turn context at enqueue: request=%s source=%s",
                str(_value(item, "request_id", "") or ""),
                previous_source,
            )
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
        "previous_turn_source": previous_source,
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
    chat_key = str(_value(item, "chat_id", ""))
    _state(runtime)[chat_key] = record
    _persist_previous_turn(runtime, chat_key, record)
    return dict(record)
