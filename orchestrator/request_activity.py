"""Bounded, presentation-only activity streams for live HASHI requests.

The request activity surface is deliberately not a transcript, audit ledger,
or scheduler.  It retains a small in-memory projection of backend stream
events so authorized local frontends can show that work is still active.
Restarting HASHI clears this projection; durable task and audit state continue
to be owned by their existing stores.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Mapping

from adapters.stream_events import (
    DELIVERY_ANSWER_PREVIEW,
    DELIVERY_FINAL,
    DELIVERY_INTERNAL,
    legacy_delivery_class,
)
from orchestrator.flexible_backend_registry import HER_V3_ENGINE


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
        epoch: int | None = None,
    ) -> None:
        self.logger = logger or logging.getLogger(__name__)
        self.max_requests = max(8, min(int(max_requests), 256))
        self.max_events_per_request = max(32, min(int(max_events_per_request), 1_024))
        # This volatile epoch changes whenever the owning Function process is
        # replaced.  Frontends keep it separate from the durable Session
        # cursor so a restart cannot make old ephemeral positions look valid.
        self.epoch = max(
            1,
            int(epoch if epoch is not None else time.time_ns() // 1_000_000),
        )
        self._lock = threading.RLock()
        self._requests: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._presentation_settings_by_request: dict[
            str, Callable[[], Mapping[str, Any]]
        ] = {}

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
            self._presentation_settings_by_request.pop(removable, None)

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
                "tool_activities": OrderedDict(),
                "activity_generation": None,
                "last_verbose": False,
            }
            self._requests[safe_id] = record
            self._prune_unlocked()
        else:
            self._requests.move_to_end(safe_id)
        return record

    def bind_presentation_settings(
        self,
        request_id: str,
        probe: Callable[[], Mapping[str, Any]],
    ) -> None:
        """Bind live presentation switches to one request's activity stream."""

        if not callable(probe):
            raise TypeError("presentation settings probe must be callable")
        safe_id = _safe_text(request_id, limit=160).strip()
        if not safe_id:
            raise ValueError("presentation settings require a request id")
        with self._lock:
            self._presentation_settings_by_request[safe_id] = probe

    def _settings_for_request_unlocked(
        self,
        request_id: str,
    ) -> Mapping[str, Any]:
        probe = self._presentation_settings_by_request.get(str(request_id or ""))
        if not callable(probe):
            probe = getattr(self, "presentation_settings", None)
        if not callable(probe):
            return {}
        try:
            settings = probe()
        except Exception as exc:  # display telemetry must remain best effort
            self.logger.warning(
                "Request activity presentation settings failed for %s (%s)",
                _safe_text(request_id, limit=160),
                type(exc).__name__,
            )
            return {}
        return settings if isinstance(settings, Mapping) else {}

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
        event_id: object = "",
        delivery_class: object = "",
        origin: object = "",
        phase: object = "",
        revision: object = None,
        required: object = False,
        provenance: object = "",
        status: str = "running",
        timestamp: float | None = None,
    ) -> dict[str, Any]:
        sequence = int(record.get("latest_sequence") or 0) + 1
        safe_current = self._safe_progress_value(current)
        safe_total = self._safe_progress_value(total)
        event_timestamp = float(timestamp if timestamp is not None else time.time())
        events = list(record.get("events") or [])
        timestamp_floor = (
            float(events[-1]["timestamp"])
            if events
            else float(record.get("created_at", event_timestamp))
        )
        event_timestamp = max(timestamp_floor, event_timestamp)
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
            "event_id": _safe_text(event_id, limit=240),
            "delivery_class": _safe_text(delivery_class, limit=40),
            "origin": _safe_text(origin, limit=80),
            "phase": _safe_text(phase, limit=80),
            "revision": self._safe_progress_value(revision),
            "required": bool(required),
            "provenance": _safe_text(provenance, limit=80),
            "timestamp": event_timestamp,
        }
        record["latest_sequence"] = sequence
        record["events"] = [
            *events,
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
            record = self._requests.get(str(request_id or ""))
            if record is None:
                return
            if record.get("terminal"):
                return
            now = float(timestamp if timestamp is not None else time.time())
            record["state"] = "running"
            event = self._append_unlocked(
                record,
                kind="started",
                summary="Working",
                status="running",
                timestamp=now,
            )
            record["started_at"] = record.get("started_at") or event["timestamp"]

    def publish_stream(self, request_id: str, event: object) -> None:
        try:
            kind = str(getattr(event, "kind", "progress") or "progress")
            activity = None
            if kind == "tool_activity":
                activity = _tool_activity_fields(getattr(event, "metadata", None))
                if activity is None or activity.get("request_id") not in {"", request_id}:
                    return
            model_route_fields: dict[str, Any] = {}
            if kind == "model_route":
                metadata = getattr(event, "metadata", None)
                if not isinstance(metadata, Mapping):
                    return
                route_status = str(metadata.get("route_status") or "")
                if (
                    metadata.get("engine") != HER_V3_ENGINE
                    or route_status not in {"selected", "returned"}
                ):
                    return
                provider = _safe_text(metadata.get("model_provider"), limit=160).strip()
                model = _safe_text(metadata.get("model"), limit=240).strip()
                try:
                    attempt = int(metadata.get("attempt"))
                except (TypeError, ValueError):
                    return
                if not provider or not model or not 1 <= attempt <= 1_000_000:
                    return
                model_route_fields = {
                    "engine": HER_V3_ENGINE,
                    "model_provider": provider,
                    "model": model,
                    "route_status": route_status,
                    "attempt": attempt,
                }
            tool_outcome = "unknown"
            status = {
                "tool_end": "completed",
                "error": "failed",
            }.get(kind, "running")
            if kind == "tool_end":
                metadata = getattr(event, "metadata", None)
                if isinstance(metadata, Mapping):
                    outcome = str(metadata.get("outcome") or "").casefold()
                    if metadata.get("is_error") is True or outcome == "failed":
                        status = "failed"
                        tool_outcome = "failed"
                    elif metadata.get("is_error") is False or outcome == "success":
                        status = "completed"
                        tool_outcome = "success"
                    elif outcome == "unknown":
                        status = "unknown"
            with self._lock:
                record = self._requests.get(str(request_id or ""))
                if record is None:
                    return
                if record.get("terminal"):
                    return
                if activity is not None:
                    generation = activity["function_generation"]
                    if record["activity_generation"] not in {None, generation}:
                        return
                    record["activity_generation"] = generation
                    key = activity["operation_id"] + ":" + activity["tool_call_id"]
                    snapshots = record["tool_activities"]
                    previous = snapshots.get(key)
                    if previous and (previous["update_sequence"] >= activity["update_sequence"] or
                                     previous["state"] in {"completed", "failed", "interrupted", "cleanup_pending"}):
                        return
                    snapshots[key] = activity
                    snapshots.move_to_end(key)
                    while len(snapshots) > 32:
                        snapshots.popitem(last=False)
                    if previous is None or activity["visible_update"]:
                        record["last_verbose"] = bool(self._settings_for_request_unlocked(request_id).get("verbose", False))
                    # Quiet snapshots are not chat events. Poll can project the
                    # latest one when verbose is enabled during a running tool.
                    if not activity["visible_update"]:
                        return
                    status = activity["state"]
                if kind == "tool_end":
                    details = (getattr(event, "metadata", None) or {}).get("tool_result_details") or {}
                    if details.get("search_outcome") in {"partial", "failed", "unavailable"}:
                        status = str(details["search_outcome"])
                        tool_outcome = status
                event_id = _safe_text(getattr(event, "event_id", ""), limit=240)
                if event_id and any(
                    existing.get("event_id") == event_id
                    for existing in record.get("events") or ()
                ):
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
                    event_id=event_id,
                    delivery_class=getattr(event, "delivery_class", ""),
                    origin=getattr(event, "origin", ""),
                    phase=getattr(event, "phase", ""),
                    revision=getattr(event, "revision", None),
                    required=getattr(event, "required", False),
                    provenance=getattr(event, "provenance", ""),
                    status=status,
                    timestamp=getattr(event, "timestamp", None),
                )
                # A Connector projection of the existing delivery owner and
                # runtime switches. Internal/unknown HER events stay closed.
                owner = (
                    DELIVERY_INTERNAL
                    if kind == "model_route"
                    else str(getattr(event, "delivery_class", "") or "")
                )
                if not owner and not str(getattr(event, "origin", "")).startswith("her_v2"):
                    owner = legacy_delivery_class(kind)
                channel = {
                    "reasoning": "thinking",
                    "user_commentary": "commentary",
                    "technical": "verbose",
                    "control": "control",
                    DELIVERY_ANSWER_PREVIEW: "answer",
                }.get(owner)
                settings = self._settings_for_request_unlocked(request_id)
                if owner == DELIVERY_ANSWER_PREVIEW:
                    # Answer previews are a Workbench-only ephemeral lane.  A
                    # local frontend opts in through the typed presentation
                    # settings; Telegram never receives this owner.
                    enabled = bool(settings.get("answer_preview", False))
                elif owner == DELIVERY_FINAL and bool(
                    settings.get("answer_preview", False)
                ):
                    # The final event is the authoritative replacement point
                    # for the ephemeral preview.  It remains deferred by the
                    # HER router for ordinary transport delivery.
                    channel = "answer"
                    enabled = True
                else:
                    enabled = bool(
                        channel
                        and (
                            settings.get(
                                channel if channel != "thinking" else "think", False
                            )
                            if channel != "control"
                            else getattr(event, "required", False)
                        )
                    )
                if channel == "commentary" and bool(getattr(event, "required", False)):
                    enabled = True
                projected = record["events"][-1]
                projected.update(delivery_class=owner, presentation_channel=channel or "internal",
                                 presentation_enabled=enabled)
                if kind == "tool_end":
                    # Public, receipt-derived fact; no private Tool result
                    # metadata or interpretation of its output prose.
                    projected["outcome"] = tool_outcome
                if model_route_fields:
                    projected.update(model_route_fields)
                if activity is not None:
                    projected["tool_activity"] = activity
                if channel == "answer":
                    projected.update(
                        answer_state=(
                            "complete" if owner == DELIVERY_FINAL else "delta"
                        ),
                        answer_authoritative=owner == DELIVERY_FINAL,
                        answer_ephemeral=owner != DELIVERY_FINAL,
                    )
                if enabled and channel == "thinking" and getattr(event, "raw_delta", ""):
                    projected["raw_delta"] = _safe_text(event.raw_delta, limit=8_000)
                    projected["body_truncated"] = len(str(event.raw_delta)) > 8_000
                elif enabled:
                    body = str(getattr(event, "summary", "") or getattr(event, "detail", "") or "")
                    projected["body_truncated"] = len(body) > (2_000 if getattr(event, "summary", "") else 8_000)
                if channel == "thinking":
                    # Sequence of the first event in this contiguous source block
                    # stays stable when another transport page is interleaved.
                    previous = record["events"][-2] if len(record["events"]) > 1 else {}
                    projected["block_id"] = previous.get("block_id", previous.get("sequence")) if previous.get("presentation_channel") == channel else projected["sequence"]
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
            record = self._requests.get(str(request_id or ""))
            if record is None:
                return
            if record.get("terminal"):
                return
            now = float(timestamp if timestamp is not None else time.time())
            record["state"] = "completed" if success else "failed"
            record["terminal"] = True
            record["success"] = bool(success)
            event = self._append_unlocked(
                record,
                kind="completed" if success else "error",
                summary="Completed" if success else (_safe_text(error, limit=2_000) or "Failed"),
                detail="" if success else error,
                status="completed" if success else "failed",
                timestamp=now,
            )
            record["completed_at"] = event["timestamp"]
            self._presentation_settings_by_request.pop(str(request_id or ""), None)

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
                    "ephemeral_epoch": self.epoch,
                }
            verbose = bool(self._settings_for_request_unlocked(request_id).get("verbose", False))
            if verbose and not record["last_verbose"] and not record["terminal"]:
                for key, snapshot in list(record["tool_activities"].items()):
                    if snapshot["state"] not in {"running", "started", "cancelling"}:
                        continue
                    self._append_unlocked(record, kind="tool_activity",
                        summary=snapshot["display_summary"], status=snapshot["state"],
                        tool_name=snapshot["tool"], delivery_class="technical",
                        event_id=f"{key}:verbose-snapshot:{record['latest_sequence']+1}")
                    projected = record["events"][-1]
                    projected.update(presentation_channel="verbose", presentation_enabled=True,
                                     tool_activity=dict(snapshot))
            record["last_verbose"] = verbose
            events = [
                dict(event)
                for event in list(record.get("events") or [])
                if int(event.get("sequence") or 0) > after
            ][:safe_limit]
            return {
                "ok": True,
                "request_id": record["request_id"],
                "ephemeral_epoch": self.epoch,
                "state": record["state"],
                "terminal": bool(record["terminal"]),
                "success": record["success"],
                "created_at": record["created_at"],
                "started_at": record["started_at"],
                "completed_at": record["completed_at"],
                "latest_sequence": int(record["latest_sequence"]),
                "earliest_available_sequence": int(record["events"][0]["sequence"]) if record["events"] else 0,
                "replay_complete": not record["events"] or after >= int(record["events"][0]["sequence"]) - 1,
                "presentation_available": callable(getattr(self, "presentation_settings", None)),
                "events": events,
                "active_tool_activities": [dict(snapshot) for snapshot in record["tool_activities"].values()
                    if snapshot["state"] in {"running", "started", "cancelling"}] if verbose else [],
            }


