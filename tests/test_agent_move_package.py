from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import zipfile
from pathlib import Path

import pytest

from orchestrator.agent_move.package import (
    RETAINED_IDENTITY_CAPABILITY,
    AgentMoveError,
    archive_snapshot_fingerprint,
    create_agent_move_package,
    decrypt_agent_secrets,
    extract_agent_workspace,
    read_agent_move_package,
)
from orchestrator.pcm import render_pcm_document


def _write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _rewrite_archive(
    source_path: Path,
    target_path: Path,
    *,
    replacements: dict[str, bytes],
) -> None:
    with zipfile.ZipFile(source_path, "r") as source:
        members = {name: source.read(name) for name in source.namelist()}
    members.update(replacements)
    checksums = {
        "schema_version": 1,
        "files": {
            name: hashlib.sha256(content).hexdigest()
            for name, content in members.items()
            if name != "checksums.json"
        },
    }
    members["checksums.json"] = json.dumps(checksums).encode("utf-8")
    with zipfile.ZipFile(target_path, "w") as target:
        for name, content in members.items():
            target.writestr(name, content)


def _source_root(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    workspace = root / "workspaces" / "zelda"
    (workspace / "memory").mkdir(parents=True)
    (workspace / "agent.md").write_text(
        render_pcm_document(
            persona="Zelda", system="Follow policy", memory="Remember Link"
        ),
        encoding="utf-8",
    )
    (workspace / "memory" / "continuity.md").write_text(
        "durable memory", encoding="utf-8"
    )
    (workspace / ".runtime_session.json").write_text("{}", encoding="utf-8")
    (workspace / ".env").write_text("PASSWORD=plaintext", encoding="utf-8")
    (workspace / "workzone.json").write_text(
        '{"path":"/home/source-only"}', encoding="utf-8"
    )
    (workspace / "state" / "request.json").parent.mkdir()
    (workspace / "state" / "request.json").write_text("{}", encoding="utf-8")
    database = workspace / "bridge_memory.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE memories (text TEXT)")
        connection.execute("INSERT INTO memories VALUES ('hello')")
    _write_json(
        root / "agents.json",
        {
            "global": {"instance_id": "HASHI1"},
            "agents": [
                {
                    "name": "zelda",
                    "display_name": "Zelda",
                    "type": "flex",
                    "workspace_dir": "workspaces/zelda",
                    "active_backend": "codex-cli",
                    "allowed_backends": [{"engine": "codex-cli", "model": "gpt-5.5"}],
                    "access_scope": "drive",
                    "is_active": True,
                }
            ],
        },
    )
    _write_json(
        root / "secrets.json", {"zelda": "telegram-token", "provider": "shared"}
    )
    _write_json(
        root / "tasks.json",
        {
            "version": 1,
            "heartbeats": [
                {"id": "zelda-heartbeat", "agent": "zelda", "enabled": True},
                {"id": "other-heartbeat", "agent": "other", "enabled": True},
            ],
            "crons": [],
            "nudges": [],
        },
    )
    return root


def test_package_round_trip_preserves_identity_memory_and_encrypted_agent_secret(
    tmp_path,
):
    root = _source_root(tmp_path)
    path = tmp_path / "zelda.hashi-agent"

    package = create_agent_move_package(
        root,
        "zelda",
        path,
        source_instance="HASHI1",
        include_agent_secrets=True,
        secret_passphrase="shared-secret",
    )

    assert package.manifest["schema_version"] == 1
    assert package.retained_identity is None
    assert package.agent_config["is_active"] is False
    assert package.agent_config["workspace_dir"] == "workspaces/zelda"
    assert decrypt_agent_secrets(package, "shared-secret") == {
        "zelda": "telegram-token"
    }
    assert "secrets.json" not in package.names
    assert "workspace/.env" not in package.names
    assert "workspace/workzone.json" not in package.names
    assert "workspace/.runtime_session.json" not in package.names
    assert "workspace/state/request.json" not in package.names
    assert "workspace/memory/continuity.md" in package.names
    assert package.schedules["heartbeats"] == [
        {
            "agent": "zelda",
            "enabled": False,
            "id": "zelda-heartbeat",
            "import_state": "disabled_review_draft",
        }
    ]

    destination = tmp_path / "target-workspace"
    extract_agent_workspace(package, destination)
    assert (destination / "memory" / "continuity.md").read_text() == "durable memory"
    with sqlite3.connect(destination / "bridge_memory.sqlite") as connection:
        assert connection.execute("SELECT text FROM memories").fetchone() == ("hello",)
    if os.name != "nt":
        assert path.stat().st_mode & 0o077 == 0


