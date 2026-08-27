"""Durable, idempotent HER v2 audit persistence with fallback spooling."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol


class AuditPersistenceError(RuntimeError):
    """Raised when neither the primary log nor approved fallback is durable."""


class AuditWriter(Protocol):
    def append(self, record: Mapping[str, Any]) -> str: ...


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "authorization",
        "password",
        "passwd",
        "secret",
        "cookie",
        "private_key",
    }
)
_SECRET_TEXT_PATTERNS = (
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer [REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"), "[REDACTED_API_KEY]"),
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{24,}\b"), "[REDACTED_BOT_TOKEN]"),
    (
        re.compile(
            r"(?i)(\b(?:api[_ -]?key|password|passwd|token|secret|authorization|cookie|private[_ -]?key)"
            r"\s*[:=]\s*)([^\s,;]+)"
        ),
        r"\1[REDACTED]",
    ),
    (
        re.compile(
            r"(?i)([?&](?:access_token|refresh_token|token|key|secret|signature)=)[^&#\s]+"
        ),
        r"\1[REDACTED]",
    ),
)


def _redact(value: Any, *, key: str = "") -> Any:
    lowered = key.casefold().replace("-", "_").replace(" ", "_")
    if lowered in _SENSITIVE_KEYS or lowered.endswith("_api_key"):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {
            str(item): _redact(content, key=str(item))
            for item, content in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        cleaned = value
        for pattern, replacement in _SECRET_TEXT_PATTERNS:
            cleaned = pattern.sub(replacement, cleaned)
        return cleaned
    return value


class JsonlAuditWriter:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def append(self, record: Mapping[str, Any]) -> str:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        with self._lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        self.path.chmod(0o600)
        return f"hashi-log:{self.path.name}:{record['event_id']}"


class DurableAuditLog:
    """Write every required record to primary storage or a durable spool.

    Event identifiers are idempotency keys.  Replaying a fallback spool never
    duplicates an event already present in the primary log.
    """

    def __init__(
        self,
        primary_path: Path | None = None,
        fallback_path: Path | None = None,
        *,
        primary_writer: AuditWriter | None = None,
        fallback_writer: AuditWriter | None = None,
        redactor: Callable[[Any], Any] | None = None,
        observer: Callable[[Mapping[str, Any]], None] | None = None,
    ):
        if primary_writer is None and primary_path is None:
            raise ValueError("a primary audit path or writer is required")
        if fallback_writer is None and fallback_path is None:
            raise ValueError("a fallback audit path or writer is required")
        self.primary_path = Path(primary_path) if primary_path is not None else None
        self.fallback_path = Path(fallback_path) if fallback_path is not None else None
        self.primary_writer = primary_writer or JsonlAuditWriter(self.primary_path)  # type: ignore[arg-type]
        self.fallback_writer = fallback_writer or JsonlAuditWriter(self.fallback_path)  # type: ignore[arg-type]
        self.redactor = redactor or _redact
        self.observer = observer
        self._seen = self._read_ids(self.primary_path) | self._read_ids(
            self.fallback_path
        )
        self._lock = threading.Lock()

    @staticmethod
    def _read_ids(path: Path | None) -> set[str]:
        if path is None or not path.exists():
            return set()
        result: set[str] = set()
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                row = json.loads(line)
                if isinstance(row, Mapping) and row.get("event_id"):
                    result.add(str(row["event_id"]))
        except (OSError, json.JSONDecodeError):
            return result
        return result

    def append(
        self,
        *,
        event_id: str,
        turn_id: str,
        request_ref: str,
        stage: str,
        role: str,
        event: str,
        provider: str = "",
        model: str = "",
        attempt: int = 1,
        plan_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> str:
        identifier = str(event_id or "").strip()
        if not identifier:
            canonical = "|".join(
                [turn_id, stage, role, event, str(attempt), str(plan_id or "")]
            )
            identifier = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
        with self._lock:
            if identifier in self._seen:
                return f"hashi-log:deduplicated:{identifier}"
            record = {
                "format": "her-v2-audit-v1",
                "event_id": identifier,
                "recorded_at": _timestamp(),
                "turn_id": str(turn_id),
                "request_ref": str(request_ref),
                "stage": str(stage),
                "role": str(role),
                "event": str(event),
                "provider": str(provider),
                "model": str(model),
                "attempt": int(attempt),
                "plan_id": str(plan_id) if plan_id else None,
                "payload": self.redactor(dict(payload or {})),
            }
            try:
                ref = self.primary_writer.append(record)
            except Exception as primary_error:
                try:
                    ref = self.fallback_writer.append(record)
                except Exception as fallback_error:
                    raise AuditPersistenceError(
                        "HER v2 audit persistence failed in primary and fallback storage "
                        f"(primary={type(primary_error).__name__}, "
                        f"fallback={type(fallback_error).__name__})"
                    ) from fallback_error
            self._seen.add(identifier)
            if self.observer is not None:
                try:
                    self.observer(record)
                except Exception:
                    # WIP observation must never change HER control flow after
                    # the canonical audit record is already durable.
                    pass
            return ref

    def record_reasoning(
        self,
        *,
        event_id: str,
        turn_id: str,
        request_ref: str,
        stage: str,
        role: str,
        provider: str,
        model: str,
        attempt: int,
        plan_id: str | None,
        trace: str | None,
    ) -> str:
        available = trace is not None and bool(str(trace).strip())
        return self.append(
            event_id=event_id,
            turn_id=turn_id,
            request_ref=request_ref,
            stage=stage,
            role=role,
            event="reasoning_trace",
            provider=provider,
            model=model,
            attempt=attempt,
            plan_id=plan_id,
            payload={
                "availability": "available" if available else "unavailable",
                **({"trace": str(trace)} if available else {}),
            },
        )

    def replay_fallback(self) -> int:
        if self.primary_path is None or self.fallback_path is None:
            return 0
        if not self.fallback_path.exists():
            return 0
        primary_ids = self._read_ids(self.primary_path)
        rows: list[Mapping[str, Any]] = []
        for line in self.fallback_path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, Mapping):
                rows.append(row)
        replayed = 0
        pending: list[Mapping[str, Any]] = []
        for row in rows:
            identifier = str(row.get("event_id") or "")
            if identifier in primary_ids:
                continue
            try:
                self.primary_writer.append(row)
            except Exception:
                pending.append(row)
                continue
            primary_ids.add(identifier)
            replayed += 1
        self.fallback_path.parent.mkdir(parents=True, exist_ok=True)
        with self.fallback_path.open("w", encoding="utf-8") as handle:
            for row in pending:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self.fallback_path.chmod(0o600)
        return replayed