def _tool_activity_fields(metadata: object) -> dict[str, Any] | None:
    """Functions-only metadata allowlist; never forward arbitrary tool payloads."""
    if not isinstance(metadata, Mapping):
        return None
    state = metadata.get("state")
    if state not in {"started", "running", "cancelling", "completed", "failed", "interrupted", "cleanup_pending"}:
        return None
    try:
        sequence = int(metadata.get("update_sequence"))
    except (TypeError, ValueError):
        return None
    if not 1 <= sequence <= 1_000_000:
        return None
    result = {"state": state, "update_sequence": sequence,
              "visible_update": metadata.get("visible_update") is True}
    for key, limit in {"operation_id": 100, "tool_call_id": 160, "tool": 80,
                       "function_generation": 160, "request_id": 160, "agent_id": 160,
                       "operation_type": 80, "scope_provenance": 40, "liveness": 32,
                       "progress": 32, "stop_reason": 64, "scope_advisory": 300,
                       "display_summary": 2000}.items():
        result[key] = _safe_text(metadata.get(key), limit=limit)
    if not all(result[key] for key in ("operation_id", "tool_call_id", "function_generation")):
        return None
    for key in ("started_at", "elapsed_ms", "last_worker_response", "last_output", "last_work_progress"):
        result[key] = RequestActivityStore._safe_progress_value(metadata.get(key))
    roots = metadata.get("selected_roots") or []
    if not isinstance(roots, list):
        return None
    result["selected_roots"] = [_safe_text(root, limit=512) for root in roots[:16] if isinstance(root, str)]
    counters = metadata.get("counters") or {}
    result["counters"] = {key: RequestActivityStore._safe_progress_value(counters.get(key)) for key in (
        "directories_enumerated", "files_enumerated", "text_files_checked", "characters_read",
        "bytes_read", "matches", "skipped", "errors", "stdout_bytes", "stderr_bytes")}
    for key in ("partial", "coverage_complete"):
        value = metadata.get(key)
        result[key] = value if isinstance(value, bool) else None
    cleanup = metadata.get("cleanup") or {}
    result["cleanup"] = {key: cleanup.get(key) for key in (
        "process_reaped", "group_alive", "forced") if isinstance(cleanup.get(key), bool)}
    result["cleanup"]["status"] = _safe_text(cleanup.get("status"), limit=40)
    return result
