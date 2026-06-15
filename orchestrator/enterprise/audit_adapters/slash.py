from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestrator.enterprise.audit_ledger import EnterpriseAuditLedger, LedgerEvent


@dataclass(frozen=True)
class SlashAuditIngestResult:
    ingested: int
    skipped: int
    duplicate: int
    errors: tuple[str, ...]
    events: tuple[LedgerEvent, ...]


def ingest_slash_command_audit_jsonl(
    ledger: EnterpriseAuditLedger,
    path: Path | str,
    *,
    limit: int | None = None,
) -> SlashAuditIngestResult:
    """Import legacy slash-command JSONL audit records into the unified ledger.

    The adapter uses deterministic event IDs derived from file path, line number,
    timestamp, command, and status so migration/backfill jobs can be safely rerun.
    """

    audit_path = Path(path)
    if not audit_path.exists():
        return SlashAuditIngestResult(0, 0, 0, (f"missing file: {audit_path}",), ())

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
                event = _append_slash_record(ledger, audit_path, line_number, record)
            except sqlite3.IntegrityError:
                duplicate += 1
                continue
            except Exception as exc:
                errors.append(f"{audit_path}:{line_number}: {exc}")
                skipped += 1
                continue
            events.append(event)

    return SlashAuditIngestResult(
        ingested=len(events),
        skipped=skipped,
        duplicate=duplicate,
        errors=tuple(errors),
        events=tuple(events),
    )


def _append_slash_record(
    ledger: EnterpriseAuditLedger,
    audit_path: Path,
    line_number: int,
    record: dict[str, Any],
) -> LedgerEvent:
    command_name = _text_or_unknown(record.get("command_name"))
    status = _text_or_unknown(record.get("status"))
    context = {
        "legacy_source": "slash_command_audit",
        "legacy_path": str(audit_path),
        "legacy_line": line_number,
        "agent": record.get("agent"),
        "command_name": command_name,
        "args_redacted": _list(record.get("args_redacted")),
        "source_channel": record.get("source_channel"),
        "handler_kind": record.get("handler_kind"),
        "duration_ms": record.get("duration_ms"),
        "chat_id": record.get("chat_id"),
        "error": record.get("error"),
        "blocked_reason": record.get("blocked_reason"),
        "side_effects": _list(record.get("side_effects")),
    }
    return ledger.append(
        event_type="slash_command",
        action=f"slash.{command_name}",
        status=status,
        actor_id=record.get("actor_id"),
        context=context,
        event_id=_event_id(audit_path, line_number, record),
        ts=record.get("ts"),
    )


def _event_id(audit_path: Path, line_number: int, record: dict[str, Any]) -> str:
    seed = json.dumps(
        {
            "path": str(audit_path.resolve()),
            "line": line_number,
            "ts": record.get("ts"),
            "command_name": record.get("command_name"),
            "status": record.get("status"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return f"slash-{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:32]}"


def _text_or_unknown(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return normalized or "unknown"


def _list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]
