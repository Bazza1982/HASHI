from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

import hashi
from orchestrator.enterprise import EnterpriseAuditLedger, IdentityService
from orchestrator.enterprise.store import SCHEMA_VERSION


def _prepare_enterprise_root(root):
    (root / "state").mkdir()
    (root / "state" / "enterprise.sqlite").write_text("db", encoding="utf-8")
    (root / "state" / "enterprise_audit.jsonl").write_text("{}\n", encoding="utf-8")
    (root / "agents.json").write_text('{"agents":[]}\n', encoding="utf-8")
    (root / "agent_capabilities.json").write_text('{"agents":[]}\n', encoding="utf-8")


def test_enterprise_backup_cli_creates_archive_and_manifest(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(hashi, "ROOT_DIR", tmp_path)
    _prepare_enterprise_root(tmp_path)
    output = tmp_path / "backup.tar.gz"

    rc = hashi.cmd_enterprise_backup(SimpleNamespace(output=str(output), include_workspaces=False))

    assert rc == 0
    assert output.exists()
    manifest = hashi.cmd_enterprise_backup_inspect(SimpleNamespace(archive=str(output)))
    assert manifest == 0
    text = capsys.readouterr().out
    assert "Enterprise backup written" in text
    assert "state/enterprise.sqlite" in text


def test_enterprise_restore_cli_restores_archive(tmp_path, monkeypatch):
    monkeypatch.setattr(hashi, "ROOT_DIR", tmp_path)
    _prepare_enterprise_root(tmp_path)
    output = tmp_path / "backup.tar.gz"
    hashi.cmd_enterprise_backup(SimpleNamespace(output=str(output), include_workspaces=False))

    rc = hashi.cmd_enterprise_restore(
        SimpleNamespace(
            archive=str(output),
            destination=str(tmp_path / "restore"),
            overwrite=False,
        )
    )

    assert rc == 0
    assert (tmp_path / "restore" / "state" / "enterprise.sqlite").read_text(encoding="utf-8") == "db"


def test_enterprise_backup_cli_fails_when_required_state_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(hashi, "ROOT_DIR", tmp_path)
    (tmp_path / "agents.json").write_text('{"agents":[]}\n', encoding="utf-8")

    with pytest.raises(FileNotFoundError, match="required backup item missing"):
        hashi.cmd_enterprise_backup(SimpleNamespace(output=str(tmp_path / "backup.tar.gz"), include_workspaces=False))


def test_enterprise_migrate_cli_initializes_schema(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(hashi, "ROOT_DIR", tmp_path)
    db_path = tmp_path / "state" / "enterprise.sqlite"

    rc = hashi.cmd_enterprise_migrate(SimpleNamespace(db=str(db_path)))

    assert rc == 0
    assert db_path.exists()
    output = capsys.readouterr().out
    assert "Enterprise schema migrated" in output
    assert "Before: (none)" in output
    assert f"After : {SCHEMA_VERSION}" in output


def test_enterprise_audit_live_export_cli_sends_events_and_updates_checkpoint(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(hashi, "ROOT_DIR", tmp_path)
    db_path = tmp_path / "state" / "enterprise.sqlite"
    identity = IdentityService.from_path(db_path)
    identity.create_organization(org_id="ORG-001", name="Acme")
    ledger = EnterpriseAuditLedger.from_path(db_path, org_id="ORG-001")
    event = ledger.append(event_type="auth", action="login", status="success", actor_id="usr-1")
    calls = []

    def transport(url, body, headers, timeout):
        calls.append((url, body, headers, timeout))
        return 202, "accepted"

    monkeypatch.setattr(hashi, "_http_post_transport", transport)

    rc = hashi.cmd_enterprise_audit_export_live(
        SimpleNamespace(
            endpoint="https://siem.example.com/ingest",
            format="ledger",
            db=None,
            org_id="ORG-001",
            checkpoint=None,
            batch_size=10,
            timeout=5.0,
            max_attempts=1,
            backoff=0.0,
            header=["Authorization: Bearer test"],
        )
    )

    assert rc == 0
    url, body, headers, timeout = calls[0]
    assert url == "https://siem.example.com/ingest"
    assert headers["authorization"] == "Bearer test"
    assert timeout == 5.0
    assert event.id in body.decode("utf-8")
    checkpoint = tmp_path / "state" / "audit_live_export_checkpoint.json"
    assert checkpoint.exists()
    assert json.loads(checkpoint.read_text(encoding="utf-8"))["last_chain_index"] == event.chain_index
    output = capsys.readouterr().out
    assert "Enterprise audit live export completed" in output
    assert "Sent            : 1" in output


def test_enterprise_audit_live_export_daemon_runs_bounded_cycles(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(hashi, "ROOT_DIR", tmp_path)
    db_path = tmp_path / "state" / "enterprise.sqlite"
    identity = IdentityService.from_path(db_path)
    identity.create_organization(org_id="ORG-001", name="Acme")
    ledger = EnterpriseAuditLedger.from_path(db_path, org_id="ORG-001")
    event = ledger.append(event_type="auth", action="login", status="success", actor_id="usr-1")
    calls = []
    sleeps = []

    def transport(url, body, headers, timeout):
        calls.append((url, body, headers, timeout))
        return 202, "accepted"

    monkeypatch.setattr(hashi, "_http_post_transport", transport)
    monkeypatch.setattr(hashi.time, "sleep", sleeps.append)

    rc = hashi.cmd_enterprise_audit_export_live(
        SimpleNamespace(
            endpoint="https://siem.example.com/ingest",
            format="ledger",
            db=None,
            org_id="ORG-001",
            checkpoint=None,
            batch_size=10,
            timeout=5.0,
            max_attempts=1,
            backoff=0.0,
            header=["Authorization: Bearer test"],
            daemon=True,
            interval=0.5,
            max_cycles=2,
        )
    )

    assert rc == 0
    assert len(calls) == 1
    assert event.id in calls[0][1].decode("utf-8")
    assert sleeps == [0.5]
    checkpoint = tmp_path / "state" / "audit_live_export_checkpoint.json"
    assert json.loads(checkpoint.read_text(encoding="utf-8"))["last_chain_index"] == event.chain_index
    output = capsys.readouterr().out
    assert "Enterprise audit live export daemon started" in output
    assert "Cycle 1: sent=1" in output
    assert "Cycle 2: sent=0" in output
    assert "completed max cycles" in output


def test_enterprise_audit_live_export_cli_accepts_vendor_formats():
    seen = []

    def fake_export(args):
        seen.append(args.format)
        return 0

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(hashi, "cmd_enterprise_audit_export_live", fake_export)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "hashi",
                "enterprise",
                "audit-export-live",
                "--endpoint",
                "https://splunk.example.com/services/collector/event",
                "--format",
                "splunk-hec",
            ],
        )
        with pytest.raises(SystemExit) as splunk_exit:
            hashi.main()
        assert splunk_exit.value.code == 0
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "hashi",
                "enterprise",
                "audit-export-live",
                "--endpoint",
                "https://elastic.example.com/hashi-audit/_bulk",
                "--format",
                "elastic-bulk",
            ],
        )
        with pytest.raises(SystemExit) as elastic_exit:
            hashi.main()
        assert elastic_exit.value.code == 0
    finally:
        monkeypatch.undo()

    assert seen == ["splunk-hec", "elastic-bulk"]


def test_enterprise_audit_live_export_cli_rejects_malformed_header():
    with pytest.raises(ValueError, match="Name: value"):
        hashi._parse_http_headers(["broken"])
