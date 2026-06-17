from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from orchestrator.enterprise import (
    EnterpriseAuditLedger,
    FilesystemAuditAnchorSink,
    IdentityService,
    create_audit_ledger_anchor,
)


def _anchor(tmp_path):
    db_path = tmp_path / "state" / "enterprise.sqlite"
    IdentityService.from_path(db_path).create_organization(org_id="ORG-001", name="Acme")
    ledger = EnterpriseAuditLedger.from_path(db_path, org_id="ORG-001")
    ledger.append(event_type="policy", action="file.write", status="denied", context={"path": "a.txt"})
    return create_audit_ledger_anchor(ledger, label="daily/anchor")


def _path_from_uri(uri: str) -> Path:
    return Path(uri.removeprefix("file://"))


def test_filesystem_anchor_sink_writes_readonly_hash_named_anchor(tmp_path):
    anchor = _anchor(tmp_path)
    sink = FilesystemAuditAnchorSink(tmp_path / "worm")

    receipt = sink.write_anchor(anchor)
    path = _path_from_uri(receipt.uri)
    stored = json.loads(path.read_text(encoding="utf-8"))

    assert path.name.endswith(f"{anchor.anchor_hash}.json")
    assert path.stat().st_mode & 0o777 == 0o444
    assert stored["anchor_hash"] == anchor.anchor_hash
    assert receipt.existed is False
    assert sink.verify_receipt(receipt) is True


def test_filesystem_anchor_sink_is_idempotent_for_same_anchor(tmp_path):
    anchor = _anchor(tmp_path)
    sink = FilesystemAuditAnchorSink(tmp_path / "worm")

    first = sink.write_anchor(anchor)
    second = sink.write_anchor(anchor)

    assert first.uri == second.uri
    assert second.existed is True
    assert sink.verify_receipt(second) is True


def test_filesystem_anchor_sink_rejects_existing_mismatched_content(tmp_path):
    anchor = _anchor(tmp_path)
    sink = FilesystemAuditAnchorSink(tmp_path / "worm")
    receipt = sink.write_anchor(anchor)
    path = _path_from_uri(receipt.uri)
    os.chmod(path, 0o644)
    path.write_text("tampered\n", encoding="utf-8")
    os.chmod(path, 0o444)

    with pytest.raises(ValueError, match="does not match"):
        sink.write_anchor(anchor)
    assert sink.verify_receipt(receipt) is False
