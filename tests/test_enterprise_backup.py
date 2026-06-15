from __future__ import annotations

import tarfile

import pytest

from orchestrator.enterprise.backup import BackupItem, EnterpriseBackup


def test_enterprise_backup_creates_archive_with_manifest(tmp_path):
    db = tmp_path / "enterprise.sqlite"
    db.write_text("db", encoding="utf-8")
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()
    (audit_dir / "events.jsonl").write_text("{}\n", encoding="utf-8")

    result = EnterpriseBackup().create_archive(
        tmp_path / "backups" / "enterprise.tar.gz",
        [
            BackupItem("state/enterprise.sqlite", db, required=True),
            BackupItem("audit", audit_dir),
            BackupItem("missing/optional.txt", tmp_path / "missing.txt"),
        ],
    )

    assert result.archive_path.exists()
    assert [item["included"] for item in result.manifest["items"]] == [True, True, False]
    manifest = EnterpriseBackup().read_manifest(result.archive_path)
    assert manifest["backup_id"].startswith("backup-")
    assert manifest["items"][0]["name"] == "state/enterprise.sqlite"


def test_enterprise_backup_rejects_missing_required_item(tmp_path):
    with pytest.raises(FileNotFoundError, match="required backup item missing"):
        EnterpriseBackup().create_archive(
            tmp_path / "backup.tar.gz",
            [BackupItem("state/enterprise.sqlite", tmp_path / "missing.sqlite", required=True)],
        )


def test_enterprise_backup_restores_archive_safely(tmp_path):
    source = tmp_path / "enterprise.sqlite"
    source.write_text("db", encoding="utf-8")
    archive = EnterpriseBackup().create_archive(
        tmp_path / "backup.tar.gz",
        [BackupItem("state/enterprise.sqlite", source, required=True)],
    )

    result = EnterpriseBackup().restore_archive(archive.archive_path, tmp_path / "restore")

    restored = tmp_path / "restore" / "state" / "enterprise.sqlite"
    assert restored.read_text(encoding="utf-8") == "db"
    assert result["restored"] == ["state/enterprise.sqlite"]


def test_enterprise_backup_restore_refuses_overwrite_without_flag(tmp_path):
    source = tmp_path / "enterprise.sqlite"
    source.write_text("db", encoding="utf-8")
    archive = EnterpriseBackup().create_archive(
        tmp_path / "backup.tar.gz",
        [BackupItem("state/enterprise.sqlite", source, required=True)],
    )
    destination = tmp_path / "restore"
    (destination / "state").mkdir(parents=True)
    (destination / "state" / "enterprise.sqlite").write_text("old", encoding="utf-8")

    with pytest.raises(FileExistsError, match="restore target exists"):
        EnterpriseBackup().restore_archive(archive.archive_path, destination)


def test_enterprise_backup_rejects_unsafe_member_names(tmp_path):
    malicious = tmp_path / "malicious.tar.gz"
    with tarfile.open(malicious, "w:gz") as archive:
        info = tarfile.TarInfo("../outside.txt")
        data = b"bad"
        info.size = len(data)
        archive.addfile(info, _Reader(data))

    with pytest.raises(ValueError, match="unsafe restore member"):
        EnterpriseBackup().restore_archive(malicious, tmp_path / "restore")


class _Reader:
    def __init__(self, data: bytes):
        self._data = data
        self._offset = 0

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = len(self._data) - self._offset
        chunk = self._data[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk
