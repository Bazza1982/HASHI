from __future__ import annotations

import json
import sqlite3

from orchestrator.enterprise import (
    EnterpriseAuditLedger,
    IdentityService,
    create_audit_ledger_anchor,
    export_audit_ledger_anchor,
    load_audit_ledger_anchor,
    verify_audit_ledger_anchor,
)


def _ledger(tmp_path) -> EnterpriseAuditLedger:
    db_path = tmp_path / "state" / "enterprise.sqlite"
    IdentityService.from_path(db_path).create_organization(org_id="ORG-001", name="Acme")
    return EnterpriseAuditLedger.from_path(db_path, org_id="ORG-001")


def _append_events(ledger: EnterpriseAuditLedger):
    first = ledger.append(event_type="policy", action="file.write", status="denied", context={"path": "a.txt"})
    second = ledger.append(event_type="channel", action="channel.access", status="denied", context={"channel": "telegram"})
    return first, second


def test_create_audit_ledger_anchor_records_chain_range_and_hash(tmp_path):
    ledger = _ledger(tmp_path)
    first, second = _append_events(ledger)

    anchor = create_audit_ledger_anchor(ledger, label="daily-2026-06-17", created_at="2026-06-17T00:00:00+00:00")

    assert anchor.org_id == "ORG-001"
    assert anchor.chain_start_index == 1
    assert anchor.chain_end_index == 2
    assert anchor.event_count == 2
    assert anchor.start_event_hash == first.event_hash
    assert anchor.end_event_hash == second.event_hash
    assert len(anchor.anchor_hash) == 64
    assert verify_audit_ledger_anchor(ledger, anchor).ok is True


def test_anchor_remains_valid_after_later_append(tmp_path):
    ledger = _ledger(tmp_path)
    _append_events(ledger)
    anchor = create_audit_ledger_anchor(ledger, label="before-more-events")

    ledger.append(event_type="auth", action="login", status="success", context={"user_id": "usr-1"})

    verified = verify_audit_ledger_anchor(ledger, anchor)
    assert verified.ok is True


def test_anchor_verification_detects_anchor_or_ledger_tampering(tmp_path):
    ledger = _ledger(tmp_path)
    first, _second = _append_events(ledger)
    anchor = create_audit_ledger_anchor(ledger)
    changed_anchor = load_audit_ledger_anchor({**anchor.to_dict(), "end_event_hash": "f" * 64})

    assert verify_audit_ledger_anchor(ledger, changed_anchor).ok is False

    with sqlite3.connect(tmp_path / "state" / "enterprise.sqlite") as con:
        con.execute(
            "UPDATE audit_events SET context_json = ? WHERE id = ?",
            (json.dumps({"path": "changed.txt"}, sort_keys=True), first.id),
        )

    tampered = verify_audit_ledger_anchor(ledger, anchor)
    assert tampered.ok is False
    assert any(error.startswith("chain:") for error in tampered.errors)


def test_export_and_load_audit_ledger_anchor(tmp_path):
    ledger = _ledger(tmp_path)
    _append_events(ledger)
    path = tmp_path / "anchors" / "audit-anchor.json"

    result = export_audit_ledger_anchor(ledger, path, label="daily")
    loaded = load_audit_ledger_anchor(result)

    assert result == path
    assert loaded.label == "daily"
    assert verify_audit_ledger_anchor(ledger, loaded).ok is True
