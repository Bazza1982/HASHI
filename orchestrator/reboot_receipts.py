"""PAO-owned, bounded reboot results; delivery never owns reboot outcome."""

from __future__ import annotations

from copy import deepcopy
import json
import logging
import math
import os
from pathlib import Path
import time
from uuid import uuid4

from orchestrator.kernel_process import write_record

MAX_RECORDS = 50
MAX_BYTES = 128 * 1024
MAX_DELIVERY_ATTEMPTS = 4
ACTIVE = frozenset({"accepted", "running"})
TERMINAL = frozenset({"succeeded", "failed", "rejected", "unconfirmed"})
OUTCOMES = frozenset(
    {
        "succeeded",
        "succeeded_with_warning",
        "partial",
        "rejected",
        "rolled_back",
        "failed",
    }
)
OBSERVATION_CONFIDENCE = frozenset({"confirmed", "unconfirmed"})
LIFECYCLE_STATES = frozenset(
    {
        "accepted",
        "candidate_rejected",
        "committed",
        "rolled_back",
        "online",
        "rejected",
        "unconfirmed",
    }
)
RECEIPT_SCHEMA_VERSION = 6
MAX_PROGRESS_EVENTS = 32
REBOOT_TIMING_STAGES = (
    "start_notice",
    "activity_preflight",
    "generation_cache_lookup",
    "qualification",
    "materialization",
    "candidate_start",
    "cutover",
    "final_observation",
    "notification_enqueue",
)
REBOOT_WORK_COUNTS = (
    "candidate_worker_count",
    "manifest_hash_count",
    "full_closure_import_count",
)
_WINDOWS_REPLACE_RETRY_DELAYS = (0.025, 0.075, 0.2, 0.5)
logger = logging.getLogger("BridgeU.RebootReceipts")


def _default_shared_replacement():
    return {
        "status": "not_requested",
        "request_id": None,
        "requested_at": None,
        "old_shared_pid": None,
        "generation_id": None,
    }


def _transient_windows_replace_denial(exc: PermissionError) -> bool:
    return os.name == "nt" and getattr(exc, "winerror", None) in {5, 32}


def _write_receipt_record(path: Path, payload: dict) -> None:
    """Retry only transient Windows replace denials before commit."""

    for attempt in range(len(_WINDOWS_REPLACE_RETRY_DELAYS) + 1):
        try:
            write_record(path, payload)
            return
        except PermissionError as exc:
            if (
                not _transient_windows_replace_denial(exc)
                or attempt == len(_WINDOWS_REPLACE_RETRY_DELAYS)
            ):
                raise
            time.sleep(_WINDOWS_REPLACE_RETRY_DELAYS[attempt])


def _default_delivery(*, requested: bool = False):
    return {
        "status": "pending" if requested else "not_requested",
        "attempts": 0,
        "next_attempt_at": 0,
    }


def _default_presentation_ack(*, required: bool = False):
    return {
        "status": "pending" if required else "not_required",
        "expected_sequence": 1 if required else None,
        "sequence": None,
        "message_id": None,
        "confirmed_at": None,
    }


def _lifecycle_for(record):
    status = record.get("status")
    if status == "succeeded":
        return "online"
    if status == "unconfirmed":
        return "unconfirmed"
    if status == "failed":
        return "rolled_back" if record.get("restored") else "unconfirmed"
    if status == "rejected":
        return (
            "candidate_rejected"
            if record.get("reason") == "candidate_rejected"
            else "rejected"
        )
    if record.get("committed") or record.get("phase") == "committed":
        return "committed"
    return "accepted"


