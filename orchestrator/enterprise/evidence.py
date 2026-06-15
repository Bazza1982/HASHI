from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from orchestrator.enterprise.artifacts import ArtifactRegistry
from orchestrator.enterprise.audit_ledger import EnterpriseAuditLedger
from orchestrator.enterprise.store import EnterpriseStore


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


@dataclass(frozen=True)
class EvidenceBundle:
    id: str
    org_id: str
    task_id: str
    audit_event_ids: tuple[str, ...]
    artifact_ids: tuple[str, ...]
    created_at: str
    metadata: dict


class EvidenceBundleRegistry:
    def __init__(self, store: EnterpriseStore):
        self.store = store
        self.store.init_schema()

    @classmethod
    def from_path(cls, db_path: Path | str) -> "EvidenceBundleRegistry":
        return cls(EnterpriseStore(db_path))

    def create_bundle(
        self,
        *,
        org_id: str,
        task_id: str,
        audit_event_ids: list[str] | tuple[str, ...],
        artifact_ids: list[str] | tuple[str, ...],
        bundle_id: str | None = None,
        metadata: dict | None = None,
    ) -> EvidenceBundle:
        now = _utc_now_iso()
        bundle_id = _require_id(bundle_id or f"evb-{uuid4().hex}", "bundle_id")
        audit_ids = tuple(str(item) for item in audit_event_ids)
        artifact_ids_t = tuple(str(item) for item in artifact_ids)
        with self.store.connect() as con:
            con.execute(
                """
                INSERT INTO evidence_bundles(
                    id, org_id, task_id, audit_event_ids_json, artifact_ids_json,
                    created_at, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    bundle_id,
                    _require_id(org_id, "org_id"),
                    _require_id(task_id, "task_id"),
                    json.dumps(list(audit_ids), ensure_ascii=False),
                    json.dumps(list(artifact_ids_t), ensure_ascii=False),
                    now,
                    json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
                ),
            )
        bundle = self.get_bundle(bundle_id)
        if bundle is None:
            raise RuntimeError(f"created evidence bundle not found: {bundle_id}")
        return bundle

    def build_for_task(
        self,
        *,
        ledger: EnterpriseAuditLedger,
        artifacts: ArtifactRegistry,
        org_id: str,
        task_id: str,
        limit: int = 1000,
        metadata: dict | None = None,
    ) -> EvidenceBundle:
        audit_events = ledger.query(task_id=task_id, limit=limit)
        task_artifacts = artifacts.list_artifacts(org_id=org_id, task_id=task_id, limit=limit)
        return self.create_bundle(
            org_id=org_id,
            task_id=task_id,
            audit_event_ids=[event.id for event in audit_events],
            artifact_ids=[artifact.id for artifact in task_artifacts],
            metadata=metadata or {"source": "build_for_task"},
        )

    def get_bundle(self, bundle_id: str) -> EvidenceBundle | None:
        with self.store.connect() as con:
            row = con.execute(
                "SELECT * FROM evidence_bundles WHERE id = ?",
                (_require_id(bundle_id, "bundle_id"),),
            ).fetchone()
        return _bundle_from_row(row) if row else None


def _bundle_from_row(row) -> EvidenceBundle:
    return EvidenceBundle(
        id=row["id"],
        org_id=row["org_id"],
        task_id=row["task_id"],
        audit_event_ids=tuple(json.loads(row["audit_event_ids_json"] or "[]")),
        artifact_ids=tuple(json.loads(row["artifact_ids_json"] or "[]")),
        created_at=row["created_at"],
        metadata=json.loads(row["metadata_json"] or "{}"),
    )


def _require_id(value: str, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized
