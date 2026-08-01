from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


RETRY_STATE_VERSION = 1
RETRY_STATE_FILENAME = "retry_state.json"
RETRY_HANDOFF_SOURCE = "retry-handoff"

_NON_RETRYABLE_PROMPT_SOURCES = frozenset(
    {
        "startup",
        "system",
        "handoff",
        RETRY_HANDOFF_SOURCE,
        "session_reset",
    }
)


@dataclass(frozen=True)
class RetryPromptSnapshot:
    prompt: str
    chat_id: int | str | None
    source: str
    summary: str
    request_id: str | None = None
    recorded_at: float = 0.0


@dataclass(frozen=True)
class ResendOutputSnapshot:
    text: str
    chat_id: int | str | None
    source: str
    request_id: str | None = None
    recorded_at: float = 0.0


@dataclass(frozen=True)
class RetryHandoffSnapshot:
    prompt: str
    exchange_count: int
    word_count: int


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


def retry_state_path(runtime: Any) -> Path | None:
    workspace_dir = _workspace_dir(runtime)
    if workspace_dir is None:
        return None
    return workspace_dir / "state" / RETRY_STATE_FILENAME


def _read_state(runtime: Any) -> dict[str, Any]:
    path = retry_state_path(runtime)
    if path is None or not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_state(runtime: Any, key: str, payload: dict[str, Any]) -> None:
    path = retry_state_path(runtime)
    if path is None:
        return
    try:
        state = _read_state(runtime)
        state["version"] = RETRY_STATE_VERSION
        state[key] = payload
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(
            f".{path.name}.tmp-{os.getpid()}-{time.time_ns()}"
        )
        try:
            temp_path.write_text(
                json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temp_path.replace(path)
        finally:
            temp_path.unlink(missing_ok=True)
    except Exception as exc:
        logger = getattr(runtime, "logger", None)
        if logger is not None:
            logger.warning("Could not persist retry state: %s", exc)


def _value(candidate: Any, key: str, default: Any = None) -> Any:
    if isinstance(candidate, dict):
        return candidate.get(key, default)
    return getattr(candidate, key, default)


def _is_retryable_source(source: str | None) -> bool:
    normalized = str(source or "text").strip().lower()
    return normalized not in _NON_RETRYABLE_PROMPT_SOURCES


def _prompt_snapshot(
    candidate: Any,
    *,
    fallback_chat_id: int | str | None = None,
) -> RetryPromptSnapshot | None:
    if candidate is None:
        return None
    prompt = str(_value(candidate, "prompt", "") or "")
    source = str(_value(candidate, "source", "text") or "text").strip()
    if not prompt.strip() or not _is_retryable_source(source):
        return None
    chat_id = _value(candidate, "chat_id", fallback_chat_id)
    summary = str(_value(candidate, "summary", "") or "").strip()
    if not summary:
        summary = " ".join(prompt.split())[:160] or "Retry request"
    request_id = _value(candidate, "request_id")
    recorded_at = _value(candidate, "recorded_at", 0.0)
    try:
        recorded_at = float(recorded_at or 0.0)
    except (TypeError, ValueError):
        recorded_at = 0.0
    return RetryPromptSnapshot(
        prompt=prompt,
        chat_id=chat_id,
        source=source,
        summary=summary,
        request_id=str(request_id) if request_id else None,
        recorded_at=recorded_at,
    )


def remember_retryable_prompt(runtime: Any, item: Any) -> RetryPromptSnapshot | None:
    if bool(_value(item, "silent", False)):
        return None
    snapshot = _prompt_snapshot(item)
    if snapshot is None:
        return None
    snapshot = RetryPromptSnapshot(
        prompt=snapshot.prompt,
        chat_id=snapshot.chat_id,
        source=snapshot.source,
        summary=snapshot.summary,
        request_id=snapshot.request_id,
        recorded_at=time.time(),
    )
    runtime._last_retryable_prompt = snapshot
    _write_state(runtime, "last_prompt", asdict(snapshot))
    return snapshot


def _last_user_prompt_from_transcript(
    runtime: Any,
    *,
    fallback_chat_id: int | str | None,
) -> RetryPromptSnapshot | None:
    path = getattr(runtime, "transcript_log_path", None)
    if path is None:
        return None
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except Exception:
        return None
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except Exception:
            continue
        if entry.get("role") != "user":
            continue
        source = str(entry.get("source") or "text")
        if not _is_retryable_source(source):
            continue
        prompt = str(entry.get("text") or "")
        if not prompt.strip():
            continue
        return RetryPromptSnapshot(
            prompt=prompt,
            chat_id=fallback_chat_id,
            source=source,
            summary=" ".join(prompt.split())[:160] or "Retry request",
            recorded_at=0.0,
        )
    return None


def capture_retryable_prompt(
    runtime: Any,
    *,
    fallback_chat_id: int | str | None = None,
) -> RetryPromptSnapshot | None:
    candidates = (
        getattr(runtime, "current_request_meta", None),
        getattr(runtime, "_last_retryable_prompt", None),
        getattr(runtime, "last_prompt", None),
        _read_state(runtime).get("last_prompt"),
    )
    for candidate in candidates:
        snapshot = _prompt_snapshot(candidate, fallback_chat_id=fallback_chat_id)
        if snapshot is not None:
            return snapshot
    return _last_user_prompt_from_transcript(
        runtime,
        fallback_chat_id=fallback_chat_id,
    )


def remember_output(
    runtime: Any,
    item: Any,
    text: str,
) -> ResendOutputSnapshot | None:
    visible_text = str(text or "")
    source = str(_value(item, "source", "") or "")
    if not visible_text.strip() or source == RETRY_HANDOFF_SOURCE:
        return None
    chat_id = _value(item, "chat_id")
    request_id = _value(item, "request_id")
    recorded_at = time.time()
    snapshot = ResendOutputSnapshot(
        text=visible_text,
        chat_id=chat_id,
        source=source,
        request_id=str(request_id) if request_id else None,
        recorded_at=recorded_at,
    )
    runtime.last_response = {
        "chat_id": chat_id,
        "text": visible_text,
        "source": source,
        "request_id": snapshot.request_id,
        "responded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    _write_state(runtime, "last_output", asdict(snapshot))
    return snapshot


def _output_snapshot(candidate: Any) -> ResendOutputSnapshot | None:
    if candidate is None:
        return None
    text = str(_value(candidate, "text", "") or "")
    source = str(_value(candidate, "source", "") or "")
    if not text.strip() or source == RETRY_HANDOFF_SOURCE:
        return None
    request_id = _value(candidate, "request_id")
    recorded_at = _value(candidate, "recorded_at", 0.0)
    try:
        recorded_at = float(recorded_at or 0.0)
    except (TypeError, ValueError):
        recorded_at = 0.0
    return ResendOutputSnapshot(
        text=text,
        chat_id=_value(candidate, "chat_id"),
        source=source,
        request_id=str(request_id) if request_id else None,
        recorded_at=recorded_at,
    )


def _last_output_from_log(path: Any) -> ResendOutputSnapshot | None:
    if path is None:
        return None
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except Exception:
        return None
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except Exception:
            continue
        role = str(entry.get("role") or "")
        if role not in {"assistant", "assistant_core"}:
            continue
        source = str(entry.get("source") or "")
        if source == RETRY_HANDOFF_SOURCE:
            continue
        text = str(entry.get("visible_text") or entry.get("text") or "")
        if not text.strip():
            continue
        return ResendOutputSnapshot(
            text=text,
            chat_id=None,
            source=source,
            request_id=str(entry.get("request_id") or "") or None,
            recorded_at=0.0,
        )
    return None


def capture_resend_output(runtime: Any) -> ResendOutputSnapshot | None:
    for candidate in (
        getattr(runtime, "last_response", None),
        _read_state(runtime).get("last_output"),
    ):
        snapshot = _output_snapshot(candidate)
        if snapshot is not None:
            return snapshot
    for path in (
        getattr(runtime, "core_transcript_log_path", None),
        getattr(runtime, "transcript_log_path", None),
    ):
        snapshot = _last_output_from_log(path)
        if snapshot is not None:
            return snapshot
    return None


def build_retry_handoff(runtime: Any) -> RetryHandoffSnapshot | None:
    builder = getattr(runtime, "handoff_builder", None)
    if builder is None:
        return None
    try:
        builder.refresh_recent_context()
        builder.build_handoff()
        prompt, exchange_count, word_count = builder.build_session_restore_prompt(
            max_rounds=10,
            max_words=6000,
        )
    except Exception as exc:
        logger = getattr(runtime, "logger", None)
        if logger is not None:
            logger.warning("Could not prepare retry handoff: %s", exc)
        return None
    if exchange_count <= 0 or not str(prompt or "").strip():
        return None
    return RetryHandoffSnapshot(
        prompt=str(prompt),
        exchange_count=int(exchange_count),
        word_count=int(word_count),
    )
