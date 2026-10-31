"""Durable adapters over Canonical Audit; never a second audit history."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import uuid

from orchestrator.canonical_audit import CanonicalAuditStore, BufferedCanonicalAuditWriter
from .models import FileVersion, FingerprintPolicy, RunIdentity, SCHEMA, relative_target


class FingerprintUnavailable(RuntimeError):
    """Required provenance could not be confirmed. Do not replay a committed write."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class FingerprintAudit:
    def __init__(self, writer):
        self.writer = writer
        self.store = writer.store if isinstance(writer, BufferedCanonicalAuditWriter) else writer

    def validate_identity(self, identity: RunIdentity):
        if not isinstance(identity, RunIdentity) or not isinstance(self.store, CanonicalAuditStore):
            raise FingerprintUnavailable("trusted Run identity and Canonical Audit are required")
        if (identity.instance_id, identity.agent_id) != (self.store.instance_id, self.store.agent_id):
            raise FingerprintUnavailable("Run identity does not match Canonical Audit owner")

    def append(self, event_type: str, payload: dict, *, request_id: str | None = None) -> str:
        if not isinstance(self.writer, (CanonicalAuditStore, BufferedCanonicalAuditWriter)):
            raise FingerprintUnavailable("Canonical Audit is unavailable")
        try:
            event_id = self.writer.record(event_type, payload, request_id=request_id)
            if isinstance(self.writer, BufferedCanonicalAuditWriter):
                self.writer.flush()
            if not event_id:
                raise OSError("missing persisted event ID")
            return event_id
        except Exception as exc:
            raise FingerprintUnavailable("fingerprint audit durability was not confirmed") from exc

    def record_operation(
        self, *, policy: FingerprintPolicy, identity: RunIdentity | None,
        tool_call_id: str, tool_name: str, artifact_id: str, root_id: str,
        relative_path: str, action: str, before: FileVersion, after: FileVersion,
        business_status: str, operation_id: str | None = None,
        evidence_grade: str = "controlled_execution", marker_status: str = "not_implemented",
        started_at: str | None = None, permission_receipt: str | None = None,
        coverage: dict | None = None, prepared_event_id: str | None = None,
        marker_changed_bytes: bool = False, business_changed_bytes: bool | None = None,
    ) -> dict | None:
        if not policy.enabled:
            return None
        self.validate_identity(identity)
        if any(not isinstance(value, str) or not value for value in (tool_call_id, tool_name, artifact_id, root_id)):
            raise ValueError("operation, tool and artifact identities are required")
        target = relative_target(relative_path)
        if evidence_grade not in {"controlled_execution", "engine_reported", "interval_observation", "unknown"}:
            raise ValueError("invalid evidence grade")
        payload = {
            "schema": SCHEMA, "operation_id": operation_id or str(uuid.uuid4()),
            "identity": asdict(identity), "tool_call_id": tool_call_id, "tool_name": tool_name,
            "artifact_id": artifact_id, "root_id": root_id, "relative_path": target,
            "action": action, "before": asdict(before), "after": asdict(after),
            "business_status": business_status, "evidence_status": "persisted",
            "marker_status": marker_status, "evidence_grade": evidence_grade,
            "policy_version": policy.version, "started_at": started_at or utc_now(),
            "observed_at": utc_now(), "permission_receipt": permission_receipt,
            "coverage": coverage or {}, "prepared_event_id": prepared_event_id,
            "marker_changed_bytes": marker_changed_bytes, "business_changed_bytes": business_changed_bytes,
        }
        event_id = self.append("fingerprint_receipt", payload, request_id=identity.request_id)
        return {**payload, "event_id": event_id}
