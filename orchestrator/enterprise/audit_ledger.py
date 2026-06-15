from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from orchestrator.enterprise.audit_schema import AuditEvent
from orchestrator.enterprise.store import EnterpriseStore


AUDIT_LEDGER_SCHEMA_VERSION = 1


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


@dataclass(frozen=True)
class LedgerEvent:
    id: str
    org_id: str
    ts: str
    schema_version: int
    event_type: str
    actor_id: str | None
    action: str
    status: str
    context: dict
    project_id: str | None = None
    task_id: str | None = None
    request_id: str | None = None
    correlation_id: str | None = None
    parent_event_id: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "org_id": self.org_id,
            "ts": self.ts,
            "schema_version": self.schema_version,
            "event_type": self.event_type,
            "actor_id": self.actor_id,
            "action": self.action,
            "status": self.status,
            "project_id": self.project_id,
            "task_id": self.task_id,
            "request_id": self.request_id,
            "correlation_id": self.correlation_id,
            "parent_event_id": self.parent_event_id,
            "context": dict(self.context or {}),
        }


class EnterpriseAuditLedger:
    def __init__(self, store: EnterpriseStore, *, org_id: str):
        self.store = store
        self.store.init_schema()
        self.org_id = _require_id(org_id, "org_id")

    @classmethod
    def from_path(cls, db_path: Path | str, *, org_id: str) -> "EnterpriseAuditLedger":
        return cls(EnterpriseStore(db_path), org_id=org_id)

    def append(
        self,
        *,
        event_type: str,
        action: str,
        status: str,
        actor_id: str | int | None = None,
        context: dict | None = None,
        project_id: str | None = None,
        task_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
        parent_event_id: str | None = None,
        event_id: str | None = None,
        ts: str | None = None,
    ) -> LedgerEvent:
        event = LedgerEvent(
            id=_require_id(event_id or f"audit-{uuid4().hex}", "event_id"),
            org_id=self.org_id,
            ts=ts or _utc_now_iso(),
            schema_version=AUDIT_LEDGER_SCHEMA_VERSION,
            event_type=_require_text(event_type, "event_type"),
            actor_id=str(actor_id) if actor_id is not None else None,
            action=_require_text(action, "action"),
            status=_require_text(status, "status"),
            project_id=_optional_text(project_id),
            task_id=_optional_text(task_id),
            request_id=_optional_text(request_id),
            correlation_id=_optional_text(correlation_id),
            parent_event_id=_optional_text(parent_event_id),
            context=_json_safe_context(context or {}),
        )
        with self.store.connect() as con:
            con.execute(
                """
                INSERT INTO audit_events(
                    id, org_id, ts, schema_version, event_type, actor_id, action,
                    status, project_id, task_id, request_id, correlation_id,
                    parent_event_id, context_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.id,
                    event.org_id,
                    event.ts,
                    event.schema_version,
                    event.event_type,
                    event.actor_id,
                    event.action,
                    event.status,
                    event.project_id,
                    event.task_id,
                    event.request_id,
                    event.correlation_id,
                    event.parent_event_id,
                    json.dumps(event.context, ensure_ascii=False, sort_keys=True),
                ),
            )
        return event

    def append_audit_event(
        self,
        event: AuditEvent,
        *,
        project_id: str | None = None,
        task_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
        parent_event_id: str | None = None,
    ) -> LedgerEvent:
        return self.append(
            event_type=event.event_type,
            actor_id=event.actor_id,
            action=event.action,
            status=event.status,
            context=dict(event.context or {}),
            project_id=project_id,
            task_id=task_id,
            request_id=request_id,
            correlation_id=correlation_id,
            parent_event_id=parent_event_id,
            ts=event.ts,
        )

    def query(
        self,
        *,
        event_type: str | None = None,
        actor_id: str | int | None = None,
        project_id: str | None = None,
        task_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
        limit: int = 100,
    ) -> list[LedgerEvent]:
        clauses = ["org_id = ?"]
        params: list = [self.org_id]
        for column, value in (
            ("event_type", event_type),
            ("actor_id", str(actor_id) if actor_id is not None else None),
            ("project_id", project_id),
            ("task_id", task_id),
            ("request_id", request_id),
            ("correlation_id", correlation_id),
        ):
            if value is None:
                continue
            clauses.append(f"{column} = ?")
            params.append(str(value))
        params.append(max(1, min(int(limit), 1000)))
        sql = f"""
            SELECT *
            FROM audit_events
            WHERE {' AND '.join(clauses)}
            ORDER BY ts ASC, id ASC
            LIMIT ?
        """
        with self.store.connect() as con:
            rows = con.execute(sql, params).fetchall()
        return [_event_from_row(row) for row in rows]

    def export_jsonl(
        self,
        path: Path | str,
        *,
        event_type: str | None = None,
        actor_id: str | int | None = None,
        project_id: str | None = None,
        task_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
        limit: int = 1000,
    ) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        events = self.query(
            event_type=event_type,
            actor_id=actor_id,
            project_id=project_id,
            task_id=task_id,
            request_id=request_id,
            correlation_id=correlation_id,
            limit=limit,
        )
        with path.open("w", encoding="utf-8") as handle:
            for event in events:
                handle.write(json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")
        return path


def _event_from_row(row) -> LedgerEvent:
    return LedgerEvent(
        id=row["id"],
        org_id=row["org_id"],
        ts=row["ts"],
        schema_version=int(row["schema_version"]),
        event_type=row["event_type"],
        actor_id=row["actor_id"],
        action=row["action"],
        status=row["status"],
        project_id=row["project_id"],
        task_id=row["task_id"],
        request_id=row["request_id"],
        correlation_id=row["correlation_id"],
        parent_event_id=row["parent_event_id"],
        context=json.loads(row["context_json"] or "{}"),
    )


def _json_safe_context(context: dict) -> dict:
    return {str(key): _json_safe_value(value) for key, value in (context or {}).items()}


def _json_safe_value(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe_value(item) for item in value]
    return repr(value)


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