def test_package_rejects_wrong_secret_passphrase(tmp_path):
    root = _source_root(tmp_path)
    package = create_agent_move_package(
        root,
        "zelda",
        tmp_path / "zelda.hashi-agent",
        include_agent_secrets=True,
        secret_passphrase="right",
    )

    with pytest.raises(AgentMoveError, match="decryption failed"):
        decrypt_agent_secrets(package, "wrong")


def test_package_rejects_inactive_retained_source_copy(tmp_path):
    root = _source_root(tmp_path)
    data = json.loads((root / "agents.json").read_text())
    data["agents"][0].update(
        {
            "is_active": False,
            "transfer_state": "moved_out_pending_reboot",
            "transfer_target": "HASHI2",
        }
    )
    _write_json(root / "agents.json", data)

    with pytest.raises(AgentMoveError, match="inactive"):
        create_agent_move_package(
            root,
            "zelda",
            tmp_path / "zelda.hashi-agent",
        )


def test_package_detects_checksum_tampering(tmp_path):
    root = _source_root(tmp_path)
    path = tmp_path / "zelda.hashi-agent"
    create_agent_move_package(root, "zelda", path)
    replacement = tmp_path / "tampered.hashi-agent"
    with (
        zipfile.ZipFile(path, "r") as source,
        zipfile.ZipFile(replacement, "w") as target,
    ):
        for info in source.infolist():
            content = source.read(info.filename)
            if info.filename == "identity/agent.md":
                content += b"tampered"
            target.writestr(info, content)

    with pytest.raises(AgentMoveError, match="checksum mismatch"):
        read_agent_move_package(replacement)


def test_package_rejects_windows_case_collision(tmp_path):
    root = _source_root(tmp_path)
    workspace = root / "workspaces" / "zelda"
    (workspace / "Readme.txt").write_text("one", encoding="utf-8")
    path = tmp_path / "zelda.hashi-agent"
    create_agent_move_package(root, "zelda", path)
    replacement = tmp_path / "case-collision.hashi-agent"
    with zipfile.ZipFile(path, "r") as archive:
        metadata = json.loads(
            archive.read("metadata/workspace.json").decode("utf-8")
        )
    metadata["files"].append(
        {
            **next(
                item for item in metadata["files"] if item["path"] == "Readme.txt"
            ),
            "path": "README.TXT",
        }
    )
    metadata["source_bytes"] += len(b"two")
    _rewrite_archive(
        path,
        replacement,
        replacements={
            "metadata/workspace.json": json.dumps(metadata).encode("utf-8"),
            "workspace/README.TXT": b"two",
        },
    )

    with pytest.raises(AgentMoveError, match="case-insensitive path collision"):
        read_agent_move_package(replacement, target_platform="windows")


def test_package_rejects_windows_reserved_agent_id(tmp_path, monkeypatch):
    root = _source_root(tmp_path)
    agents = json.loads((root / "agents.json").read_text())
    agents["agents"][0]["name"] = "CON"
    agents["agents"][0]["workspace_dir"] = "workspaces/zelda"
    _write_json(root / "agents.json", agents)
    path = tmp_path / "con.hashi-agent"
    monkeypatch.setattr(
        "orchestrator.agent_move.package.detect_environment_kind", lambda: "linux"
    )
    create_agent_move_package(root, "CON", path)

    with pytest.raises(AgentMoveError, match="Windows reserved filename"):
        read_agent_move_package(path, target_platform="windows")


