from __future__ import annotations

import copy
import json
import os
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from orchestrator.multimodal_contract import (
    attachment_manifest,
    normalize_request_content,
)


RETRY_STATE_VERSION = 2
RETRY_STATE_FILENAME = "retry_state.json"
RETRY_HANDOFF_SOURCE = "retry-handoff"

_DIRECT_CONTINUATION_SOURCES = frozenset({"text", "voice", "telegram"})
_ENGLISH_CONTINUATION_RE = re.compile(
    r"(?:please\s+)?(?:you\s+can\s+)?"
    r"(?:continue|resume|carry\s+on|go\s+on)"
    r"(?:\s+(?:now|please|the\s+(?:task|work)|working|with\s+it|"
    r"from\s+where\s+(?:you|we)\s+left\s+off|"
    r"where\s+(?:you|we)\s+left\s+off|what\s+you\s+were\s+doing))?",
    re.IGNORECASE,
)
_ENGLISH_PICK_UP_RE = re.compile(
    r"(?:please\s+)?pick\s+(?:it\s+)?up\s+where\s+(?:you|we)\s+left\s+off",
    re.IGNORECASE,
)
_CHINESE_CONTINUATION_RE = re.compile(
    r"(?:请|您可以|可以)?(?:继续|接着)"
    r"(?:吧|了|做|进行|完成|工作|这个|它|刚才(?:的)?(?:任务|工作)?|"
    r"之前(?:的)?(?:任务|工作)?|上次(?:的)?(?:任务|工作)?)?"
)
_CHINESE_FROM_STOP_RE = re.compile(
    r"(?:请)?从(?:刚才|之前|上次)(?:停下|中断)(?:的)?地方(?:继续|接着)"
)

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
    request_content: dict[str, Any] | None = None


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


@dataclass(frozen=True)
class InterruptedTaskSnapshot:
    prompt: str
    chat_id: int | str | None
    source: str
    summary: str
    request_id: str | None = None
    backend: str = ""
    reason: str = "user_stop"
    interrupted_at: float = 0.0
    request_content: dict[str, Any] | None = None


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


