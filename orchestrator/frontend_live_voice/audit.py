"""Privacy-bounded, append-only lifecycle evidence for one logical Live Phone call.

The canonical transcript remains in the Session store.  This audit deliberately
uses a separate JSONL file so a Session-event persistence failure cannot erase
the evidence needed to explain why a call stopped.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import threading
import time
import traceback
from typing import Any, Mapping
from uuid import uuid4

from .protocol import CallBinding

logger = logging.getLogger(__name__)

AUDIT_SCHEMA = "hashi.live_voice.audit.v1"
_EVENT = re.compile(r"[a-z][a-z0-9_.-]{0,127}\Z")
_SAFE_FILE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}\Z")
_MAX_RECORD_BYTES = 16_384
_MAX_STRING = 512
_DURABLE_EVENTS = frozenset({
    "provider.event_persistence_interrupted", "provider.event_persistence_recovered",
    "provider.event_persistence_rejected", "provider.event_processing_failed",
    "provider.event_staging_failed", "provider.event_staging_interrupted",
    "provider.event_staging_rejected",
    "provider.socket_operation_failed",
    "provider.session_closed", "call.transcript_projection_deferred",
    "runtime.recovery_detected", "sideband.cancelled", "sideband.disconnected",
    "sideband.exception", "termination.requested",
})
_ALLOWED_DETAIL_KEYS = frozenset({
    "acknowledged", "action", "attempt", "attempt_id", "attempts", "call_epoch",
    "chunk_count", "chunk_index", "cleanup_state", "client_event_id",
    "client_sequence", "close_sent", "consecutive_failures",
    "action_id", "delegation_id", "detail_truncated", "duration_ms", "end_ms", "error_code",
    "exception_frames", "exception_message", "exception_type", "expires_at",
    "failed_attempts", "history_omitted_units", "initiator", "input_messages",
    "input_tokens", "item_type", "maximum_at", "observed_at", "online",
    "operation", "outcome", "phase", "previous_phase", "provider_close_state",
    "provider_event_id", "provider_event_type", "provider_reason", "reason",
    "request_id", "retry_delay_s", "run_id", "sequence", "source",
    "source_event_id", "source_message_id", "source_session_id", "speaker",
    "start_ms", "state", "summary_code", "task_name", "text_bytes", "usage",
    "visible", "ws_close_code", "ws_message_type",
    "opening_id", "request_sent", "request_accepted", "output_observed",
    "output_evidence", "playback_observed",
})
_SECRET_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~-]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]+\b"),
    re.compile(r"\blive_[A-Za-z0-9_-]+\b"),
    re.compile(r"(?i)(api[_-]?key\s*[=:]\s*)[^\s,;]+"),
)
_PUBLIC_ERROR_CODES = frozenset({
    "live_outcome_unknown", "live_admission_scope_changed", "live_scope_changed",
    "live_admission_rejected", "live_agent_unavailable", "live_admission_unavailable",
})


def _redact(value: str) -> str:
    text = value.replace("\r", " ").replace("\n", " ")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda match: f"{match.group(1)}[REDACTED]" if match.lastindex else "[REDACTED]", text)
    return text[:_MAX_STRING]


def _safe_value(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return max(-(2**63), min(2**63 - 1, value))
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else None
    if isinstance(value, str):
        return _redact(value)
    if isinstance(value, Mapping):
        # Nested mappings are admitted only for scalar metering/evidence fields.
        return {
            _redact(str(key))[:80]: _safe_value(item)
            for key, item in list(value.items())[:32]
            if isinstance(item, (type(None), bool, int, float, str))
        }
    if isinstance(value, (list, tuple)):
        return [
            _safe_value(item) for item in list(value)[:16]
            if isinstance(item, (type(None), bool, int, float, str, Mapping))
        ]
    return _redact(type(value).__name__)


def exception_evidence(exc: BaseException) -> dict[str, Any]:
    """Return bounded traceback metadata without locals, prompts, or credentials."""

    frames = []
    for frame in traceback.extract_tb(exc.__traceback__)[-8:]:
        frames.append({
            "file": Path(frame.filename).name,
            "function": frame.name[:120],
            "line": int(frame.lineno),
        })
    return {
        "exception_type": type(exc).__name__[:120],
        "exception_message": _redact(str(exc)),
        "exception_frames": frames,
    }


class LiveVoiceAuditLog:
    """Append independent, non-transcript evidence for Live Phone diagnosis."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self._lock = threading.Lock()

    def path_for(self, binding: CallBinding | str) -> Path:
        call_id = binding.call_id if isinstance(binding, CallBinding) else str(binding)
        if not _SAFE_FILE_ID.fullmatch(call_id):
            raise ValueError("invalid Live Phone audit id")
        return self.root / f"{call_id}.jsonl"

    def attempt_path(self, attempt_id: str) -> Path:
        if not _SAFE_FILE_ID.fullmatch(attempt_id):
            raise ValueError("invalid Live Phone attempt audit id")
        return self.root / f"attempt-{attempt_id}.jsonl"

    @staticmethod
    def _scope(binding: CallBinding) -> dict[str, Any]:
        # provider_session_id and owner_id intentionally remain in canonical state only.
        return binding.public_scope()

    def _record(
        self,
        path: Path,
        scope: Mapping[str, Any],
        event: str,
        detail: Mapping[str, Any],
    ) -> bool:
        if not isinstance(event, str) or not _EVENT.fullmatch(event):
            return False
        admitted = {
            key: (value if key == "error_code" and isinstance(value, str) and value in _PUBLIC_ERROR_CODES
                  else _safe_value(value))
            for key, value in detail.items()
            if key in _ALLOWED_DETAIL_KEYS
        }
        record = {
            "schema": AUDIT_SCHEMA,
            "audit_event_id": f"audit-{uuid4().hex}",
            "recorded_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "monotonic_ns": time.monotonic_ns(),
            "process_id": os.getpid(),
            "event": event,
            "scope": dict(scope),
            "detail": admitted,
        }
        encoded = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        if len(encoded) > _MAX_RECORD_BYTES:
            record["detail"] = {
                key: value for key, value in admitted.items()
                if key not in {"exception_frames", "usage"}
            }
            record["detail"]["detail_truncated"] = True
            encoded = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            with self._lock:
                self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
                descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
                try:
                    remaining = memoryview(encoded)
                    while remaining:
                        written = os.write(descriptor, remaining)
                        if written <= 0:
                            raise OSError("Live Voice audit append made no progress")
                        remaining = remaining[written:]
                    if event in _DURABLE_EVENTS or (
                        event == "call.phase"
                        and admitted.get("phase") in {"ended", "failed", "interrupted"}
                    ):
                        os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            return True
        except OSError as exc:
            logger.warning(
                "Live Voice audit append failed for %s (%s)",
                path.stem,
                type(exc).__name__,
            )
            return False

    def record(self, binding: CallBinding, event: str, /, **detail: Any) -> bool:
        return self._record(self.path_for(binding), self._scope(binding), event, detail)

    def record_attempt(self, attempt_id: str, event: str, /, **detail: Any) -> bool:
        return self._record(
            self.attempt_path(attempt_id),
            {"attempt_id": attempt_id},
            event,
            {"attempt_id": attempt_id, **detail},
        )