def test_package_excludes_nested_git_worktree_with_gitfile(tmp_path):
    root = _source_root(tmp_path)
    project = root / "workspaces" / "zelda" / "nested-project"
    project.mkdir()
    (project / ".git").write_text("gitdir: /outside/repo", encoding="utf-8")
    (project / "large-model.bin").write_bytes(b"x" * 1024)

    package = create_agent_move_package(
        root,
        "zelda",
        tmp_path / "zelda.hashi-agent",
    )

    assert "workspace/nested-project/large-model.bin" not in package.names
    assert {
        "path": "nested-project",
        "reason": "nested_project_not_agent_state",
    } in package.workspace_metadata["excluded"]


def test_package_excludes_ephemeral_directories_case_insensitively(tmp_path):
    root = _source_root(tmp_path)
    hidden_repo = root / "workspaces" / "zelda" / ".Git"
    hidden_repo.mkdir()
    (hidden_repo / "config").write_text("private source metadata", encoding="utf-8")

    package = create_agent_move_package(
        root,
        "zelda",
        tmp_path / "zelda.hashi-agent",
        source_instance="HASHI1",
    )

    assert "workspace/.Git/config" not in package.names
    assert {
        "path": ".Git",
        "reason": "ephemeral_or_environment_directory",
    } in package.workspace_metadata["excluded"]


def test_package_enforces_receiver_size_before_publishing_output(tmp_path):
    root = _source_root(tmp_path)
    path = tmp_path / "too-large.hashi-agent"

    with pytest.raises(AgentMoveError, match="receiver limit"):
        create_agent_move_package(
            root,
            "zelda",
            path,
            max_package_bytes=100,
        )

    assert not path.exists()


def test_package_reserves_canonical_agent_md_name(tmp_path):
    root = _source_root(tmp_path)
    workspace = root / "workspaces" / "zelda"
    (workspace / "agent.md").rename(workspace / "Agent.md")
    assert "Agent.md" in {entry.name for entry in workspace.iterdir()}

    with pytest.raises(AgentMoveError, match="reserved"):
        create_agent_move_package(
            root,
            "zelda",
            tmp_path / "zelda.hashi-agent",
            include_workspace=False,
        )


def test_package_schema2_preserves_exact_uppercase_identity_as_attachment(tmp_path):
    root = _source_root(tmp_path)
    workspace = root / "workspaces" / "zelda"
    retained = b"# Historical AGENT identity\n\nDo not execute this as PCM.\n"
    (workspace / "AGENT.md").write_bytes(retained)

    package = create_agent_move_package(
        root,
        "zelda",
        tmp_path / "zelda-schema2.hashi-agent",
        source_instance="HASHI1",
    )

    assert package.manifest["schema_version"] == 2
    assert RETAINED_IDENTITY_CAPABILITY in package.manifest[
        "required_receiver_capabilities"
    ]
    assert package.manifest["sections"]["retained_identity"] is True
    assert package.retained_identity == {
        "authoritative": False,
        "original_path": "AGENT.md",
        "archive_path": "retained-identity/AGENT.md",
        "sha256": hashlib.sha256(retained).hexdigest(),
        "size": len(retained),
    }
    assert "retained-identity/AGENT.md" in package.names
    assert "workspace/AGENT.md" not in package.names

    destination = tmp_path / "windows-target-workspace"
    extract_agent_workspace(package, destination, target_platform="windows")
    assert (destination / "agent.md").is_file()
    assert not (destination / "AGENT.md").exists()


@pytest.mark.parametrize("name", ["Agent.md", "aGeNt.Md"])
def test_package_rejects_other_case_variants_of_reserved_identity(tmp_path, name):
    root = _source_root(tmp_path)
    workspace = root / "workspaces" / "zelda"
    (workspace / name).write_text("not allowed", encoding="utf-8")

    with pytest.raises(AgentMoveError, match="only exact root AGENT.md"):
        create_agent_move_package(
            root,
            "zelda",
            tmp_path / "invalid-alias.hashi-agent",
        )