def _normalize_record(record):
    if not isinstance(record, dict):
        raise ValueError("Invalid reboot receipt record")
    record.setdefault("lifecycle_state", _lifecycle_for(record))
    record.setdefault("workers", {})
    shared = record.setdefault("shared_replacement", _default_shared_replacement())
    if isinstance(shared, dict):
        for key, value in _default_shared_replacement().items():
            shared.setdefault(key, deepcopy(value))
    # Legacy receipts have no separately provable start-delivery event.  Do
    # not infer one merely because their final origin was deliverable.
    record.setdefault("start_delivery", _default_delivery())
    # v1-v4 receipts predate browser-render confirmation.  Historical
    # operations must remain readable, but must never be reinterpreted as
    # having received a presentation acknowledgement.
    record.setdefault("presentation_ack", _default_presentation_ack())
    record.setdefault("progress", [])
    if "outcome" not in record:
        status = record.get("status")
        if status in ACTIVE:
            record["outcome"] = None
        elif status == "succeeded":
            record["outcome"] = "succeeded"
        elif status == "rejected":
            record["outcome"] = "rejected"
        elif status == "failed" and record.get("restored"):
            record["outcome"] = "rolled_back"
        elif status == "unconfirmed" and any(
            bool(value) for value in (record.get("online") or {}).values()
        ):
            record["outcome"] = "partial"
        else:
            record["outcome"] = "failed"
    record.setdefault(
        "observation_confidence",
        "unconfirmed" if record.get("status") == "unconfirmed" else "confirmed",
    )
    record.setdefault("warnings", [])
    timings = record.setdefault("stage_timings_ms", {})
    if isinstance(timings, dict):
        for name in REBOOT_TIMING_STAGES:
            timings.setdefault(name, 0.0)
    counts = record.setdefault("work_counts", {})
    if isinstance(counts, dict):
        for name in REBOOT_WORK_COUNTS:
            counts.setdefault(name, 0)
    return record


def clean_origin(origin):
    if origin is None:
        return {}
    if not isinstance(origin, dict):
        raise ValueError("Invalid reboot origin")
    surface = origin.get("surface") or "telegram"
    if not isinstance(surface, str) or len(surface) > 32:
        raise ValueError("Invalid reboot frontend")
    result = {"surface": surface}
    for key in ("owner_id", "actor_id", "chat_id", "thread_id"):
        value = origin.get(key)
        if value in (None, ""):
            result[key] = None
        elif isinstance(value, bool) or not isinstance(value, (str, int)):
            raise ValueError("Invalid reboot origin identifier")
        elif key in {"owner_id", "actor_id"} or (
            key == "chat_id" and surface != "telegram"
        ):
            if len(str(value)) > 200:
                raise ValueError("Invalid reboot origin identifier")
            result[key] = str(value)
        elif len(str(value)) > 24 or not str(value).lstrip("-").isdigit():
            raise ValueError("Invalid reboot origin identifier")
        else:
            result[key] = int(value)
    if result.get("owner_id") is None:
        legacy_actor = str(result.get("actor_id") or "")
        if legacy_actor.isdigit():
            result["owner_id"] = f"user:{legacy_actor}"
    if origin.get("session_id") is not None:
        session_id = origin["session_id"]
        generation = origin.get("context_generation")
        if (not isinstance(session_id, str) or not 1 <= len(session_id) <= 200
                or not isinstance(generation, int) or isinstance(generation, bool) or generation < 1):
            raise ValueError("Invalid reboot Conversation Session")
        result.update(session_id=session_id, context_generation=generation,
                      session_channel_key=str(origin.get("session_channel_key") or "default"))
        if origin.get("delivery_route") is not None:
            from orchestrator.frontend_delivery import normalize_run_delivery_route

            result["delivery_route"] = normalize_run_delivery_route(origin["delivery_route"])
    return result


def origin_delivery_requested(origin) -> bool:
    """Whether a reboot receipt should attempt delivery to its origin.

    Telegram receipts need a resolved chat to send to.  Workbench receipts are
    delivered through the shared primary session that survives Worker cutover,
    so they are always deliverable.  Unknown frontends remain ``not_requested``
    (fail closed) instead of inventing a new transport.
    """

    if not isinstance(origin, dict):
        return False
    surface = origin.get("surface", "telegram")
    if surface == "telegram":
        return bool(origin.get("chat_id"))
    return surface == "workbench"


