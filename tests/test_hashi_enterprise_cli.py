from __future__ import annotations

from types import SimpleNamespace

import pytest

import hashi
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
