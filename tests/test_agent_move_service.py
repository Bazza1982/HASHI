from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.agent_move import service
from orchestrator.agent_move.package import (
    AgentMoveError,
    create_agent_move_package,
    package_sha256,
)
from orchestrator.agent_move.service import (
    activate_agent_move,
    commit_agent_move,
    deactivate_source_agent,
    restore_source_agent,
    rollback_agent_move,
    stage_agent_move,
)
from orchestrator.pcm import render_pcm_document


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _agents(
    instance: str, rows: list[dict], *, max_access_scope: str = "drive"
) -> dict:
    return {
        "global": {
            "instance_id": instance,
            "agent_move": {"max_access_scope": max_access_scope},
        },
        "agents": rows,
    }


def _roots(tmp_path: Path) -> tuple[Path, Path, Path]:
    source = tmp_path / "source"
    target = tmp_path / "target"
    workspace = source / "workspaces" / "zelda"
    (workspace / "memory").mkdir(parents=True)
    (workspace / "agent.md").write_text(
        render_pcm_document(persona="Zelda", system="Follow policy", memory="Memory"),
        encoding="utf-8",
    )
    (workspace / "memory" / "facts.md").write_text("fact", encoding="utf-8")
    row = {
        "name": "zelda",
        "type": "flex",
        "workspace_dir": "workspaces/zelda",
        "active_backend": "codex-cli",
        "allowed_backends": [{"engine": "codex-cli", "model": "gpt-5.5"}],
        "access_scope": "drive",
        "is_active": True,
    }
    _write_json(source / "agents.json", _agents("HASHI1", [row]))
    _write_json(source / "secrets.json", {"zelda": "agent-token", "shared": "source"})
    _write_json(
        source / "tasks.json",
        {
            "version": 1,
            "heartbeats": [{"id": "same-id", "agent": "zelda", "enabled": True}],
            "crons": [],
            "nudges": [],
        },
    )
    target.mkdir()
    _write_json(
        target / "agents.json", _agents("HASHI2", [], max_access_scope="project")
    )
    _write_json(target / "secrets.json", {"shared": "target"})
    _write_json(
        target / "tasks.json",
        {
            "version": 1,
            "heartbeats": [{"id": "same-id", "agent": "other", "enabled": True}],
            "crons": [],
            "nudges": [],
        },
    )
    package_path = tmp_path / "zelda.hashi-agent"
    create_agent_move_package(
        source,
        "zelda",
        package_path,
        source_instance="HASHI1",
        include_agent_secrets=True,
        secret_passphrase="shared-secret",
    )
    return source, target, package_path


def test_stage_commit_activate_and_recoverable_rollback(tmp_path):
    source, target, package_path = _roots(tmp_path)
    agents_before = (target / "agents.json").read_bytes()
    tasks_before = (target / "tasks.json").read_bytes()

    staged = stage_agent_move(
        target,
        package_path.read_bytes(),
        expected_sha256=package_sha256(package_path),
        source_instance="HASHI1",
        secret_passphrase="shared-secret",
    )

    assert staged["status"] == "staged"
    assert (target / "agents.json").read_bytes() == agents_before
    assert (target / "tasks.json").read_bytes() == tasks_before
    assert not (target / "workspaces" / "zelda").exists()

    committed = commit_agent_move(
        target,
        staged["package_id"],
        secret_passphrase="shared-secret",
    )
    assert committed["status"] == "committed_inactive"
    target_agents = json.loads((target / "agents.json").read_text())
    imported = target_agents["agents"][0]
    assert imported["name"] == "zelda"
    assert imported["is_active"] is False
    assert imported["access_scope"] == "project"
    assert imported["transfer_package_id"] == staged["package_id"]
    assert (
        target / "workspaces" / "zelda" / "memory" / "facts.md"
    ).read_text() == "fact"
    target_tasks = json.loads((target / "tasks.json").read_text())
    imported_task = target_tasks["heartbeats"][1]
    assert imported_task["enabled"] is False
    assert imported_task["id"].startswith("same-id--moved-")
    assert json.loads((target / "secrets.json").read_text())["zelda"] == "agent-token"

    activated = activate_agent_move(target, staged["package_id"])
    assert activated["status"] == "activated_pending_reboot"
    assert (
        json.loads((target / "agents.json").read_text())["agents"][0]["is_active"]
        is True
    )

    rolled_back = rollback_agent_move(target, staged["package_id"])
    assert rolled_back["status"] == "rolled_back"
    assert json.loads((target / "agents.json").read_text())["agents"] == []
    assert len(json.loads((target / "tasks.json").read_text())["heartbeats"]) == 1
    assert "zelda" not in json.loads((target / "secrets.json").read_text())
    quarantine = target / "state" / "agent_moves" / "rolled_back"
    assert any(path.name.endswith("-zelda") for path in quarantine.iterdir())
    assert (source / "workspaces" / "zelda").is_dir()