def validate_record(record):
    try:
        valid = (
            isinstance(record["id"], str)
            and bool(record["id"])
            and isinstance(record["source_agent"], str)
            and isinstance(record["targets"], list)
            and len(record["targets"]) <= 100
            and all(isinstance(name, str) for name in record["targets"])
            and isinstance(record["display_names"], dict)
            and isinstance(record["mode"], str)
            and record["status"] in ACTIVE | TERMINAL
            and (record.get("outcome") is None or record.get("outcome") in OUTCOMES)
            and record.get("observation_confidence") in OBSERVATION_CONFIDENCE
            and isinstance(record.get("warnings"), list)
            and len(record.get("warnings")) <= 100
            and all(isinstance(item, str) for item in record.get("warnings"))
            and isinstance(record.get("stage_timings_ms"), dict)
            and set(record.get("stage_timings_ms")) == set(REBOOT_TIMING_STAGES)
            and all(
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
                and value >= 0
                for value in record.get("stage_timings_ms").values()
            )
            and isinstance(record.get("work_counts"), dict)
            and set(record.get("work_counts")) == set(REBOOT_WORK_COUNTS)
            and all(
                isinstance(value, int)
                and not isinstance(value, bool)
                and value >= 0
                for value in record.get("work_counts").values()
            )
            and record["lifecycle_state"] in LIFECYCLE_STATES
            and isinstance(record["workers"], dict)
            and isinstance(record["shared_replacement"], dict)
            and isinstance(record["shared_replacement"].get("adoption", {}), dict)
            and record["shared_replacement"].get("adoption", {}).get("status")
            in {None, "qualified", "fallback", "unknown"}
            and record["shared_replacement"].get("adoption", {}).get("reason_code")
            in {None, "source_uncommitted", "qualification_failed"}
            and record["shared_replacement"].get("status")
            in {
                "not_requested",
                "requested",
                "committed",
                "rolled_back",
                "unconfirmed",
            }
            and (
                record["shared_replacement"].get("request_id") is None
                or (
                    isinstance(record["shared_replacement"].get("request_id"), str)
                    and len(record["shared_replacement"]["request_id"]) == 32
                    and all(
                        char in "0123456789abcdef"
                        for char in record["shared_replacement"]["request_id"]
                    )
                )
            )
            and (
                record["shared_replacement"].get("requested_at") is None
                or (
                    isinstance(
                        record["shared_replacement"].get("requested_at"),
                        (int, float),
                    )
                    and not isinstance(
                        record["shared_replacement"].get("requested_at"), bool
                    )
                    and math.isfinite(
                        record["shared_replacement"]["requested_at"]
                    )
                )
            )
            and (
                record["shared_replacement"].get("old_shared_pid") is None
                or (
                    isinstance(
                        record["shared_replacement"].get("old_shared_pid"), int
                    )
                    and not isinstance(
                        record["shared_replacement"].get("old_shared_pid"), bool
                    )
                    and record["shared_replacement"]["old_shared_pid"] > 0
                )
            )
            and (
                record["shared_replacement"].get("generation_id") is None
                or isinstance(
                    record["shared_replacement"].get("generation_id"), str
                )
            )
            and isinstance(record["shared_replacement"].get("remote", {}), dict)
            and (
                record.get("duration_seconds") is None
                or (
                    isinstance(record["duration_seconds"], (int, float))
                    and not isinstance(record["duration_seconds"], bool)
                    and math.isfinite(record["duration_seconds"])
                    and record["duration_seconds"] >= 0
                )
            )
            and record["delivery"]["status"]
            in {"pending", "sent", "exhausted", "not_requested"}
            and isinstance(record["delivery"]["attempts"], int)
            and 0 <= record["delivery"]["attempts"] <= MAX_DELIVERY_ATTEMPTS
            and math.isfinite(record["delivery"]["next_attempt_at"])
            and record["start_delivery"]["status"]
            in {"pending", "sent", "exhausted", "not_requested"}
            and isinstance(record["start_delivery"]["attempts"], int)
            and 0 <= record["start_delivery"]["attempts"] <= MAX_DELIVERY_ATTEMPTS
            and math.isfinite(record["start_delivery"]["next_attempt_at"])
            and record["presentation_ack"]["status"]
            in {"not_required", "pending", "confirmed", "expired"}
            and (
                record["presentation_ack"].get("expected_sequence") is None
                or (
                    isinstance(
                        record["presentation_ack"].get("expected_sequence"), int
                    )
                    and not isinstance(
                        record["presentation_ack"].get("expected_sequence"), bool
                    )
                    and record["presentation_ack"]["expected_sequence"] > 0
                )
            )
            and (
                record["presentation_ack"].get("sequence") is None
                or (
                    isinstance(record["presentation_ack"].get("sequence"), int)
                    and not isinstance(
                        record["presentation_ack"].get("sequence"), bool
                    )
                    and record["presentation_ack"]["sequence"] > 0
                )
            )
            and (
                record["presentation_ack"].get("message_id") is None
                or (
                    isinstance(
                        record["presentation_ack"].get("message_id"), str
                    )
                    and len(record["presentation_ack"]["message_id"]) <= 200
                )
            )
            and (
                record["presentation_ack"].get("confirmed_at") is None
                or (
                    isinstance(
                        record["presentation_ack"].get("confirmed_at"),
                        (int, float),
                    )
                    and not isinstance(
                        record["presentation_ack"].get("confirmed_at"), bool
                    )
                    and math.isfinite(record["presentation_ack"]["confirmed_at"])
                )
            )
            and isinstance(record["progress"], list)
            and len(record["progress"]) <= MAX_PROGRESS_EVENTS
            and all(
                isinstance(event, dict)
                and event.get("sequence") == index
                and isinstance(event.get("phase"), str)
                and bool(event.get("phase"))
                and isinstance(event.get("status"), str)
                and isinstance(event.get("created_at"), (int, float))
                and not isinstance(event.get("created_at"), bool)
                and math.isfinite(event["created_at"])
                for index, event in enumerate(record["progress"], start=1)
            )
        )
        clean_origin(record["origin"])
        ack = record["presentation_ack"]
        if ack["status"] == "not_required" and any(
            ack.get(key) is not None
            for key in ("expected_sequence", "sequence", "confirmed_at")
        ):
            valid = False
        if ack["status"] in {"pending", "expired"} and (
            ack.get("expected_sequence") is None
            or ack.get("sequence") is not None
            or ack.get("confirmed_at") is not None
        ):
            valid = False
        if ack["status"] == "confirmed" and (
            ack.get("expected_sequence") is None
            or ack.get("sequence") != ack.get("expected_sequence")
            or ack.get("confirmed_at") is None
        ):
            valid = False
        if not valid:
            raise ValueError("Invalid reboot receipt record")
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Invalid reboot receipt record") from exc