def test_package_rejects_uppercase_identity_symlink(tmp_path):
    root = _source_root(tmp_path)
    workspace = root / "workspaces" / "zelda"
    try:
        (workspace / "AGENT.md").symlink_to(workspace / "memory" / "continuity.md")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this platform")

    with pytest.raises(AgentMoveError, match="regular file"):
        create_agent_move_package(
            root,
            "zelda",
            tmp_path / "linked-alias.hashi-agent",
        )


def test_package_rejects_uppercase_identity_directory(tmp_path):
    root = _source_root(tmp_path)
    (root / "workspaces" / "zelda" / "AGENT.md").mkdir()

    with pytest.raises(AgentMoveError, match="regular file"):
        create_agent_move_package(
            root,
            "zelda",
            tmp_path / "directory-alias.hashi-agent",
        )


@pytest.mark.parametrize("relative", ["nested/AGENT.md", "nested/agent.md"])
def test_package_rejects_nested_identity_names(tmp_path, relative):
    root = _source_root(tmp_path)
    path = root / "workspaces" / "zelda" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("nested identity", encoding="utf-8")

    with pytest.raises(AgentMoveError, match="only exact root AGENT.md"):
        create_agent_move_package(
            root,
            "zelda",
            tmp_path / "nested-alias.hashi-agent",
        )


def test_schema2_snapshot_fingerprint_detects_retained_identity_change(tmp_path):
    root = _source_root(tmp_path)
    retained_path = root / "workspaces" / "zelda" / "AGENT.md"
    retained_path.write_text("historical identity one", encoding="utf-8")
    first = create_agent_move_package(
        root,
        "zelda",
        tmp_path / "first-schema2.hashi-agent",
    )
    first_fingerprint = archive_snapshot_fingerprint(first)

    retained_path.write_text("historical identity two", encoding="utf-8")
    second = create_agent_move_package(
        root,
        "zelda",
        tmp_path / "second-schema2.hashi-agent",
    )

    assert archive_snapshot_fingerprint(second) != first_fingerprint


def test_schema2_rejects_retained_identity_metadata_mismatch(tmp_path):
    root = _source_root(tmp_path)
    (root / "workspaces" / "zelda" / "AGENT.md").write_text(
        "historical identity",
        encoding="utf-8",
    )
    path = tmp_path / "valid-schema2.hashi-agent"
    create_agent_move_package(root, "zelda", path)
    replacement = tmp_path / "tampered-schema2.hashi-agent"
    _rewrite_archive(
        path,
        replacement,
        replacements={"retained-identity/AGENT.md": b"different retained identity"},
    )

    with pytest.raises(AgentMoveError, match="metadata does not match"):
        read_agent_move_package(replacement)


def test_snapshot_fingerprint_is_stable_and_detects_sqlite_changes(tmp_path):
    root = _source_root(tmp_path)
    first = create_agent_move_package(
        root,
        "zelda",
        tmp_path / "first.hashi-agent",
        include_agent_secrets=True,
        secret_passphrase="shared-secret",
    )
    second = create_agent_move_package(
        root,
        "zelda",
        tmp_path / "second.hashi-agent",
        include_agent_secrets=True,
        secret_passphrase="shared-secret",
    )
    first_fingerprint = archive_snapshot_fingerprint(
        first,
        secret_passphrase="shared-secret",
    )
    assert archive_snapshot_fingerprint(
        second,
        secret_passphrase="shared-secret",
    ) == first_fingerprint

    with sqlite3.connect(root / "workspaces" / "zelda" / "bridge_memory.sqlite") as db:
        db.execute("INSERT INTO memories VALUES ('newer')")
    third = create_agent_move_package(
        root,
        "zelda",
        tmp_path / "third.hashi-agent",
        include_agent_secrets=True,
        secret_passphrase="shared-secret",
    )
    assert archive_snapshot_fingerprint(
        third,
        secret_passphrase="shared-secret",
    ) != first_fingerprint