def test_schema2_retained_identity_is_persisted_outside_workspace_and_survives_rollback(
    tmp_path,
):
    source, target, _ = _roots(tmp_path)
    retained = b"historical uppercase identity\n"
    (source / "workspaces" / "zelda" / "AGENT.md").write_bytes(retained)
    package_path = tmp_path / "zelda-schema2.hashi-agent"
    package = create_agent_move_package(
        source,
        "zelda",
        package_path,
        source_instance="HASHI1",
        include_agent_secrets=True,
        secret_passphrase="shared-secret",
    )

    staged = stage_agent_move(
        target,
        package_path.read_bytes(),
        expected_sha256=package_sha256(package_path),
        source_instance="HASHI1",
        secret_passphrase="shared-secret",
        target_platform="windows",
    )

    retained_state = staged["retained_identity"]
    assert retained_state["authoritative"] is False
    assert retained_state["original_path"] == "AGENT.md"
    stored = target / retained_state["storage_path"]
    assert stored.read_bytes() == retained
    assert retained_state["sha256"] == package.retained_identity["sha256"]
    assert any("non-authoritative" in warning for warning in staged["warnings"])

    commit_agent_move(
        target,
        staged["package_id"],
        secret_passphrase="shared-secret",
        target_platform="windows",
    )
    workspace = target / "workspaces" / "zelda"
    assert (workspace / "agent.md").is_file()
    assert not (workspace / "AGENT.md").exists()

    rolled_back = rollback_agent_move(target, staged["package_id"])
    assert rolled_back["status"] == "rolled_back"
    assert stored.read_bytes() == retained
    assert rolled_back["retained_identity"]["storage_path"] == retained_state[
        "storage_path"
    ]