class RebootReceipts:
    def __init__(self, home: Path | None):
        self.path = Path(home) / "state/instance/reboot-receipts.json" if home else None
        self._records = None
        self._inherited_ids = set()

    def records(self):
        if self._records is None:
            records = []
            if self.path is not None and self.path.exists():
                if self.path.stat().st_size > MAX_BYTES:
                    raise ValueError("Reboot receipt storage exceeds its size limit")
                payload = json.loads(self.path.read_text(encoding="utf-8"))
                if (
                    not isinstance(payload, dict)
                    or payload.get("schema")
                    not in {1, 2, 3, 4, 5, RECEIPT_SCHEMA_VERSION}
                    or not isinstance(payload.get("records"), list)
                ):
                    raise ValueError("Invalid reboot receipt storage")
                records = payload["records"]
                if len(records) > MAX_RECORDS:
                    raise ValueError("Too many reboot receipts")
                for record in records:
                    _normalize_record(record)
                    validate_record(record)
            self._records = records
            self._inherited_ids = {record["id"] for record in records}
        return deepcopy(self._records)

    def _save(self, records):
        for record in records:
            _normalize_record(record)
            validate_record(record)
        payload = {"schema": RECEIPT_SCHEMA_VERSION, "records": records}
        expired_notices = []
        while (
            len(records) > MAX_RECORDS
            or len(json.dumps(payload, ensure_ascii=False).encode()) > MAX_BYTES
        ):
            disposable = next(
                (
                    i
                    for i, r in enumerate(records[:-1])
                    if r["status"] not in ACTIVE
                ),
                None,
            )
            if disposable is None:
                raise ValueError("Reboot receipt storage is full of pending operations")
            expired = records.pop(disposable)
            if expired["delivery"]["status"] == "pending":
                # Notification retries share bounded receipt retention, never
                # operation capacity. Expiration is not a delivery success.
                expired_notices.append(expired["id"])
        if self.path is not None:
            _write_receipt_record(self.path, payload)
        self._records = deepcopy(records)
        for operation_id in expired_notices:
            logger.warning(
                "Reboot receipt retention expired an undelivered notice: operation=%s",
                operation_id,
            )

    def get(self, operation_id):
        return next((r for r in self.records() if r["id"] == operation_id), None)

    def by_request(self, source, request_key):
        if not request_key:
            return None
        return next(
            (
                r
                for r in reversed(self.records())
                if r["source_agent"] == source and r.get("request_key") == request_key
            ),
            None,
        )

    def create(
        self,
        *,
        source,
        targets,
        display_names,
        mode,
        origin=None,
        locale="en",
        request_key=None,
    ):
        origin = clean_origin(origin)
        if request_key is not None and (
            not isinstance(request_key, str) or len(request_key) > 200
        ):
            raise ValueError("Invalid reboot request key")
        now = time.time()
        record = {
            "id": uuid4().hex,
            "request_key": request_key,
            "source_agent": source,
            "targets": list(targets),
            "display_names": display_names,
            "mode": mode,
            "origin": origin,
            "locale": locale,
            "created_at": now,
            "updated_at": now,
            "status": "accepted",
            "outcome": None,
            "observation_confidence": "unconfirmed",
            "warnings": [],
            "stage_timings_ms": {
                name: 0.0 for name in REBOOT_TIMING_STAGES
            },
            "work_counts": {name: 0 for name in REBOOT_WORK_COUNTS},
            "phase": "accepted",
            "lifecycle_state": "accepted",
            "committed": False,
            "restored": None,
            "reason": "",
            "online": {},
            "workers": {},
            "shared_replacement": _default_shared_replacement(),
            "start_delivery": _default_delivery(
                requested=origin_delivery_requested(origin)
            ),
            "presentation_ack": _default_presentation_ack(),
            "delivery": _default_delivery(requested=origin_delivery_requested(origin)),
            "progress": [
                {
                    "sequence": 1,
                    "phase": "accepted",
                    "status": "accepted",
                    "created_at": now,
                }
            ],
        }
        self._save([*self.records(), record])
        return deepcopy(record)

    def update(self, operation_id, **changes):
        records = self.records()
        record = next(r for r in records if r["id"] == operation_id)
        if "status" in changes and "lifecycle_state" not in changes:
            projected = {**record, **changes}
            changes["lifecycle_state"] = _lifecycle_for(projected)
        now = time.time()
        progress_message_id = changes.pop("progress_message_id", None)
        next_phase = changes.get("phase")
        if next_phase is not None and next_phase != record.get("phase"):
            progress = list(record.get("progress") or [])
            if len(progress) >= MAX_PROGRESS_EVENTS:
                raise ValueError("Reboot progress exceeds its event limit")
            event = {
                "sequence": len(progress) + 1,
                "phase": str(next_phase),
                "status": str(changes.get("status", record.get("status") or "")),
                "created_at": now,
            }
            if progress_message_id not in (None, ""):
                event["message_id"] = str(progress_message_id)
            progress.append(event)
            changes["progress"] = progress
        record.update(deepcopy(changes), updated_at=now)
        self._save(records)
        return deepcopy(record)

    def recover(self):
        # Do not infer a successful transaction merely because some Agents are
        # online after a new shared process starts. Never rerun accepted work.
        for record in self.records():
            if record["id"] not in self._inherited_ids:
                continue
            changes = {}
            pending_shared = (
                record.get("shared_replacement", {}).get("status") == "requested"
                and bool(record.get("shared_replacement", {}).get("request_id"))
            )
            if record["status"] in ACTIVE and not pending_shared:
                changes.update(
                    status="unconfirmed",
                    outcome="failed",
                    observation_confidence="unconfirmed",
                    phase="interrupted",
                    lifecycle_state="unconfirmed",
                    reason="interrupted",
                )
            if record["delivery"]["status"] == "pending" or pending_shared:
                changes["recovered"] = True
            if changes:
                self.update(record["id"], **changes)
        self._inherited_ids.clear()
