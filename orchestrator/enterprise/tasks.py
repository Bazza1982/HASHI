from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from uuid import uuid4

from orchestrator.enterprise.store import EnterpriseStore


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


class TaskStatus(str, Enum):
    DELEGATED = "delegated"
    IN_PROGRESS = "in_progress"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"

    @classmethod
    def from_value(cls, value: str | "TaskStatus") -> "TaskStatus":
        if isinstance(value, cls):
            return value
        normalized = str(value or "").strip().lower()
        for status in cls:
            if status.value == normalized:
                return status
        raise ValueError(f"unsupported task status: {value!r}")


@dataclass(frozen=True)
class EnterpriseTask:
    id: str
    org_id: str
    project_id: str
    user_id: str | None
    agent_id: str | None
    status: str
    prompt_summary: str
    created_at: str
    updated_at: str
    completed_at: str | None
    failed_reason: str | None
    metadata: dict


class TaskRegistry:
    def __init__(self, store: EnterpriseStore):
        self.store = store
        self.store.init_schema()

    @classmethod
    def from_path(cls, db_path: Path | str) -> "TaskRegistry":
        return cls(EnterpriseStore(db_path))

    def create_task(
        self,
        *,
        org_id: str,
        project_id: str,
        prompt_summary: str,
        user_id: str | None = None,
        agent_id: str | None = None,
        task_id: str | None = None,
        metadata: dict | None = None,
    ) -> EnterpriseTask:
        now = _utc_now_iso()
        task_id = _require_id(task_id or f"task-{uuid4().hex}", "task_id")
        payload = _json_dumps(metadata or {})
        with self.store.connect() as con:
            con.execute(
                """
                INSERT INTO tasks(
                    id, org_id, project_id, user_id, agent_id, status,
                    prompt_summary, created_at, updated_at, completed_at,
                    failed_reason, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)
                """,
                (
                    task_id,
                    _require_id(org_id, "org_id"),
                    _require_id(project_id, "project_id"),
                    _optional_text(user_id),
                    _optional_text(agent_id),
                    TaskStatus.DELEGATED.value,
                    _require_text(prompt_summary, "prompt_summary"),
                    now,
                    now,
                    payload,
                ),
            )
        task = self.get_task(task_id)
        if task is None:
            raise RuntimeError(f"created task not found: {task_id}")
        return task

    def get_task(self, task_id: str) -> EnterpriseTask | None:
        with self.store.connect() as con:
            row = con.execute("SELECT * FROM tasks WHERE id = ?", (_require_id(task_id, "task_id"),)).fetchone()
        return _task_from_row(row) if row else None

    def list_tasks(
        self,
        *,
        org_id: str,
        project_id: str | None = None,
        status: TaskStatus | str | None = None,
        limit: int = 100,
    ) -> list[EnterpriseTask]:
        clauses = ["org_id = ?"]
        params: list = [_require_id(org_id, "org_id")]
        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(_require_id(project_id, "project_id"))
        if status is not None:
            clauses.append("status = ?")
            params.append(TaskStatus.from_value(status).value)
        params.append(max(1, min(int(limit), 1000)))
        sql = f"""
            SELECT *
            FROM tasks
            WHERE {' AND '.join(clauses)}
            ORDER BY created_at DESC, id DESC
            LIMIT ?
        """
        with self.store.connect() as con:
            rows = con.execute(sql, params).fetchall()
        return [_task_from_row(row) for row in rows]

    def transition_task(
        self,
        task_id: str,
        status: TaskStatus | str,
        *,
        failed_reason: str | None = None,
    ) -> EnterpriseTask:
        next_status = TaskStatus.from_value(status)
        now = _utc_now_iso()
        completed_at = now if next_status in {TaskStatus.COMPLETED, TaskStatus.FAILED} else None
        reason = _optional_text(failed_reason) if next_status == TaskStatus.FAILED else None
        with self.store.connect() as con:
            cur = con.execute(
                """
                UPDATE tasks
                SET status = ?, updated_at = ?, completed_at = ?, failed_reason = ?
                WHERE id = ?
                """,
                (next_status.value, now, completed_at, reason, _require_id(task_id, "task_id")),
            )
            if cur.rowcount == 0:
                raise ValueError(f"unknown task: {task_id!r}")
        task = self.get_task(task_id)
        if task is None:
            raise RuntimeError(f"updated task not found: {task_id}")
        return task


def _task_from_row(row) -> EnterpriseTask:
    return EnterpriseTask(
        id=row["id"],
        org_id=row["org_id"],
        project_id=row["project_id"],
        user_id=row["user_id"],
        agent_id=row["agent_id"],
        status=row["status"],
        prompt_summary=row["prompt_summary"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        completed_at=row["completed_at"],
        failed_reason=row["failed_reason"],
        metadata=json.loads(row["metadata_json"] or "{}"),
    )


def _json_dumps(value: dict) -> str:
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True)


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
