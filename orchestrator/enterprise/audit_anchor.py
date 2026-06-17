from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchestrator.enterprise.audit_ledger import EnterpriseAuditLedger


AUDIT_ANCHOR_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class AuditLedgerAnchor:
    org_id: str
    created_at: str
    schema_version: int
    chain_start_index: int
    chain_end_index: int
    event_count: int
    start_event_hash: str
    end_event_hash: str
    anchor_hash: str
    label: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "org_id": self.org_id,
            "created_at": self.created_at,
            "schema_version": self.schema_version,
            "chain_start_index": self.chain_start_index,
            "chain_end_index": self.chain_end_index,
            "event_count": self.event_count,
            "start_event_hash": self.start_event_hash,
            "end_event_hash": self.end_event_hash,
            "anchor_hash": self.anchor_hash,
            "label": self.label,
        }


@dataclass(frozen=True)
class AuditAnchorVerification:
    ok: bool
    errors: tuple[str, ...]


def create_audit_ledger_anchor(
    ledger: EnterpriseAuditLedger,
    *,
    label: str | None = None,
    created_at: str | None = None,
) -> AuditLedgerAnchor:
    verified = ledger.verify_chain()
    if not verified.ok:
        raise ValueError("audit ledger chain verification failed")
    rows = _anchor_rows(ledger)
    if not rows:
        raise ValueError("cannot anchor an empty audit ledger")
    first = rows[0]
    last = rows[-1]
    payload = {
        "org_id": ledger.org_id,
        "created_at": created_at or datetime.now(tz=timezone.utc).isoformat(),
        "schema_version": AUDIT_ANCHOR_SCHEMA_VERSION,
        "chain_start_index": int(first["chain_index"]),
        "chain_end_index": int(last["chain_index"]),
        "event_count": len(rows),
        "start_event_hash": str(first["event_hash"]),
        "end_event_hash": str(last["event_hash"]),
        "label": _optional_text(label),
    }
    return AuditLedgerAnchor(anchor_hash=_anchor_hash(payload), **payload)


def verify_audit_ledger_anchor(
    ledger: EnterpriseAuditLedger,
    anchor: AuditLedgerAnchor | dict[str, Any],
) -> AuditAnchorVerification:
    anchor = load_audit_ledger_anchor(anchor) if isinstance(anchor, dict) else anchor
    errors: list[str] = []
    if anchor.anchor_hash != _anchor_hash(_anchor_payload(anchor)):
        errors.append("anchor_hash mismatch")
    if anchor.org_id != ledger.org_id:
        errors.append("org_id mismatch")
    chain = ledger.verify_chain()
    if not chain.ok:
        errors.extend(f"chain: {error}" for error in chain.errors)
    rows = _anchor_rows(ledger, end_index=anchor.chain_end_index)
    if len(rows) != anchor.event_count:
        errors.append(f"event_count mismatch: expected {anchor.event_count}, got {len(rows)}")
    if rows:
        first = rows[0]
        last = rows[-1]
        if int(first["chain_index"]) != anchor.chain_start_index:
            errors.append("chain_start_index mismatch")
        if str(first["event_hash"]) != anchor.start_event_hash:
            errors.append("start_event_hash mismatch")
        if int(last["chain_index"]) != anchor.chain_end_index:
            errors.append("chain_end_index mismatch")
        if str(last["event_hash"]) != anchor.end_event_hash:
            errors.append("end_event_hash mismatch")
    return AuditAnchorVerification(ok=not errors, errors=tuple(errors))


def export_audit_ledger_anchor(
    ledger: EnterpriseAuditLedger,
    path: Path | str,
    *,
    label: str | None = None,
) -> Path:
    anchor = create_audit_ledger_anchor(ledger, label=label)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(anchor.to_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return path


def load_audit_ledger_anchor(value: Path | str | dict[str, Any]) -> AuditLedgerAnchor:
    if isinstance(value, (str, Path)):
        raw = json.loads(Path(value).read_text(encoding="utf-8"))
    else:
        raw = dict(value)
    return AuditLedgerAnchor(
        org_id=str(raw["org_id"]),
        created_at=str(raw["created_at"]),
        schema_version=int(raw["schema_version"]),
        chain_start_index=int(raw["chain_start_index"]),
        chain_end_index=int(raw["chain_end_index"]),
        event_count=int(raw["event_count"]),
        start_event_hash=str(raw["start_event_hash"]),
        end_event_hash=str(raw["end_event_hash"]),
        anchor_hash=str(raw["anchor_hash"]),
        label=_optional_text(raw.get("label")),
    )


def _anchor_rows(ledger: EnterpriseAuditLedger, *, end_index: int | None = None):
    clauses = ["org_id = ?", "chain_index IS NOT NULL", "event_hash IS NOT NULL"]
    params: list[Any] = [ledger.org_id]
    if end_index is not None:
        clauses.append("chain_index <= ?")
        params.append(int(end_index))
    with ledger.store.connect() as con:
        return con.execute(
            f"""
            SELECT chain_index, event_hash
            FROM audit_events
            WHERE {' AND '.join(clauses)}
            ORDER BY chain_index ASC
            """,
            tuple(params),
        ).fetchall()


def _anchor_payload(anchor: AuditLedgerAnchor) -> dict[str, Any]:
    payload = anchor.to_dict()
    payload.pop("anchor_hash", None)
    return payload


def _anchor_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _optional_text(value) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None
