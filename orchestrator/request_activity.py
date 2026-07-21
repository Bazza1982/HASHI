"""Bounded, presentation-only activity streams for live HASHI requests.

The request activity surface is deliberately not a transcript, audit ledger,
or scheduler.  It retains a small in-memory projection of backend stream
events so local clients such as Aptenra can show that work is still active.
Restarting HASHI clears this projection; durable task and audit state continue
to be owned by their existing stores.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import OrderedDict
from typing import Any


_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer [REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"), "[REDACTED_API_KEY]"),
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{24,}\b"), "[REDACTED_BOT_TOKEN]"),
    (
        re.compile(
            r"(?i)(\b(?:password|passwd|token|secret|authorization|cookie|private[_ -]?key)"
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


def _safe_text(value: object, *, limit: int) -> str:
    text = str(value or "")
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    if len(text) > limit:
        return text[:limit] + f"…[truncated {len(text) - limit} chars]"
    return text


class RequestActivityStore:
    """Thread-safe, bounded request event projection.

    Each runtime owns one store.  Events are safe to poll by request id and are
    intentionally cleared on runtime restart.  This keeps the feature a user
    display layer rather than another source of truth.
    """

    def __init__(
        self,
        *,
        logger: logging.Logger | None = None,
        max_requests: int = 64,
        max_events_per_request: int = 256,
    ) -> None:
        self.logger = logger or logging.getLogger(__name__)
        self.max_requests = max(8, min(int(max_requests), 256))
        self.max_events_per_request = max(32, min(int(max_events_per_request), 1_024))
        self._lock = threading.RLock()
        self._requests: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def _prune_unlocked(self) -> None:
        while len(self._requests) > self.max_requests:
            removable = next(
                (
                    request_id
                    for request_id, record in self._requests.items()
                    if bool(record.get("terminal"))
                ),
                next(iter(self._requests), None),
            )
            if removable is None:
                return
            self._requests.pop(removable, None)

    def _ensure_unlocked(
        self,
        request_id: str,
        *,
        source: str = "",
        created_at: float | None = None,
    ) -> dict[str, Any]:
        safe_id = _safe_text(request_id, limit=160).strip()
        if not safe_id:
            raise ValueError("request activity requires a request id")
        record = self._requests.get(safe_id)
        if record is None:
            now = float(created_at if created_at is not None else time.time())
            record = {
                "request_id": safe_id,
                "source": _safe_text(source, limit=80),
                "state": "queued",
                "terminal": False,
                "success": None,
                "created_at": now,
                "started_at": None,
                "completed_at": None,
                "latest_sequence": 0,
                "events": [],
            }
            self._requests[safe_id] = record
            self._prune_unlocked()
        else:
            self._requests.move_to_end(safe_id)
        return record

    def _append_unlocked(
        self,
        record: dict[str, Any],
        *,
        kind: str,
        summary: object = "",
        detail: object = "",
        tool_name: object = "",
        file_path: object = "",
        current: object = None,
        total: object = None,
        unit: object = "",
        status: str = "running",
        timestamp: float | None = None,
    ) -> dict[str, Any]:
        sequence = int(record.get("latest_sequence") or 0) + 1
        safe_current = self._safe_progress_value(current)
        safe_total = self._safe_progress_value(total)
        event = {
            "sequence": sequence,
            "kind": _safe_text(kind, limit=64) or "progress",
            "status": _safe_text(status, limit=32) or "running",
            "summary": _safe_text(summary, limit=2_000),
            "detail": _safe_text(detail, limit=8_000),
            "tool_name": _safe_text(tool_name, limit=160),
            "file_path": _safe_text(file_path, limit=4_096),
            "current": safe_current,
            "total": safe_total,
            "unit": _safe_text(unit, limit=40),
            "timestamp": float(timestamp if timestamp is not None else time.time()),
        }
        record["latest_sequence"] = sequence
        record["events"] = [
            *list(record.get("events") or []),
            event,
        ][-self.max_events_per_request :]
        return dict(event)

    @staticmethod
    def _safe_progress_value(value: object) -> float | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if number < 0 or number != number or number in (float("inf"), float("-inf")):
            return None
        return number

    def start(self, request_id: str, *, source: str = "", created_at: float | None = None) -> None:
        with self._lock:
            record = self._ensure_unlocked(request_id, source=source, created_at=created_at)
            if record["events"]:
                return
            self._append_unlocked(
                record,
                kind="queued",
                summary="Queued",
                status="pending",
                timestamp=created_at,
            )

    def mark_running(self, request_id: str, *, timestamp: float | None = None) -> None:
        with self._lock:
            record = self._ensure_unlocked(request_id)
            if record.get("terminal"):
                return
            now = float(timestamp if timestamp is not None else time.time())
            record["state"] = "running"
            record["started_at"] = record.get("started_at") or now
            self._append_unlocked(
                record,
                kind="started",
                summary="Working",
                status="running",
                timestamp=now,
            )

    def publish_stream(self, request_id: str, event: object) -> None:
        try:
            kind = str(getattr(event, "kind", "progress") or "progress")
            status = {
                "tool_end": "completed",
                "error": "failed",
            }.get(kind, "running")
            with self._lock:
                record = self._ensure_unlocked(request_id)
                if record.get("terminal"):
                    return
                self._append_unlocked(
                    record,
                    kind=kind,
                    summary=getattr(event, "summary", ""),
                    detail=getattr(event, "detail", ""),
                    tool_name=getattr(event, "tool_name", ""),
                    file_path=getattr(event, "file_path", ""),
                    current=getattr(event, "current", None),
                    total=getattr(event, "total", None),
                    unit=getattr(event, "unit", ""),
                    status=status,
                    timestamp=getattr(event, "timestamp", None),
                )
        except Exception as exc:  # display telemetry must not break generation
            self.logger.warning(
                "Request activity stream event dropped for %s (%s)",
                _safe_text(request_id, limit=160),
                type(exc).__name__,
            )

    def complete(
        self,
        request_id: str,
        *,
        success: bool,
        error: object = "",
        timestamp: float | None = None,
    ) -> None:
        with self._lock:
            record = self._ensure_unlocked(request_id)
            if record.get("terminal"):
                return
            now = float(timestamp if timestamp is not None else time.time())
            record["state"] = "completed" if success else "failed"
            record["terminal"] = True
            record["success"] = bool(success)
            record["completed_at"] = now
            self._append_unlocked(
                record,
                kind="completed" if success else "error",
                summary="Completed" if success else (_safe_text(error, limit=2_000) or "Failed"),
                detail="" if success else error,
                status="completed" if success else "failed",
                timestamp=now,
            )

    def poll(self, request_id: str, *, after_sequence: int = 0, limit: int = 100) -> dict[str, Any]:
        after = max(0, int(after_sequence))
        safe_limit = max(1, min(int(limit), 256))
        with self._lock:
            record = self._requests.get(str(request_id or ""))
            if record is None:
                return {
                    "ok": False,
                    "error": "request activity not found",
                    "error_code": "request_activity_not_found",
                }
            events = [
                dict(event)
                for event in list(record.get("events") or [])
                if int(event.get("sequence") or 0) > after
            ][:safe_limit]
            return {
                "ok": True,
                "request_id": record["request_id"],
                "state": record["state"],
                "terminal": bool(record["terminal"]),
                "success": record["success"],
                "created_at": record["created_at"],
                "started_at": record["started_at"],
                "completed_at": record["completed_at"],
                "latest_sequence": int(record["latest_sequence"]),
                "events": events,
            }
