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
class BrowserAuditIngestResult:
    ingested: int
    skipped: int
    duplicate: int
    errors: tuple[str, ...]
    events: tuple[LedgerEvent, ...]


def ingest_browser_action_audit_jsonl(
    ledger: EnterpriseAuditLedger,
    path: Path | str,
    *,
    limit: int | None = None,
) -> BrowserAuditIngestResult:
    """Import browser action audit JSONL records into the unified audit ledger."""

    audit_path = Path(path)
    if not audit_path.exists():
        return BrowserAuditIngestResult(0, 0, 0, (f"missing file: {audit_path}",), ())

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
                event = _append_browser_record(ledger, audit_path, line_number, record)
            except sqlite3.IntegrityError:
                duplicate += 1
                continue
            except Exception as exc:
                errors.append(f"{audit_path}:{line_number}: {exc}")
                skipped += 1
                continue
            events.append(event)

    return BrowserAuditIngestResult(
        ingested=len(events),
        skipped=skipped,
        duplicate=duplicate,
        errors=tuple(errors),
        events=tuple(events),
    )


def _append_browser_record(
    ledger: EnterpriseAuditLedger,
    audit_path: Path,
    line_number: int,
    record: dict[str, Any],
) -> LedgerEvent:
    action = _text_or_unknown(record.get("action"))
    context = {
        "legacy_source": "browser_action_audit",
        "legacy_path": str(audit_path),
        "legacy_line": line_number,
        **{key: value for key, value in record.items() if key != "ts"},
    }
    return ledger.append(
        event_type="tool",
        action=f"browser.{action}",
        status=_status(record),
        actor_id=_actor_id(record),
        context=context,
        request_id=_optional_text(record.get("request_id")),
        correlation_id=_optional_text(record.get("session_id")),
        event_id=_event_id(audit_path, line_number, record),
        ts=_normalize_ts(record.get("ts")),
    )


def _status(record: dict[str, Any]) -> str:
    response = record.get("response")
    if isinstance(response, dict) and "ok" in response:
        return "success" if bool(response.get("ok")) else "failed"
    return "observed"


def _actor_id(record: dict[str, Any]) -> str | None:
    args = record.get("args")
    if isinstance(args, dict):
        audit = args.get("_audit")
        if isinstance(audit, dict) and audit.get("agent_name"):
            return str(audit.get("agent_name"))
        for key in ("agent_name", "owner"):
            if args.get(key):
                return str(args.get(key))
    return None


def _event_id(audit_path: Path, line_number: int, record: dict[str, Any]) -> str:
    seed = json.dumps(
        {
            "path": str(audit_path.resolve()),
            "line": line_number,
            "ts": record.get("ts"),
            "kind": record.get("kind"),
            "action": record.get("action"),
            "request_id": record.get("request_id"),
            "session_id": record.get("session_id"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return f"browser-{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:32]}"


def _normalize_ts(value: Any) -> str | None:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    normalized = str(value or "").strip()
    return normalized or None


def _optional_text(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _text_or_unknown(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return normalized or "unknown"
