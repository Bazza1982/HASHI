from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from orchestrator.enterprise.store import EnterpriseStore


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


@dataclass(frozen=True)
class Artifact:
    id: str
    org_id: str
    project_id: str
    task_id: str
    type: str
    path: str
    hash: str | None
    created_at: str
    metadata: dict


class ArtifactRegistry:
    def __init__(self, store: EnterpriseStore):
        self.store = store
        self.store.init_schema()

    @classmethod
    def from_path(cls, db_path: Path | str) -> "ArtifactRegistry":
        return cls(EnterpriseStore(db_path))

    def register_artifact(
        self,
        *,
        org_id: str,
        project_id: str,
        task_id: str,
        artifact_type: str,
        path: Path | str,
        artifact_id: str | None = None,
        content_hash: str | None = None,
        metadata: dict | None = None,
    ) -> Artifact:
        artifact_path = str(path)
        artifact_id = _require_id(artifact_id or f"art-{uuid4().hex}", "artifact_id")
        if content_hash is None:
            content_hash = _hash_file_if_readable(Path(path))
        now = _utc_now_iso()
        with self.store.connect() as con:
            con.execute(
                """
                INSERT INTO artifacts(
                    id, org_id, project_id, task_id, type, path, hash,
                    created_at, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    _require_id(org_id, "org_id"),
                    _require_id(project_id, "project_id"),
                    _require_id(task_id, "task_id"),
                    _require_text(artifact_type, "artifact_type"),
                    _require_text(artifact_path, "path"),
                    _optional_text(content_hash),
                    now,
                    json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
                ),
            )
        artifact = self.get_artifact(artifact_id)
        if artifact is None:
            raise RuntimeError(f"created artifact not found: {artifact_id}")
        return artifact

    def get_artifact(self, artifact_id: str) -> Artifact | None:
        with self.store.connect() as con:
            row = con.execute("SELECT * FROM artifacts WHERE id = ?", (_require_id(artifact_id, "artifact_id"),)).fetchone()
        return _artifact_from_row(row) if row else None

    def list_artifacts(self, *, org_id: str, task_id: str | None = None, limit: int = 100) -> list[Artifact]:
        clauses = ["org_id = ?"]
        params: list = [_require_id(org_id, "org_id")]
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(_require_id(task_id, "task_id"))
        params.append(max(1, min(int(limit), 1000)))
        sql = f"""
            SELECT *
            FROM artifacts
            WHERE {' AND '.join(clauses)}
            ORDER BY created_at ASC, id ASC
            LIMIT ?
        """
        with self.store.connect() as con:
            rows = con.execute(sql, params).fetchall()
        return [_artifact_from_row(row) for row in rows]


def _artifact_from_row(row) -> Artifact:
    return Artifact(
        id=row["id"],
        org_id=row["org_id"],
        project_id=row["project_id"],
        task_id=row["task_id"],
        type=row["type"],
        path=row["path"],
        hash=row["hash"],
        created_at=row["created_at"],
        metadata=json.loads(row["metadata_json"] or "{}"),
    )


def _hash_file_if_readable(path: Path) -> str | None:
    try:
        if path.is_file():
            return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    except Exception:
        return None
    return None


def _optional_text(value) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _require_id(value: str, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized


def _require_text(value: str, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized
