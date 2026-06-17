from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from orchestrator.enterprise.audit_ledger import LedgerEvent


def format_siem_event(event: LedgerEvent) -> dict[str, Any]:
    outcome = _event_outcome(event.status)
    return {
        "@timestamp": event.ts,
        "event": {
            "id": event.id,
            "kind": "event",
            "category": [_event_category(event.event_type)],
            "type": [_event_type(event.status)],
            "action": event.action,
            "outcome": outcome,
        },
        "organization": {"id": event.org_id},
        "user": {"id": event.actor_id} if event.actor_id else {},
        "trace": {"id": event.correlation_id} if event.correlation_id else {},
        "labels": {
            "hashi.audit.schema_version": event.schema_version,
            "hashi.audit.event_type": event.event_type,
            "hashi.audit.status": event.status,
            "hashi.audit.project_id": event.project_id,
            "hashi.audit.task_id": event.task_id,
            "hashi.audit.request_id": event.request_id,
            "hashi.audit.parent_event_id": event.parent_event_id,
            "hashi.audit.chain_index": event.chain_index,
            "hashi.audit.prev_hash": event.prev_hash,
            "hashi.audit.event_hash": event.event_hash,
        },
        "hashi": {"audit": {"context": dict(event.context or {})}},
    }


def format_otel_log(event: LedgerEvent) -> dict[str, Any]:
    return {
        "time_unix_nano": _time_unix_nano(event.ts),
        "severity_text": _severity_text(event.status),
        "severity_number": _severity_number(event.status),
        "body": f"{event.event_type}.{event.action} {event.status}",
        "attributes": {
            "hashi.audit.id": event.id,
            "hashi.audit.org_id": event.org_id,
            "hashi.audit.schema_version": event.schema_version,
            "hashi.audit.event_type": event.event_type,
            "hashi.audit.actor_id": event.actor_id,
            "hashi.audit.action": event.action,
            "hashi.audit.status": event.status,
            "hashi.audit.project_id": event.project_id,
            "hashi.audit.task_id": event.task_id,
            "hashi.audit.request_id": event.request_id,
            "hashi.audit.correlation_id": event.correlation_id,
            "hashi.audit.parent_event_id": event.parent_event_id,
            "hashi.audit.chain_index": event.chain_index,
            "hashi.audit.prev_hash": event.prev_hash,
            "hashi.audit.event_hash": event.event_hash,
            "hashi.audit.context": dict(event.context or {}),
        },
    }


def format_splunk_hec_event(event: LedgerEvent) -> dict[str, Any]:
    return {
        "time": _time_unix_nano(event.ts) // 1_000_000_000,
        "host": "hashi",
        "source": "hashi.enterprise.audit",
        "sourcetype": "_json",
        "event": format_siem_event(event),
        "fields": {
            "hashi_audit_id": event.id,
            "hashi_org_id": event.org_id,
            "hashi_event_type": event.event_type,
            "hashi_action": event.action,
            "hashi_status": event.status,
            "hashi_chain_index": event.chain_index,
            "hashi_event_hash": event.event_hash,
        },
    }


def format_elastic_bulk_action(event: LedgerEvent) -> dict[str, Any]:
    return {"create": {"_id": event.id}}


def _event_category(event_type: str) -> str:
    return {
        "auth": "authentication",
        "admin_api": "configuration",
        "policy": "configuration",
        "channel": "network",
        "connector": "api",
        "tool": "process",
        "model_invocation": "api",
        "remote": "network",
        "slash_command": "configuration",
    }.get(event_type, "configuration")


def _event_type(status: str) -> str:
    normalized = (status or "").strip().lower()
    if normalized in {"denied", "blocked", "failed", "error"}:
        return "denied" if normalized in {"denied", "blocked"} else "error"
    if normalized in {"success", "completed"}:
        return "info"
    return "info"


def _event_outcome(status: str) -> str:
    normalized = (status or "").strip().lower()
    if normalized in {"success", "completed", "observed"}:
        return "success"
    if normalized in {"denied", "blocked", "failed", "error"}:
        return "failure"
    return "unknown"


def _severity_text(status: str) -> str:
    normalized = (status or "").strip().lower()
    if normalized in {"failed", "error"}:
        return "ERROR"
    if normalized in {"denied", "blocked", "approval_required"}:
        return "WARN"
    return "INFO"


def _severity_number(status: str) -> int:
    text = _severity_text(status)
    if text == "ERROR":
        return 17
    if text == "WARN":
        return 13
    return 9


def _time_unix_nano(ts: str) -> int:
    try:
        parsed = datetime.fromisoformat(ts)
    except ValueError:
        parsed = datetime.now(tz=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1_000_000_000)