def _delete_state_key(runtime: Any, key: str) -> None:
    path = retry_state_path(runtime)
    if path is None:
        return
    try:
        state = _read_state(runtime)
        if key not in state:
            return
        state.pop(key, None)
        state["version"] = RETRY_STATE_VERSION
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
            logger.warning("Could not clear retry state: %s", exc)


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
    raw_request_content = _value(candidate, "request_content")
    request_content = (
        normalize_request_content(raw_request_content)
        if raw_request_content is not None
        else None
    )
    return RetryPromptSnapshot(
        prompt=prompt,
        chat_id=chat_id,
        source=source,
        summary=summary,
        request_id=str(request_id) if request_id else None,
        recorded_at=recorded_at,
        request_content=copy.deepcopy(request_content),
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
        request_content=copy.deepcopy(snapshot.request_content),
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


def _interrupted_task_snapshot(candidate: Any) -> InterruptedTaskSnapshot | None:
    if candidate is None:
        return None
    prompt = str(_value(candidate, "prompt", "") or "")
    source = str(_value(candidate, "source", "text") or "text").strip()
    if not prompt.strip() or not _is_retryable_source(source):
        return None
    summary = str(_value(candidate, "summary", "") or "").strip()
    if not summary:
        summary = " ".join(prompt.split())[:160] or "Interrupted task"
    request_id = _value(candidate, "request_id")
    interrupted_at = _value(candidate, "interrupted_at", 0.0)
    try:
        interrupted_at = float(interrupted_at or 0.0)
    except (TypeError, ValueError):
        interrupted_at = 0.0
    raw_request_content = _value(candidate, "request_content")
    request_content = (
        normalize_request_content(raw_request_content)
        if raw_request_content is not None
        else None
    )
    return InterruptedTaskSnapshot(
        prompt=prompt,
        chat_id=_value(candidate, "chat_id"),
        source=source,
        summary=summary,
        request_id=str(request_id) if request_id else None,
        backend=str(_value(candidate, "backend", "") or ""),
        reason=str(_value(candidate, "reason", "user_stop") or "user_stop"),
        interrupted_at=interrupted_at,
        request_content=copy.deepcopy(request_content),
    )


def remember_interrupted_task(
    runtime: Any,
    candidate: Any = None,
    *,
    backend: str = "",
    reason: str = "user_stop",
) -> InterruptedTaskSnapshot | None:
    """Persist the original task killed by /stop, including across restarts."""
    candidate = candidate if candidate is not None else getattr(runtime, "current_request_meta", None)
    resumed = _value(candidate, "resumed_interrupted_task")
    snapshot = _interrupted_task_snapshot(resumed) or _interrupted_task_snapshot(candidate)
    if snapshot is None:
        return None
    snapshot = InterruptedTaskSnapshot(
        prompt=snapshot.prompt,
        chat_id=snapshot.chat_id,
        source=snapshot.source,
        summary=snapshot.summary,
        request_id=snapshot.request_id,
        backend=snapshot.backend or str(backend or ""),
        reason=str(reason or "user_stop"),
        interrupted_at=time.time(),
        request_content=copy.deepcopy(snapshot.request_content),
    )
    runtime._interrupted_task = snapshot
    _write_state(runtime, "unfinished_task", asdict(snapshot))
    return snapshot


def capture_interrupted_task(runtime: Any) -> InterruptedTaskSnapshot | None:
    for candidate in (
        getattr(runtime, "_interrupted_task", None),
        _read_state(runtime).get("unfinished_task"),
    ):
        snapshot = _interrupted_task_snapshot(candidate)
        if snapshot is not None:
            return snapshot
    return None


def is_explicit_continuation(prompt: str) -> bool:
    """Recognize a short referent-free request to resume the stopped task."""
    text = str(prompt or "").strip()
    if not text or len(text) > 240 or text.startswith("/"):
        return False
    text = text.strip(" \t\r\n.?!。！？~～")
    return any(
        pattern.fullmatch(text) is not None
        for pattern in (
            _ENGLISH_CONTINUATION_RE,
            _ENGLISH_PICK_UP_RE,
            _CHINESE_CONTINUATION_RE,
            _CHINESE_FROM_STOP_RE,
        )
    )


def build_interrupted_task_continuation(
    snapshot: InterruptedTaskSnapshot,
    continuation_prompt: str,
    *,
    backend: str = "",
) -> str:
    original = snapshot.prompt.strip()
    if len(original) > 20000:
        original = original[:20000] + "\n…[original task truncated]"
    backend_name = str(backend or snapshot.backend or "").strip()
    backend_note = f"\nActive backend/engine now: {backend_name}" if backend_name else ""
    return (
        "[HASHI /stop continuation — resume preserved unfinished task]\n"
        "The user explicitly asked to continue the task interrupted by /stop. "
        "This is not a new blank task and not a status-only request.\n"
        "Requirements:\n"
        "1. Continue the original requested outcome and scope below.\n"
        "2. Resume from existing session state, workspace files, artefacts, tool results, "
        "and partial progress; do not restart completed work without need.\n"
        "3. Take the next concrete action and keep working until the original task is "
        "complete or genuinely blocked.\n"
        "4. Treat the current message only as permission to resume unless it contains an "
        "explicit additional direction."
        f"{backend_note}\n\n"
        "Current continuation message:\n"
        f"{str(continuation_prompt or '').strip()}\n\n"
        "--- Original unfinished user task (authoritative) ---\n"
        f"{original}\n"
        "--- End original unfinished user task ---"
    )


def prepare_interrupted_task_continuation(
    runtime: Any,
    item: Any,
    effective_prompt: str,
    *,
    backend: str = "",
) -> str:
    """Bind a bare 'continue' turn to the durable /stop snapshot when present."""
    source = str(_value(item, "source", "") or "").strip().lower()
    if (
        bool(_value(item, "silent", False))
        or source not in _DIRECT_CONTINUATION_SOURCES
        or not is_explicit_continuation(str(_value(item, "prompt", "") or ""))
    ):
        return effective_prompt
    snapshot = capture_interrupted_task(runtime)
    if snapshot is None:
        return effective_prompt
    from orchestrator.fresh_context import entry_is_after_boundary

    if not entry_is_after_boundary(runtime, snapshot.interrupted_at):
        return effective_prompt
    metadata = asdict(snapshot)
    current_meta = getattr(runtime, "current_request_meta", None)
    if isinstance(current_meta, dict):
        current_meta["resumed_interrupted_task"] = metadata
    try:
        setattr(item, "_resumed_interrupted_task", metadata)
        if snapshot.request_content is not None:
            item.request_content = copy.deepcopy(snapshot.request_content)
            item.attachment_manifest = attachment_manifest(item.request_content)
            if isinstance(current_meta, dict):
                current_meta["request_content"] = copy.deepcopy(item.request_content)
                current_meta["attachment_manifest"] = [
                    copy.deepcopy(entry) for entry in item.attachment_manifest
                ]
    except Exception:
        pass
    logger = getattr(runtime, "logger", None)
    log_info = getattr(logger, "info", None)
    if callable(log_info):
        log_info(
            f"Bound continuation request {_value(item, 'request_id', '')} "
            f"to interrupted task {snapshot.request_id or 'unknown'}"
        )
    return build_interrupted_task_continuation(
        snapshot,
        effective_prompt,
        backend=backend,
    )


def clear_completed_interrupted_task(runtime: Any, item: Any) -> bool:
    """Clear only the /stop snapshot explicitly attached to this successful turn."""
    resumed = _value(item, "_resumed_interrupted_task")
    if _interrupted_task_snapshot(resumed) is None:
        current_meta = getattr(runtime, "current_request_meta", None)
        resumed = _value(current_meta, "resumed_interrupted_task")
    if _interrupted_task_snapshot(resumed) is None:
        return False
    runtime._interrupted_task = None
    _delete_state_key(runtime, "unfinished_task")
    return True


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
