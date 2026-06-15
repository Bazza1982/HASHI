from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchestrator.enterprise.audit_ledger import EnterpriseAuditLedger, LedgerEvent


@dataclass(frozen=True)
class RemoteAuditIngestResult:
    ingested: int
    skipped: int
    duplicate: int
    errors: tuple[str, ...]
    events: tuple[LedgerEvent, ...]


def ingest_remote_audit_jsonl(
    ledger: EnterpriseAuditLedger,
    path: Path | str,
    *,
    limit: int | None = None,
) -> RemoteAuditIngestResult:
    """Import Hashi Remote audit JSONL records into the unified audit ledger."""

    audit_path = Path(path)
    if not audit_path.exists():
        return RemoteAuditIngestResult(0, 0, 0, (f"missing file: {audit_path}",), ())

    events: list[LedgerEvent] = []
    errors: list[str] = []
    skipped = 0
    duplicate = 0
    seen = 0

    with audit_path.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            if limit is not None and seen >= limit:
                break
            line = raw_line.strip()
            if not line:
                skipped += 1
                continue
            seen += 1
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                errors.append(f"{audit_path}:{line_number}: invalid json: {exc.msg}")
                skipped += 1
                continue
            if not isinstance(record, dict):
                errors.append(f"{audit_path}:{line_number}: expected object")
                skipped += 1
                continue

            try:
                event = _append_remote_record(ledger, audit_path, line_number, record)
            except sqlite3.IntegrityError:
                duplicate += 1
                continue
            except Exception as exc:
                errors.append(f"{audit_path}:{line_number}: {exc}")
                skipped += 1
                continue
            events.append(event)

    return RemoteAuditIngestResult(
        ingested=len(events),
        skipped=skipped,
        duplicate=duplicate,
        errors=tuple(errors),
        events=tuple(events),
    )


def _append_remote_record(
    ledger: EnterpriseAuditLedger,
    audit_path: Path,
    line_number: int,
    record: dict[str, Any],
) -> LedgerEvent:
    event_name = _text_or_unknown(record.get("event"))
    context = {
        "legacy_source": "remote_audit",
        "legacy_path": str(audit_path),
        "legacy_line": line_number,
        **{key: value for key, value in record.items() if key != "ts"},
    }
    return ledger.append(
        event_type="remote",
        action=f"remote.{event_name}",
        status=_status(event_name, record),
        actor_id=record.get("client") or record.get("from") or record.get("instance"),
        context=context,
        event_id=_event_id(audit_path, line_number, record),
        ts=_normalize_ts(record.get("ts")),
    )


def _status(event_name: str, record: dict[str, Any]) -> str:
    if event_name == "terminal_exec" and "allowed" in record:
        return "allowed" if bool(record.get("allowed")) else "denied"
    if event_name == "pairing_request" and "auto_approved" in record:
        return "approved" if bool(record.get("auto_approved")) else "pending"
    return "observed"


def _event_id(audit_path: Path, line_number: int, record: dict[str, Any]) -> str:
    seed = json.dumps(
        {
            "path": str(audit_path.resolve()),
            "line": line_number,
            "ts": record.get("ts"),
            "event": record.get("event"),
            "client": record.get("client"),
            "from": record.get("from"),
            "to_agent": record.get("to_agent"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return f"remote-{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:32]}"


def _normalize_ts(value: Any) -> str | None:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    normalized = str(value or "").strip()
    return normalized or None


def _text_or_unknown(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return normalized or "unknown"