def test_schema2_stage_failure_removes_only_incomplete_transaction(tmp_path, monkeypatch):
    source, target, _ = _roots(tmp_path)
    (source / "workspaces" / "zelda" / "AGENT.md").write_text(
        "historical identity",
        encoding="utf-8",
    )
    package_path = tmp_path / "zelda-schema2-failure.hashi-agent"
    package = create_agent_move_package(
        source,
        "zelda",
        package_path,
        source_instance="HASHI1",
    )
    target_agents_before = (target / "agents.json").read_bytes()
    source_identity_before = (
        source / "workspaces" / "zelda" / "AGENT.md"
    ).read_bytes()
    monkeypatch.setattr(
        service,
        "_preserve_retained_identity",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(OSError, match="disk full"):
        stage_agent_move(
            target,
            package_path.read_bytes(),
            expected_sha256=package_sha256(package_path),
            source_instance="HASHI1",
        )

    record_dir = (
        target / "state" / "agent_moves" / "incoming" / package.package_id
    )
    assert not record_dir.exists()
    assert not list((target / "state" / "agent_moves" / "incoming").glob(".upload-*"))
    assert (target / "agents.json").read_bytes() == target_agents_before
    assert (
        source / "workspaces" / "zelda" / "AGENT.md"
    ).read_bytes() == source_identity_before


def test_activation_blocks_when_agent_credential_was_not_packaged(tmp_path):
    source, target, _ = _roots(tmp_path)
    package_path = tmp_path / "without-secrets.hashi-agent"
    package = create_agent_move_package(
        source,
        "zelda",
        package_path,
        source_instance="HASHI1",
        include_agent_secrets=False,
    )
    stage_agent_move(
        target,
        package_path.read_bytes(),
        expected_sha256=package_sha256(package_path),
        source_instance="HASHI1",
    )
    commit_agent_move(target, package.package_id)

    with pytest.raises(AgentMoveError, match="credentials are incomplete"):
        activate_agent_move(target, package.package_id)


def test_target_collision_is_rejected_before_mutation(tmp_path):
    _, target, package_path = _roots(tmp_path)
    target_agents = json.loads((target / "agents.json").read_text())
    target_agents["agents"].append(
        {"name": "ZELDA", "workspace_dir": "workspaces/other"}
    )
    _write_json(target / "agents.json", target_agents)

    with pytest.raises(AgentMoveError, match="already has Agent"):
        stage_agent_move(
            target,
            package_path.read_bytes(),
            expected_sha256=package_sha256(package_path),
            source_instance="HASHI1",
            secret_passphrase="shared-secret",
        )


def test_return_move_cannot_replace_target_owned_shared_secret():
    with pytest.raises(AgentMoveError, match="credential key collision"):
        service._merge_secrets_preview(
            {"provider_shared": "target-value"},
            {"provider_shared": "source-value"},
            replace_conflicts_for_agent="zelda",
        )


def test_source_deactivation_disables_schedules_and_restore_is_lossless(tmp_path):
    source, _, _ = _roots(tmp_path)
    package_id = "12345678-abcd"

    disabled = deactivate_source_agent(
        source,
        "zelda",
        package_id,
        target_instance="HASHI2",
    )
    assert disabled["workspace_retained"] is True
    assert (
        json.loads((source / "agents.json").read_text())["agents"][0]["is_active"]
        is False
    )
    assert (
        json.loads((source / "tasks.json").read_text())["heartbeats"][0]["enabled"]
        is False
    )
    assert (source / "workspaces" / "zelda" / "memory" / "facts.md").exists()

    restored = restore_source_agent(source, package_id)
    assert restored["status"] == "source_restored_pending_reboot"
    source_agent = json.loads((source / "agents.json").read_text())["agents"][0]
    assert source_agent["is_active"] is True
    assert "transfer_package_id" not in source_agent
    assert (
        json.loads((source / "tasks.json").read_text())["heartbeats"][0]["enabled"]
        is True
    )


def test_source_deactivation_journal_recovers_interrupted_write(tmp_path, monkeypatch):
    source, _, _ = _roots(tmp_path)
    package_id = "12345678-journal"
    original_atomic = service._atomic_json
    interrupted = False

    def _interrupt_tasks(path, value, **kwargs):
        nonlocal interrupted
        if path.name == "tasks.json" and not interrupted:
            interrupted = True
            raise KeyboardInterrupt("simulated process exit")
        return original_atomic(path, value, **kwargs)

    monkeypatch.setattr(service, "_atomic_json", _interrupt_tasks)
    with pytest.raises(KeyboardInterrupt, match="simulated process exit"):
        deactivate_source_agent(
            source,
            "zelda",
            package_id,
            target_instance="HASHI2",
        )
    monkeypatch.setattr(service, "_atomic_json", original_atomic)

    disabled = deactivate_source_agent(
        source,
        "zelda",
        package_id,
        target_instance="HASHI2",
    )
    assert disabled["status"] == "source_disabled_pending_reboot"
    restored = restore_source_agent(source, package_id)
    assert restored["status"] == "source_restored_pending_reboot"
    assert (
        json.loads((source / "agents.json").read_text())["agents"][0]["is_active"]
        is True
    )


def test_source_restore_journal_recovers_after_agent_config_write(
    tmp_path, monkeypatch
):
    source, _, _ = _roots(tmp_path)
    package_id = "12345678-restore"
    deactivate_source_agent(
        source,
        "zelda",
        package_id,
        target_instance="HASHI2",
    )
    original_atomic = service._atomic_json
    interrupted = False

    def _interrupt_tasks(path, value, **kwargs):
        nonlocal interrupted
        if path.name == "tasks.json" and not interrupted:
            interrupted = True
            raise KeyboardInterrupt("simulated process exit")
        return original_atomic(path, value, **kwargs)

    monkeypatch.setattr(service, "_atomic_json", _interrupt_tasks)
    with pytest.raises(KeyboardInterrupt, match="simulated process exit"):
        restore_source_agent(source, package_id)
    monkeypatch.setattr(service, "_atomic_json", original_atomic)

    restored = restore_source_agent(source, package_id)
    assert restored["status"] == "source_restored_pending_reboot"
    assert (
        json.loads((source / "tasks.json").read_text())["heartbeats"][0]["enabled"]
        is True
    )


def test_target_rollback_is_retryable_after_interrupted_config_write(
    tmp_path, monkeypatch
):
    _, target, package_path = _roots(tmp_path)
    staged = stage_agent_move(
        target,
        package_path.read_bytes(),
        expected_sha256=package_sha256(package_path),
        source_instance="HASHI1",
        secret_passphrase="shared-secret",
    )
    commit_agent_move(
        target,
        staged["package_id"],
        secret_passphrase="shared-secret",
    )
    original_atomic = service._atomic_json
    interrupted = False

    def _interrupt_tasks(path, value, **kwargs):
        nonlocal interrupted
        if path.name == "tasks.json" and not interrupted:
            interrupted = True
            raise KeyboardInterrupt("simulated process exit")
        return original_atomic(path, value, **kwargs)

    monkeypatch.setattr(service, "_atomic_json", _interrupt_tasks)
    with pytest.raises(KeyboardInterrupt, match="simulated process exit"):
        rollback_agent_move(target, staged["package_id"])
    monkeypatch.setattr(service, "_atomic_json", original_atomic)

    rolled_back = rollback_agent_move(target, staged["package_id"])
    assert rolled_back["status"] == "rolled_back"
    assert json.loads((target / "agents.json").read_text())["agents"] == []
    assert not (target / "workspaces" / "zelda").exists()


def test_target_rollback_retains_agent_secret_changed_after_import(tmp_path):
    _, target, package_path = _roots(tmp_path)
    staged = stage_agent_move(
        target,
        package_path.read_bytes(),
        expected_sha256=package_sha256(package_path),
        source_instance="HASHI1",
        secret_passphrase="shared-secret",
    )
    commit_agent_move(
        target,
        staged["package_id"],
        secret_passphrase="shared-secret",
    )
    secrets = json.loads((target / "secrets.json").read_text())
    secrets["zelda"] = "rotated-after-import"
    _write_json(target / "secrets.json", secrets)

    rolled_back = rollback_agent_move(target, staged["package_id"])

    assert json.loads((target / "secrets.json").read_text())["zelda"] == (
        "rotated-after-import"
    )
    assert any("retained credential keys" in item for item in rolled_back["warnings"])


def test_return_move_replaces_and_can_restore_dormant_source_copy(tmp_path):
    source, target, outbound_path = _roots(tmp_path)
    outbound = stage_agent_move(
        target,
        outbound_path.read_bytes(),
        expected_sha256=package_sha256(outbound_path),
        source_instance="HASHI1",
        secret_passphrase="shared-secret",
    )
    commit_agent_move(
        target,
        outbound["package_id"],
        secret_passphrase="shared-secret",
    )
    activate_agent_move(target, outbound["package_id"])
    deactivate_source_agent(
        source,
        "zelda",
        outbound["package_id"],
        target_instance="HASHI2",
    )

    (target / "workspaces" / "zelda" / "memory" / "facts.md").write_text(
        "newer target memory",
        encoding="utf-8",
    )
    return_path = tmp_path / "zelda-return.hashi-agent"
    returned_package = create_agent_move_package(
        target,
        "zelda",
        return_path,
        source_instance="HASHI2",
        include_agent_secrets=True,
        secret_passphrase="shared-secret",
    )

    staged_return = stage_agent_move(
        source,
        return_path.read_bytes(),
        expected_sha256=package_sha256(return_path),
        source_instance="HASHI2",
        secret_passphrase="shared-secret",
    )
    assert staged_return["replaces_dormant_source"] is True
    committed_return = commit_agent_move(
        source,
        returned_package.package_id,
        secret_passphrase="shared-secret",
    )
    assert committed_return["replaces_dormant_source"] is True
    assert len(json.loads((source / "agents.json").read_text())["agents"]) == 1
    assert (
        source / "workspaces" / "zelda" / "memory" / "facts.md"
    ).read_text() == "newer target memory"

    rollback_agent_move(source, returned_package.package_id)
    dormant = json.loads((source / "agents.json").read_text())["agents"][0]
    assert dormant["transfer_state"] == "moved_out_pending_reboot"
    assert dormant["transfer_package_id"] == outbound["package_id"]
    assert (
        source / "workspaces" / "zelda" / "memory" / "facts.md"
    ).read_text() == "fact"
