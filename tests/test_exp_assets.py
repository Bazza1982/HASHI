from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from scripts.exp_assets import ExpAssetError, install_pack, pack_status


def _pack(tmp_path: Path, *, member_name: str = "exp/owner/domain/sample.pdf"):
    archive_path = tmp_path / "assets.tar.gz"
    payload = b"asset-content"
    with tarfile.open(archive_path, "w:gz") as archive:
        info = tarfile.TarInfo(member_name)
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    manifest_path = tmp_path / "asset-packs.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "packs": {
                    "test": {
                        "filename": archive_path.name,
                        "sha256": digest,
                        "size_bytes": archive_path.stat().st_size,
                        "content_size_bytes": len(payload),
                        "file_count": 1,
                        "url": None,
                        "extensions": [".pdf"],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return archive_path, manifest_path


def test_exp_asset_pack_installs_exact_paths_and_preserves_existing_files(tmp_path):
    archive, manifest = _pack(tmp_path)
    project = tmp_path / "project"
    (project / "exp").mkdir(parents=True)

    installed, skipped = install_pack(
        "test",
        project_root=project,
        manifest_path=manifest,
        source=str(archive),
    )

    restored = project / "exp/owner/domain/sample.pdf"
    assert (installed, skipped) == (1, 0)
    assert restored.read_bytes() == b"asset-content"
    assert pack_status("test", project_root=project, manifest_path=manifest) == (
        1,
        1,
        True,
    )

    restored.write_bytes(b"user-change")
    assert install_pack(
        "test",
        project_root=project,
        manifest_path=manifest,
        source=str(archive),
    ) == (0, 1)
    assert restored.read_bytes() == b"user-change"

    restored.unlink()
    assert pack_status("test", project_root=project, manifest_path=manifest) == (
        0,
        1,
        True,
    )


def test_exp_asset_status_rejects_stale_or_untrusted_marker(tmp_path):
    _, manifest = _pack(tmp_path)
    project = tmp_path / "project"
    marker = project / "exp/.asset-packs/test.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "pack_id": "test",
                "sha256": "wrong",
                "members": ["exp/owner/domain/sample.pdf"],
            }
        ),
        encoding="utf-8",
    )
    candidate = project / "exp/owner/domain/sample.pdf"
    candidate.parent.mkdir(parents=True)
    candidate.write_bytes(b"unmanaged")

    assert pack_status("test", project_root=project, manifest_path=manifest) == (
        0,
        1,
        False,
    )


def test_exp_asset_pack_rejects_archive_traversal(tmp_path):
    archive, manifest = _pack(tmp_path, member_name="../escape.pdf")
    project = tmp_path / "project"
    (project / "exp").mkdir(parents=True)

    with pytest.raises(ExpAssetError, match="Unsafe EXP asset path"):
        install_pack(
            "test",
            project_root=project,
            manifest_path=manifest,
            source=str(archive),
        )

    assert not (tmp_path / "escape.pdf").exists()


def test_exp_asset_pack_rejects_existing_parent_symlink_escape(tmp_path):
    archive, manifest = _pack(tmp_path)
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    outside.mkdir()
    (project / "exp").mkdir(parents=True)
    (project / "exp" / "owner").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ExpAssetError, match="Unsafe EXP asset target"):
        install_pack(
            "test",
            project_root=project,
            manifest_path=manifest,
            source=str(archive),
        )

    assert list(outside.rglob("*")) == []


def test_exp_asset_pack_rejects_insecure_remote_source(tmp_path):
    _, manifest = _pack(tmp_path)
    project = tmp_path / "project"
    (project / "exp").mkdir(parents=True)

    with pytest.raises(ExpAssetError, match="Unsupported asset source scheme: http"):
        install_pack(
            "test",
            project_root=project,
            manifest_path=manifest,
            source="http://example.invalid/assets.tar.gz",
        )
