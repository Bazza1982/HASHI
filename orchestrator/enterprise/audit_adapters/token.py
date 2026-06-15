from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestrator.enterprise.audit_ledger import EnterpriseAuditLedger, LedgerEvent


@dataclass(frozen=True)
class TokenAuditIngestResult:
    ingested: int
    skipped: int
    duplicate: int
    errors: tuple[str, ...]
    events: tuple[LedgerEvent, ...]


def ingest_token_audit_jsonl(
    ledger: EnterpriseAuditLedger,
    path: Path | str,
    *,
    limit: int | None = None,
) -> TokenAuditIngestResult:
    """Import legacy token audit records into the unified audit ledger."""

    audit_path = Path(path)
    if not audit_path.exists():
        return TokenAuditIngestResult(0, 0, 0, (f"missing file: {audit_path}",), ())

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
                event = _append_token_record(ledger, audit_path, line_number, record)
            except sqlite3.IntegrityError:
                duplicate += 1
                continue
            except Exception as exc:
                errors.append(f"{audit_path}:{line_number}: {exc}")
                skipped += 1
                continue
            events.append(event)

    return TokenAuditIngestResult(
        ingested=len(events),
        skipped=skipped,
        duplicate=duplicate,
        errors=tuple(errors),
        events=tuple(events),
    )


def _append_token_record(
    ledger: EnterpriseAuditLedger,
    audit_path: Path,
    line_number: int,
    record: dict[str, Any],
) -> LedgerEvent:
    context = {
        "legacy_source": "token_audit",
        "legacy_path": str(audit_path),
        "legacy_line": line_number,
        **{key: value for key, value in record.items() if key != "ts"},
    }
    return ledger.append(
        event_type="model_invocation",
        action="model.invoke",
        status=_status(record),
        context=context,
        request_id=_optional_text(record.get("request_id")),
        correlation_id=_optional_text(record.get("request_fingerprint")),
        event_id=_event_id(audit_path, line_number, record),
        ts=record.get("ts"),
    )


def _status(record: dict[str, Any]) -> str:
    if "success" not in record:
        return "unknown"
    return "success" if bool(record.get("success")) else "failed"


def _event_id(audit_path: Path, line_number: int, record: dict[str, Any]) -> str:
    seed = json.dumps(
        {
            "path": str(audit_path.resolve()),
            "line": line_number,
            "ts": record.get("ts"),
            "request_id": record.get("request_id"),
            "backend": record.get("backend"),
            "model": record.get("model"),
            "success": record.get("success"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return f"token-{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:32]}"


def _optional_text(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None
