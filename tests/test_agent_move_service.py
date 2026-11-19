from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from orchestrator.agent_move import service
from orchestrator.agent_move.package import (
    AgentMoveError,
    create_agent_move_package,
    package_sha256,
    read_agent_move_package,
)
from orchestrator.agent_move.service import (
    activate_agent_move,
    cleanup_source_agent,
    commit_agent_move,
    deactivate_source_agent,
    finalize_agent_move,
    moved_agent_destination,
    resolve_agent_transfer_target,
    restore_source_agent,
    rollback_agent_move,
    stage_agent_move,
)
from orchestrator.config_json import ConfigConflictError, read_config_json, write_config_json
from orchestrator.pcm import render_pcm_document
from orchestrator.session_store import SessionStore
from orchestrator.telegram_delivery_state import telegram_bot_fingerprint


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _rewrite_legacy_json(path: Path) -> None:
    rendered = json.dumps(
        json.loads(path.read_text(encoding="utf-8")),
        ensure_ascii=False,
        indent=2,
    ).replace("\n", "\r\n")
    path.write_bytes(b"\xef\xbb\xbf" + (rendered + "\r\n").encode("utf-8"))


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
    _write_json(
        source / "agents.json",
        _agents(
            "HASHI1",
            [
                row,
                {
                    "name": "anchor",
                    "type": "flex",
                    "workspace_dir": "workspaces/anchor",
                    "active_backend": "codex-cli",
                    "allowed_backends": [
                        {"engine": "codex-cli", "model": "gpt-5.5"}
                    ],
                    "is_active": True,
                },
            ],
        ),
    )
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


def _complete_history(
    store: SessionStore,
    *,
    session_id: str,
    agent_id: str,
    request_id: str,
    question: str,
    answer: str,
    owner_id: str = "user:7",
) -> None:
    accepted = store.accept_run(
        session_id=session_id,
        owner_id=owner_id,
        agent_id=agent_id,
        request_id=request_id,
        text=question,
        source="workbench",
        idempotency_key=request_id,
    )
    store.mark_request_running(accepted.request_id, worker_id="fixture")
    store.finish_request(
        accepted.request_id,
        success=True,
        assistant_text=answer,
        assistant_source="fixture",
    )


def _authorize_owner(root: Path, owner_number: int = 7) -> None:
    agents = json.loads((root / "agents.json").read_text())
    agents["global"]["authorized_id"] = owner_number
    _write_json(root / "agents.json", agents)


def test_move_preserves_schedule_enabled_intent_and_restores_absent_flag(tmp_path):
    source, target, _ = _roots(tmp_path)
    tasks_path = source / "tasks.json"
    tasks = json.loads(tasks_path.read_text())
    tasks["crons"] = [
        {"id": "disabled", "agent": "zelda", "enabled": False},
        {"id": "implicit-disabled", "agent": "zelda"},
    ]
    _write_json(tasks_path, tasks)
    package_path = tmp_path / "with-schedule-intent.hashi-agent"
    package = create_agent_move_package(
        source, "zelda", package_path, source_instance="HASHI1",
        include_agent_secrets=True, secret_passphrase="shared-secret",
    )
    staged = stage_agent_move(
        target, package_path.read_bytes(), expected_sha256=package_sha256(package_path),
        source_instance="HASHI1", secret_passphrase="shared-secret",
        operation="move", target_agent_id="zelda",
    )
    committed = commit_agent_move(target, staged["package_id"], secret_passphrase="shared-secret")
    target_tasks = json.loads((target / "tasks.json").read_text())
    intent = committed["schedule_intent"]
    moved = [item for item in target_tasks["heartbeats"] if item.get("import_package_id") == package.package_id]
    assert len(moved) == 1 and moved[0]["enabled"] is False
    assert intent["heartbeats"][moved[0]["id"]] is True
    assert intent["crons"]["disabled"] is False
    assert intent["crons"]["implicit-disabled"] is False

    deactivate_source_agent(source, "zelda", package.package_id, target_instance="HASHI2")
    restore_source_agent(source, package.package_id)
    restored = json.loads(tasks_path.read_text())
    assert restored["heartbeats"][0]["enabled"] is True
    assert restored["crons"][0]["enabled"] is False
    assert "enabled" not in restored["crons"][1]


def test_legacy_schedule_package_does_not_invent_original_enabled_state(tmp_path, monkeypatch):
    from orchestrator.agent_move import package as package_module

    source, target, _ = _roots(tmp_path)
    original_collect = package_module._collect_schedules

    def old_format(root, agent_id):
        schedules = original_collect(root, agent_id)
        schedules.pop("source_enabled", None)
        return schedules

    monkeypatch.setattr(package_module, "_collect_schedules", old_format)
    package_path = tmp_path / "legacy-schedules.hashi-agent"
    create_agent_move_package(
        source, "zelda", package_path, source_instance="HASHI1",
        include_agent_secrets=True, secret_passphrase="shared-secret",
    )
    staged = stage_agent_move(
        target, package_path.read_bytes(), expected_sha256=package_sha256(package_path),
        source_instance="HASHI1", secret_passphrase="shared-secret",
        operation="move", target_agent_id="zelda",
    )
    committed = commit_agent_move(target, staged["package_id"], secret_passphrase="shared-secret")
    assert committed["schedule_intent"] is None


def test_cleanup_preserves_changed_and_new_source_schedules(tmp_path):
    source, _, package_path = _roots(tmp_path)
    package = read_agent_move_package(package_path, verify=True)
    deactivate_source_agent(source, "zelda", package.package_id, target_instance="HASHI2")
    tasks_path = source / "tasks.json"
    tasks = json.loads(tasks_path.read_text())
    tasks["heartbeats"][0]["prompt"] = "edited after packaging"
    tasks["crons"].append({"id": "new-job", "agent": "zelda", "enabled": True, "prompt": "keep me"})
    _write_json(tasks_path, tasks)

    cleaned = cleanup_source_agent(
        source, package.package_id, source_secret_keys=["zelda"],
        target_instance="HASHI2", target_agent_id="zelda",
        source_secret_snapshot={"zelda": "agent-token"},
        source_schedule_snapshot=package.schedules,
    )
    remaining = json.loads(tasks_path.read_text())
    assert remaining["heartbeats"][0]["prompt"] == "edited after packaging"
    assert remaining["crons"][0]["prompt"] == "keep me"
    assert all(not item["enabled"] for section in ("heartbeats", "crons") for item in remaining[section])
    retained = cleaned["preserved_source_schedules"]
    assert retained["heartbeats"][0]["prompt"] == "edited after packaging"
    assert retained["crons"][0]["enabled"] is True
    assert "zelda" not in json.loads((source / "secrets.json").read_text())


def test_cleanup_preserves_unproven_schedule_and_credential(tmp_path):
    # This separate case covers credentials that were never proven transferred.
    source, _, package_path = _roots(tmp_path)
    package = read_agent_move_package(package_path, verify=True)
    deactivate_source_agent(source, "zelda", package.package_id, target_instance="HASHI2")

    cleaned = cleanup_source_agent(
        source, package.package_id, source_secret_keys=["zelda"],
        target_instance="HASHI2", target_agent_id="zelda",
    )
    assert cleaned["preserved_source_schedules"]["heartbeats"][0]["id"] == "same-id"
    assert cleaned["preserved_source_secret_keys"] == ["zelda"]
    assert json.loads((source / "secrets.json").read_text())["zelda"] == "agent-token"
    remaining = json.loads((source / "tasks.json").read_text())["heartbeats"]
    assert len(remaining) == 1 and remaining[0]["enabled"] is False


def test_cleanup_keeps_shared_credential_without_blocking_move(tmp_path):
    source, _, package_path = _roots(tmp_path)
    package = read_agent_move_package(package_path, verify=True)
    agents = json.loads((source / "agents.json").read_text())
    agents["agents"][1]["telegram_token_key"] = "zelda"
    _write_json(source / "agents.json", agents)
    deactivate_source_agent(source, "zelda", package.package_id, target_instance="HASHI2")

    cleaned = cleanup_source_agent(
        source, package.package_id, source_secret_keys=["zelda"],
        target_instance="HASHI2", target_agent_id="zelda",
        source_secret_snapshot={"zelda": "agent-token"},
        source_schedule_snapshot=package.schedules,
    )

    assert cleaned["status"] == "source_cleaned"
    assert cleaned["preserved_source_secret_keys"] == ["zelda"]
    assert json.loads((source / "secrets.json").read_text())["zelda"] == "agent-token"
    assert [row["name"] for row in json.loads((source / "agents.json").read_text())["agents"]] == ["anchor"]
    assert not (source / "workspaces" / "zelda").exists()


def test_source_cleanup_preserves_untransferred_and_new_workspace_files(tmp_path):
    source, _, package_path = _roots(tmp_path)
    workspace = source / "workspaces" / "zelda"
    (workspace / ".env").write_text("private project configuration", encoding="utf-8")
    (workspace / "new-after-package.txt").write_text("new user work", encoding="utf-8")
    package = read_agent_move_package(package_path, verify=True)
    deactivate_source_agent(source, "zelda", package.package_id, target_instance="HASHI2")

    cleaned = cleanup_source_agent(
        source, package.package_id, source_secret_keys=["zelda"],
        target_instance="HASHI2", target_agent_id="zelda",
        source_secret_snapshot={"zelda": "agent-token"}, source_schedule_snapshot=package.schedules,
    )

    retained = Path(cleaned["retained_source_workspace"])
    assert cleaned["workspace_retained"] is True
    assert not workspace.exists()
    assert (retained / ".env").read_text(encoding="utf-8") == "private project configuration"
    assert (retained / "new-after-package.txt").read_text(encoding="utf-8") == "new user work"
    assert (retained / "memory" / "facts.md").read_text(encoding="utf-8") == "fact"
    assert cleanup_source_agent(
        source, package.package_id, source_secret_keys=[], target_instance="HASHI2",
        target_agent_id="zelda",
    )["retained_source_workspace"] == str(retained)


def test_source_workspace_retention_recovers_after_rename_before_journal(tmp_path, monkeypatch):
    source, _, package_path = _roots(tmp_path)
    package = read_agent_move_package(package_path, verify=True)
    deactivate_source_agent(source, "zelda", package.package_id, target_instance="HASHI2")
    original_atomic = service._atomic_json
    interrupted = False

    def interrupt_after_rename(path, value, **kwargs):
        nonlocal interrupted
        retention = value.get("workspace_retention", {}) if isinstance(value, dict) else {}
        if retention.get("status") == "preserved" and not interrupted:
            interrupted = True
            raise KeyboardInterrupt("simulated process exit after rename")
        return original_atomic(path, value, **kwargs)

    monkeypatch.setattr(service, "_atomic_json", interrupt_after_rename)
    with pytest.raises(KeyboardInterrupt, match="after rename"):
        cleanup_source_agent(
            source, package.package_id, source_secret_keys=[], target_instance="HASHI2",
            target_agent_id="zelda", source_schedule_snapshot=package.schedules,
        )
    assert not (source / "workspaces" / "zelda").exists()
    monkeypatch.setattr(service, "_atomic_json", original_atomic)
    recovered = cleanup_source_agent(
        source, package.package_id, source_secret_keys=[], target_instance="HASHI2",
        target_agent_id="zelda", source_schedule_snapshot=package.schedules,
    )
    retained = Path(recovered["retained_source_workspace"])
    assert recovered["status"] == "source_cleaned"
    assert (retained / "memory" / "facts.md").read_text(encoding="utf-8") == "fact"
    assert len(list((source / "workspaces").glob(".hashi-move-retained-*"))) == 1


def test_source_workspace_stays_in_place_if_atomic_move_is_unavailable(tmp_path, monkeypatch):
    source, _, package_path = _roots(tmp_path)
    package = read_agent_move_package(package_path, verify=True)
    deactivate_source_agent(source, "zelda", package.package_id, target_instance="HASHI2")
    def unavailable(*args):
        raise OSError("filesystem cannot move the directory")
    monkeypatch.setattr(service, "_rename_workspace_without_replacement", unavailable)
    cleaned = cleanup_source_agent(
        source, package.package_id, source_secret_keys=[], target_instance="HASHI2",
        target_agent_id="zelda", source_schedule_snapshot=package.schedules,
    )
    retained = source / "workspaces" / "zelda"
    assert cleaned["workspace_retention"]["status"] == "in_place"
    assert cleaned["retained_source_workspace"] == str(retained)
    assert (retained / "memory" / "facts.md").read_text(encoding="utf-8") == "fact"


def test_retention_detects_windows_reparse_points_without_junction_api():
    import stat
    from types import SimpleNamespace

    class PathWithoutJunctionAPI:
        def lstat(self):
            return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)

    assert service._workspace_path_is_link(PathWithoutJunctionAPI()) is True


def test_source_workspace_collision_never_overwrites_existing_data(tmp_path, monkeypatch):
    source, _, package_path = _roots(tmp_path)
    package = read_agent_move_package(package_path, verify=True)
    deactivate_source_agent(source, "zelda", package.package_id, target_instance="HASHI2")
    def collision(source_path, target_path):
        target_path.mkdir()
        (target_path / "other.txt").write_text("other data", encoding="utf-8")
        raise OSError("destination exists")
    monkeypatch.setattr(service, "_rename_workspace_without_replacement", collision)
    with pytest.raises(AgentMoveError, match="outcome is ambiguous"):
        cleanup_source_agent(
            source, package.package_id, source_secret_keys=[], target_instance="HASHI2",
            target_agent_id="zelda", source_schedule_snapshot=package.schedules,
        )
    assert (source / "workspaces" / "zelda" / "memory" / "facts.md").exists()
    assert list((source / "workspaces").glob(".hashi-move-retained-*/zelda/other.txt"))


@pytest.mark.parametrize("maximum, effective", [("drive", "drive"), ("project", "project")])
def test_transfer_previews_and_commits_selected_access_policy(tmp_path, maximum, effective):
    _, target, package_path = _roots(tmp_path)
    config = json.loads((target / "agents.json").read_text())
    config["global"]["agent_move"]["max_access_scope"] = maximum
    _write_json(target / "agents.json", config)

    staged = stage_agent_move(
        target, package_path, expected_sha256=package_sha256(package_path),
        source_instance="HASHI1", secret_passphrase="shared-secret",
    )
    assert staged["access_scope_plan"] == {
        "requested": "drive", "target_maximum": maximum, "effective": effective,
    }
    assert any("clamp access_scope" in warning for warning in staged["warnings"]) == (maximum == "project")
    commit_agent_move(target, staged["package_id"], secret_passphrase="shared-secret")
    assert json.loads((target / "agents.json").read_text())["agents"][0]["access_scope"] == effective


@pytest.mark.parametrize("bad_value", ["typo", "", None, 9])
def test_transfer_rejects_malformed_destination_access_policy(tmp_path, bad_value):
    _, target, package_path = _roots(tmp_path)
    config = json.loads((target / "agents.json").read_text())
    config["global"]["agent_move"]["max_access_scope"] = bad_value
    _write_json(target / "agents.json", config)
    before = (target / "agents.json").read_bytes()

    with pytest.raises(AgentMoveError, match="max_access_scope is invalid"):
        stage_agent_move(
            target, package_path, expected_sha256=package_sha256(package_path),
            source_instance="HASHI1", secret_passphrase="shared-secret",
        )
    assert (target / "agents.json").read_bytes() == before
    assert not (target / "workspaces" / "zelda").exists()


def test_transfer_rejects_malformed_source_access_scope(tmp_path):
    source, target, _ = _roots(tmp_path)
    config = json.loads((source / "agents.json").read_text())
    config["agents"][0]["access_scope"] = "typo"
    _write_json(source / "agents.json", config)
    package_path = tmp_path / "invalid-scope.hashi-agent"
    create_agent_move_package(
        source, "zelda", package_path, source_instance="HASHI1",
        include_agent_secrets=True, secret_passphrase="shared-secret",
    )

    with pytest.raises(AgentMoveError, match="imported Agent access_scope is invalid"):
        stage_agent_move(
            target, package_path, expected_sha256=package_sha256(package_path),
            source_instance="HASHI1", secret_passphrase="shared-secret",
        )
    assert not (target / "workspaces" / "zelda").exists()


def test_transfer_rejects_target_policy_change_after_preview_without_mutation(tmp_path):
    _, target, package_path = _roots(tmp_path)
    staged = stage_agent_move(
        target, package_path, expected_sha256=package_sha256(package_path),
        source_instance="HASHI1", secret_passphrase="shared-secret",
    )
    config = json.loads((target / "agents.json").read_text())
    config["global"]["agent_move"]["max_access_scope"] = "workspace"
    _write_json(target / "agents.json", config)
    files = [target / "agents.json", target / "tasks.json", target / "secrets.json"]
    before = [path.read_bytes() for path in files]

    with pytest.raises(AgentMoveError, match="policy changed after preview"):
        commit_agent_move(target, staged["package_id"], secret_passphrase="shared-secret")
    assert [path.read_bytes() for path in files] == before
    assert not (target / "workspaces" / "zelda").exists()

    config["global"]["agent_move"]["max_access_scope"] = "project"
    _write_json(target / "agents.json", config)
    committed = commit_agent_move(target, staged["package_id"], secret_passphrase="shared-secret")
    assert committed["status"] == "committed_inactive"


def test_staged_only_abort_is_replayable_and_prevents_late_commit(tmp_path):
    source, target, package_path = _roots(tmp_path)
    staged = stage_agent_move(
        target, package_path, expected_sha256=package_sha256(package_path),
        source_instance="HASHI1", secret_passphrase="shared-secret",
    )
    before = (target / "agents.json").read_bytes()
    first = service.abort_staged_agent_move(target, staged["package_id"])
    second = service.abort_staged_agent_move(target, staged["package_id"])
    assert first["status"] == second["status"] == "rolled_back"
    assert first["staged_abort"] is second["staged_abort"] is True
    with pytest.raises(AgentMoveError, match="cannot be committed again"):
        commit_agent_move(target, staged["package_id"], secret_passphrase="shared-secret")
    assert (target / "agents.json").read_bytes() == before
    assert (source / "workspaces" / "zelda" / "memory" / "facts.md").exists()


def test_staged_only_abort_cannot_rollback_committed_or_other_rollback(tmp_path):
    _, target, package_path = _roots(tmp_path)
    staged = stage_agent_move(
        target, package_path, expected_sha256=package_sha256(package_path),
        source_instance="HASHI1", secret_passphrase="shared-secret",
    )
    commit_agent_move(target, staged["package_id"], secret_passphrase="shared-secret")
    before = (target / "agents.json").read_bytes()
    with pytest.raises(AgentMoveError, match="cannot touch a committed or active target"):
        service.abort_staged_agent_move(target, staged["package_id"])
    assert (target / "agents.json").read_bytes() == before
    rollback_agent_move(target, staged["package_id"])
    with pytest.raises(AgentMoveError, match="not rolled back by the staged-only abort"):
        service.abort_staged_agent_move(target, staged["package_id"])


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


def test_inactive_move_finalizes_only_with_offline_runtime_and_owned_inactive_config(tmp_path):
    source, target, _ = _roots(tmp_path)
    data = json.loads((source / "agents.json").read_text())
    data["agents"][0]["is_active"] = False
    _write_json(source / "agents.json", data)
    path = tmp_path / "inactive.hashi-agent"
    create_agent_move_package(source, "zelda", path, source_instance="HASHI1", operation="move",
                              include_agent_secrets=True, secret_passphrase="shared-secret")
    staged = stage_agent_move(target, path, expected_sha256=package_sha256(path),
                              source_instance="HASHI1", target_instance="HASHI2",
                              secret_passphrase="shared-secret", operation="move")
    assert staged["desired_target_active"] is False
    commit_agent_move(target, staged["package_id"], secret_passphrase="shared-secret")
    with pytest.raises(AgentMoveError, match="inactive target"):
        activate_agent_move(target, staged["package_id"])
    for runtime_online in (None, True):
        with pytest.raises(AgentMoveError, match="online or its status is unknown"):
            finalize_agent_move(target, staged["package_id"], runtime_online=runtime_online)
    target_config = json.loads((target / "agents.json").read_text())
    target_config["agents"][0]["is_active"] = True
    _write_json(target / "agents.json", target_config)
    with pytest.raises(AgentMoveError, match="committed inactive"):
        finalize_agent_move(target, staged["package_id"], runtime_online=False)
    target_config["agents"][0]["is_active"] = False
    _write_json(target / "agents.json", target_config)
    result = finalize_agent_move(target, staged["package_id"], runtime_online=False)
    assert result["status"] == "completed"
    assert result["target_verification"] == {"runtime_online": False, "runtime_present": False,
        "presence_checked": True, "desired_active": False, "config_inactive": True}
    assert json.loads((target / "agents.json").read_text())["agents"][0]["is_active"] is False
    assert finalize_agent_move(target, staged["package_id"], runtime_online=False)["status"] == "completed"


def test_inactive_source_can_request_active_destination_without_starting_source(tmp_path):
    source, target, _ = _roots(tmp_path)
    data = json.loads((source / "agents.json").read_text())
    data["agents"][0]["is_active"] = False
    _write_json(source / "agents.json", data)
    before = (source / "agents.json").read_bytes()
    path = tmp_path / "start-at-target.hashi-agent"
    create_agent_move_package(
        source, "zelda", path, source_instance="HASHI1", operation="move",
        desired_target_active=True, include_agent_secrets=True, secret_passphrase="shared-secret",
    )
    assert (source / "agents.json").read_bytes() == before
    staged = stage_agent_move(target, path, expected_sha256=package_sha256(path),
                              source_instance="HASHI1", target_instance="HASHI2",
                              secret_passphrase="shared-secret", operation="move")
    assert staged["desired_target_active"] is True
    commit_agent_move(target, staged["package_id"], secret_passphrase="shared-secret")
    assert json.loads((target / "agents.json").read_text())["agents"][0]["is_active"] is False
    activate_agent_move(target, staged["package_id"])
    assert json.loads((target / "agents.json").read_text())["agents"][0]["is_active"] is True
    assert finalize_agent_move(target, staged["package_id"], runtime_online=True)["status"] == "completed"
    assert (source / "agents.json").read_bytes() == before


def test_schema5_commit_prepends_history_idempotently_and_rollback_preserves_target_work(
    tmp_path,
):
    source, target, _ = _roots(tmp_path)
    _authorize_owner(source)
    _authorize_owner(target)
    source_store = SessionStore(
        source / "state" / "sessions.sqlite3", instance_id="HASHI1"
    )
    source_session = source_store.resolve_session(
        owner_id="user:7",
        agent_id="zelda",
        surface="workbench",
        channel_key="default",
    )
    source_store.bind_channel(
        owner_id="user:7",
        agent_id="zelda",
        surface="telegram",
        channel_key="7",
        session_id=source_session["session_id"],
    )
    _complete_history(
        source_store,
        session_id=source_session["session_id"],
        agent_id="zelda",
        request_id="source-old",
        question="old source question",
        answer="old source answer",
    )
    target_store = SessionStore(
        target / "state" / "sessions.sqlite3", instance_id="HASHI2"
    )
    target_session = target_store.resolve_session(
        owner_id="user:7",
        agent_id="zelda",
        surface="workbench",
        channel_key="default",
    )
    _complete_history(
        target_store,
        session_id=target_session["session_id"],
        agent_id="zelda",
        request_id="target-new",
        question="new target question",
        answer="new target answer",
    )
    package_path = tmp_path / "history-move.hashi-agent"
    create_agent_move_package(
        source,
        "zelda",
        package_path,
        source_instance="HASHI1",
        transfer_mode="identity_memory",
        include_agent_secrets=True,
        secret_passphrase="shared-secret",
    )
    staged = stage_agent_move(
        target,
        package_path.read_bytes(),
        expected_sha256=package_sha256(package_path),
        source_instance="HASHI1",
        operation="move",
        secret_passphrase="shared-secret",
    )

    committed = commit_agent_move(
        target, staged["package_id"], secret_passphrase="shared-secret"
    )
    replayed = commit_agent_move(
        target, staged["package_id"], secret_passphrase="shared-secret"
    )

    resolved = target_store.resolve_session(
        owner_id="user:7",
        agent_id="zelda",
        surface="workbench",
        channel_key="default",
    )
    assert [
        item["text"]
        for item in target_store.messages(
            resolved["session_id"], owner_id="user:7"
        )
    ] == [
        "old source question",
        "old source answer",
        "new target question",
        "new target answer",
    ]
    assert committed["conversation_continuity"]["imported_messages"] == 2
    assert replayed["conversation_continuity"]["imported_messages"] == 2

    # A fresh store models a Worker restart after the move.
    reopened_store = SessionStore(target / "state" / "sessions.sqlite3", instance_id="HASHI2")
    reopened_session = reopened_store.resolve_session(
        owner_id="user:7", agent_id="zelda", surface="workbench", channel_key="default"
    )
    assert [
        item["text"]
        for item in reopened_store.messages(reopened_session["session_id"], owner_id="user:7")
    ] == ["old source question", "old source answer", "new target question", "new target answer"]

    rollback_agent_move(target, staged["package_id"])
    assert [
        item["text"]
        for item in target_store.messages(
            resolved["session_id"], owner_id="user:7"
        )
    ] == ["new target question", "new target answer"]
    assert [
        item["text"]
        for item in reopened_store.messages(reopened_session["session_id"], owner_id="user:7")
    ] == ["new target question", "new target answer"]
    assert source_store.messages(
        source_session["session_id"], owner_id="user:7"
    )[0]["text"] == "old source question"


def test_move_maps_distinct_owner_histories_and_preserves_unrelated_and_new_work(tmp_path):
    source, target, _ = _roots(tmp_path)
    for root, owner in ((source, "owner:source"), (target, "owner:target")):
        config = json.loads((root / "agents.json").read_text())
        config["global"]["owner_scope_id"] = owner
        config["global"].pop("authorized_id", None)
        _write_json(root / "agents.json", config)
    source_store = SessionStore(source / "state/sessions.sqlite3", instance_id="HASHI1")
    source_session = source_store.ensure_default_session(owner_id="owner:source", agent_id="zelda")
    other_source = source_store.ensure_default_session(owner_id="owner:other", agent_id="zelda")
    _complete_history(source_store, session_id=source_session["session_id"], agent_id="zelda",
                      request_id="source-owned", question="source question", answer="source answer",
                      owner_id="owner:source")
    _complete_history(source_store, session_id=other_source["session_id"], agent_id="zelda",
                      request_id="source-other", question="not selected", answer="private",
                      owner_id="owner:other")
    target_store = SessionStore(target / "state/sessions.sqlite3", instance_id="HASHI2")
    target_session = target_store.resolve_session(owner_id="owner:target", agent_id="zelda",
                                                   surface="workbench", channel_key="default")
    unrelated = target_store.ensure_default_session(owner_id="owner:other", agent_id="zelda")
    _complete_history(target_store, session_id=target_session["session_id"], agent_id="zelda",
                      request_id="target-existing", question="target question", answer="target answer",
                      owner_id="owner:target")
    _complete_history(target_store, session_id=unrelated["session_id"], agent_id="zelda",
                      request_id="target-other", question="other target", answer="other answer",
                      owner_id="owner:other")
    path = tmp_path / "different-owner-move.hashi-agent"
    create_agent_move_package(source, "zelda", path, source_instance="HASHI1",
                              operation="move", transfer_mode="identity_memory",
                              include_agent_secrets=True, secret_passphrase="shared-secret")
    staged = stage_agent_move(target, path, expected_sha256=package_sha256(path),
                              source_instance="HASHI1", target_instance="HASHI2", operation="move",
                              secret_passphrase="shared-secret")
    first = commit_agent_move(target, staged["package_id"], secret_passphrase="shared-secret")
    again = commit_agent_move(target, staged["package_id"], secret_passphrase="shared-secret")
    assert first["conversation_continuity"]["imported_messages"] == 2
    assert again["conversation_continuity"]["imported_messages"] == 2
    selected = target_store.resolve_session(owner_id="owner:target", agent_id="zelda",
                                            surface="workbench", channel_key="default")
    def texts():
        return [row["text"] for row in target_store.messages(
            selected["session_id"], owner_id="owner:target"
        )]

    assert texts() == ["source question", "source answer", "target question", "target answer"]
    reopened = SessionStore(target / "state/sessions.sqlite3", instance_id="HASHI2")
    assert [row["text"] for row in reopened.messages(selected["session_id"], owner_id="owner:target")] == texts()
    assert [row["text"] for row in target_store.messages(unrelated["session_id"], owner_id="owner:other")] == [
        "other target", "other answer",
    ]
    _complete_history(target_store, session_id=selected["session_id"], agent_id="zelda",
                      request_id="target-later", question="later question", answer="later answer",
                      owner_id="owner:target")
    rollback_agent_move(target, staged["package_id"])
    assert texts() == ["target question", "target answer", "later question", "later answer"]
    assert [row["text"] for row in source_store.messages(other_source["session_id"], owner_id="owner:other")] == [
        "not selected", "private",
    ]


def test_schema5_commit_failure_compensates_conversation_import(tmp_path, monkeypatch):
    source, target, _ = _roots(tmp_path)
    _authorize_owner(source)
    _authorize_owner(target)
    source_store = SessionStore(
        source / "state" / "sessions.sqlite3", instance_id="HASHI1"
    )
    source_session = source_store.ensure_default_session(
        owner_id="user:7", agent_id="zelda"
    )
    _complete_history(
        source_store,
        session_id=source_session["session_id"],
        agent_id="zelda",
        request_id="source",
        question="must roll back",
        answer="must also roll back",
    )
    package_path = tmp_path / "failing-history.hashi-agent"
    package = create_agent_move_package(
        source,
        "zelda",
        package_path,
        source_instance="HASHI1",
        transfer_mode="identity_memory",
        include_agent_secrets=True,
        secret_passphrase="shared-secret",
    )
    staged = stage_agent_move(
        target,
        package_path.read_bytes(),
        expected_sha256=package_sha256(package_path),
        source_instance="HASHI1",
        operation="move",
        secret_passphrase="shared-secret",
    )
    monkeypatch.setattr(
        service,
        "_credential_status",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AgentMoveError("injected post-history failure")
        ),
    )

    with pytest.raises(AgentMoveError, match="injected post-history failure"):
        commit_agent_move(
            target, staged["package_id"], secret_passphrase="shared-secret"
        )

    target_store = SessionStore(
        target / "state" / "sessions.sqlite3", instance_id="HASHI2"
    )
    assert target_store.conversation_continuity_import_status(package.package_id) is None
    assert target_store.list_sessions(owner_id="user:7") == []
    assert json.loads((target / "agents.json").read_text())["agents"] == []


def test_schema5_clone_read_only_history_is_archived_and_source_is_unchanged(tmp_path):
    source, target, _ = _roots(tmp_path)
    _authorize_owner(source)
    _authorize_owner(target)
    source_store = SessionStore(
        source / "state" / "sessions.sqlite3", instance_id="HASHI1"
    )
    source_session = source_store.ensure_default_session(
        owner_id="user:7", agent_id="zelda"
    )
    _complete_history(
        source_store,
        session_id=source_session["session_id"],
        agent_id="zelda",
        request_id="clone-source",
        question="read only source",
        answer="read only answer",
    )
    package_path = tmp_path / "read-only-clone.hashi-agent"
    create_agent_move_package(
        source,
        "zelda",
        package_path,
        source_instance="HASHI1",
        operation="clone",
        include_telegram_secret=False,
        transfer_mode="identity_memory",
        history_mode="inherit_read_only",
    )
    staged = stage_agent_move(
        target,
        package_path.read_bytes(),
        expected_sha256=package_sha256(package_path),
        source_instance="HASHI1",
        operation="clone",
        target_agent_id="zelda-copy",
    )
    commit_agent_move(target, staged["package_id"])

    target_store = SessionStore(
        target / "state" / "sessions.sqlite3", instance_id="HASHI2"
    )
    inherited = target_store.list_sessions(
        owner_id="user:7", agent_id="zelda-copy", include_archived=True
    )
    assert len(inherited) == 1
    assert inherited[0]["status"] == "archived"
    assert inherited[0]["is_default"] is False
    assert [
        item["text"]
        for item in target_store.messages(
            inherited[0]["session_id"], owner_id="user:7"
        )
    ] == ["read only source", "read only answer"]
    active = target_store.resolve_session(
        owner_id="user:7",
        agent_id="zelda-copy",
        surface="workbench",
        channel_key="default",
    )
    assert active["session_id"] != inherited[0]["session_id"]
    assert target_store.messages(active["session_id"], owner_id="user:7") == []
    assert len(
        source_store.messages(source_session["session_id"], owner_id="user:7")
    ) == 2


@pytest.mark.parametrize("history_mode", ["copy", "inherit_read_only"])
def test_clone_receiver_uses_stable_owner_after_telegram_account_replacement(
    tmp_path, history_mode
):
    source, target, _ = _roots(tmp_path)
    _authorize_owner(source)
    _authorize_owner(target)
    source_store = SessionStore(source / "state/sessions.sqlite3", instance_id="HASHI1")
    session = source_store.ensure_default_session(owner_id="user:7", agent_id="zelda")
    _complete_history(
        source_store,
        session_id=session["session_id"],
        agent_id="zelda",
        request_id="clone-stable",
        question="owned question",
        answer="owned answer",
    )
    config = json.loads((target / "agents.json").read_text())
    config["global"].update(
        owner_scope_id="user:7", authorized_id=99, telegram_owner_id=100
    )
    _write_json(target / "agents.json", config)
    package_path = tmp_path / "stable-clone.hashi-agent"
    create_agent_move_package(
        source,
        "zelda",
        package_path,
        source_instance="HASHI1",
        operation="clone",
        transfer_mode="workspace",
        history_mode=history_mode,
        include_telegram_secret=False,
    )
    staged = stage_agent_move(
        target,
        package_path.read_bytes(),
        expected_sha256=package_sha256(package_path),
        source_instance="HASHI1",
        operation="clone",
        target_agent_id="zelda-copy",
    )
    first = commit_agent_move(target, staged["package_id"])
    replay = commit_agent_move(target, staged["package_id"])
    target_store = SessionStore(target / "state/sessions.sqlite3", instance_id="HASHI2")
    imported = target_store.list_sessions(
        owner_id="user:7", agent_id="zelda-copy", include_archived=True
    )
    assert len(imported) == 1
    assert imported[0]["status"] == (
        "archived" if history_mode == "inherit_read_only" else "active"
    )
    assert [
        row["text"]
        for row in target_store.messages(imported[0]["session_id"], owner_id="user:7")
    ] == [
        "owned question",
        "owned answer",
    ]
    assert first["conversation_continuity"]["imported_messages"] == 2
    assert replay["conversation_continuity"]["imported_messages"] == 2
    assert target_store.list_sessions(owner_id="user:99", agent_id="zelda-copy") == []
    assert len(source_store.messages(session["session_id"], owner_id="user:7")) == 2


@pytest.mark.parametrize("history_mode", ["copy", "inherit_read_only"])
def test_clone_receiver_maps_distinct_source_and_target_owners(tmp_path, history_mode):
    source, target, _ = _roots(tmp_path)
    _authorize_owner(source)
    _authorize_owner(target)
    source_store = SessionStore(source / "state/sessions.sqlite3", instance_id="HASHI1")
    session = source_store.ensure_default_session(owner_id="user:7", agent_id="zelda")
    _complete_history(
        source_store,
        session_id=session["session_id"],
        agent_id="zelda",
        request_id="cross-owner-clone",
        question="portable question",
        answer="portable answer",
    )
    config = json.loads((target / "agents.json").read_text())
    config["global"]["owner_scope_id"] = "user:8"
    _write_json(target / "agents.json", config)
    package_path = tmp_path / "different-owner.hashi-agent"
    create_agent_move_package(
        source,
        "zelda",
        package_path,
        source_instance="HASHI1",
        operation="clone",
        transfer_mode="workspace",
        history_mode=history_mode,
        include_telegram_secret=False,
    )
    staged = stage_agent_move(
        target,
        package_path.read_bytes(),
        expected_sha256=package_sha256(package_path),
        source_instance="HASHI1",
        operation="clone",
        target_agent_id="zelda-copy",
    )
    result = commit_agent_move(target, staged["package_id"])
    replay = commit_agent_move(target, staged["package_id"])
    target_store = SessionStore(target / "state/sessions.sqlite3", instance_id="HASHI2")
    imported = target_store.list_sessions(
        owner_id="user:8", agent_id="zelda-copy", include_archived=True
    )
    assert len(imported) == 1
    assert [m["text"] for m in target_store.messages(
        imported[0]["session_id"], owner_id="user:8"
    )] == ["portable question", "portable answer"]
    assert target_store.list_sessions(
        owner_id="user:7", agent_id="zelda-copy", include_archived=True
    ) == []
    assert result["conversation_continuity"]["imported_messages"] == 2
    assert replay["conversation_continuity"]["imported_messages"] == 2
    assert len(source_store.messages(session["session_id"], owner_id="user:7")) == 2


@pytest.mark.parametrize("history_mode", ["none", "copy"])
def test_stage_retry_cannot_reuse_package_for_another_target_instance(tmp_path, history_mode):
    source, target, _ = _roots(tmp_path)
    package_path = tmp_path / "target-retry.hashi-agent"
    create_agent_move_package(
        source, "zelda", package_path, source_instance="HASHI1",
        operation="clone", transfer_mode="workspace", history_mode=history_mode,
        include_telegram_secret=False,
    )
    payload = package_path.read_bytes()
    kwargs = dict(
        expected_sha256=package_sha256(package_path), source_instance="HASHI1",
        operation="clone", target_agent_id="zelda-copy",
    )
    first = stage_agent_move(target, payload, target_instance="HASHI2", **kwargs)
    replay = stage_agent_move(target, payload, target_instance="HASHI2", **kwargs)
    assert replay["package_id"] == first["package_id"]
    record_dir, _ = service._load_record(target, first["package_id"])
    prior = (record_dir / "state.json").read_bytes()

    with pytest.raises(AgentMoveError, match="different target instance"):
        stage_agent_move(target, payload, target_instance="HASHI3", **kwargs)
    assert (record_dir / "state.json").read_bytes() == prior


@pytest.mark.parametrize("legacy_projection_conflict", [False, True])
def test_clone_preserves_staged_destination_when_legacy_config_changes(tmp_path, legacy_projection_conflict):
    source, target, _ = _roots(tmp_path)
    _authorize_owner(source)
    _authorize_owner(target, 8)
    source_store = SessionStore(source / "state/sessions.sqlite3", instance_id="HASHI1")
    source_session = source_store.ensure_default_session(owner_id="user:7", agent_id="zelda")
    accepted = source_store.accept_run(
        session_id=source_session["session_id"], owner_id="user:7", agent_id="zelda",
        request_id="config-change", text="original history", source="workbench",
        idempotency_key="config-change",
    )
    source_store.mark_request_running(accepted.request_id, worker_id="fixture")
    source_store.finish_request(
        accepted.request_id, success=True, assistant_text="original answer",
        assistant_source="fixture",
    )
    package_path = tmp_path / "staged-owner.hashi-agent"
    create_agent_move_package(
        source, "zelda", package_path, source_instance="HASHI1",
        operation="clone", transfer_mode="workspace", history_mode="copy",
        include_telegram_secret=False,
    )
    staged = stage_agent_move(
        target, package_path.read_bytes(), expected_sha256=package_sha256(package_path),
        source_instance="HASHI1", operation="clone", target_agent_id="zelda-copy",
    )
    _authorize_owner(target, 9)
    if legacy_projection_conflict:
        config = json.loads((target / "agents.json").read_text())
        config["global"]["owner_scope_id"] = "other-existing-key"
        _write_json(target / "agents.json", config)
    committed = commit_agent_move(target, staged["package_id"])
    assert committed["status"] == "committed_inactive"
    target_config = json.loads((target / "agents.json").read_text())
    assert target_config["global"]["owner_scope_id"] == (
        "other-existing-key" if legacy_projection_conflict else "user:8"
    )
    assert any("Legacy local data projection differs" in warning for warning in committed["warnings"]) == legacy_projection_conflict
    _, saved = service._load_record(target, staged["package_id"])
    assert saved["conversation_owner_binding"]["target_owner_id"] == "user:8"
    assert (target / "workspaces/zelda-copy").exists()
    target_store = SessionStore(target / "state/sessions.sqlite3", instance_id="HASHI2")
    sessions = target_store.list_sessions(owner_id="user:8", agent_id="zelda-copy")
    assert len(sessions) == 1
    assert [row["text"] for row in target_store.messages(
        sessions[0]["session_id"], owner_id="user:8"
    )] == ["original history", "original answer"]
    assert target_store.list_sessions(owner_id="user:9", agent_id="zelda-copy") == []


@pytest.mark.parametrize("history_mode", ["none", "copy"])
def test_governed_remote_clone_allows_no_history_and_empty_history(tmp_path, history_mode):
    source, target, _ = _roots(tmp_path)
    _authorize_owner(source)
    _authorize_owner(target, 8)
    target_config = json.loads((target / "agents.json").read_text())
    target_config["global"].update(deployment_profile="enterprise", organization_id="org-a")
    _write_json(target / "agents.json", target_config)
    package_path = tmp_path / "empty-governed.hashi-agent"
    create_agent_move_package(
        source, "zelda", package_path, source_instance="HASHI1", operation="clone",
        transfer_mode="workspace", history_mode=history_mode, include_telegram_secret=False,
    )
    staged = stage_agent_move(
        target, package_path, expected_sha256=package_sha256(package_path),
        source_instance="HASHI1", operation="clone", target_agent_id="zelda-copy",
    )
    assert staged["status"] == "staged"


@pytest.mark.parametrize("same_instance_claim", [False, True])
def test_governed_remote_history_does_not_treat_instance_token_as_user_mapping(
    tmp_path, same_instance_claim
):
    # A governed target without a verified destination user must not reject the
    # whole Clone. It downgrades to an Agent-only transfer: the Agent is staged
    # and committed, but its conversation history is never silently attributed
    # to an unverified user or to the instance token.
    source, target, _ = _roots(tmp_path)
    _authorize_owner(source)
    _authorize_owner(target, 8)
    target_config = json.loads((target / "agents.json").read_text())
    target_config["global"].update(deployment_profile="enterprise", organization_id="org-a")
    if same_instance_claim:
        target_config["global"]["instance_id"] = "HASHI1"
    _write_json(target / "agents.json", target_config)
    source_store = SessionStore(source / "state/sessions.sqlite3", instance_id="HASHI1")
    source_store.ensure_default_session(owner_id="user:7", agent_id="zelda")
    package_path = tmp_path / "unmapped-governed.hashi-agent"
    create_agent_move_package(
        source, "zelda", package_path, source_instance="HASHI1", operation="clone",
        transfer_mode="workspace", history_mode="copy", include_telegram_secret=False,
    )
    staged = stage_agent_move(
        target, package_path, expected_sha256=package_sha256(package_path),
        source_instance="HASHI1", operation="clone", target_agent_id="zelda-copy",
    )
    assert staged["status"] == "staged"
    assert staged.get("history_mode") == "none"
    assert staged.get("conversation_owner_binding") is None
    committed = commit_agent_move(target, staged["package_id"])
    assert committed["status"] in {"committed_inactive", "completed"}
    assert (target / "workspaces/zelda-copy").exists()
    # No conversation history may be imported without a verified destination user.
    target_store = SessionStore(target / "state/sessions.sqlite3", instance_id="HASHI2")
    assert target_store.list_sessions(owner_id="user:7", agent_id="zelda-copy") == []
    assert target_store.list_sessions(owner_id="user:8", agent_id="zelda-copy") == []


def test_staged_personal_history_does_not_bypass_later_governed_policy(tmp_path):
    # If the target becomes governed between stage and commit, the staged
    # personal owner mapping must not be silently reused for governed history.
    # The transfer still succeeds, but conversation history is skipped rather
    # than attributed to the stale personal owner.
    source, target, _ = _roots(tmp_path)
    _authorize_owner(source)
    _authorize_owner(target, 8)
    source_store = SessionStore(source / "state/sessions.sqlite3", instance_id="HASHI1")
    source_store.ensure_default_session(owner_id="user:7", agent_id="zelda")
    package_path = tmp_path / "policy-change.hashi-agent"
    create_agent_move_package(
        source, "zelda", package_path, source_instance="HASHI1", operation="clone",
        transfer_mode="workspace", history_mode="copy", include_telegram_secret=False,
    )
    staged = stage_agent_move(
        target, package_path, expected_sha256=package_sha256(package_path),
        source_instance="HASHI1", operation="clone", target_agent_id="zelda-copy",
    )
    target_config = json.loads((target / "agents.json").read_text())
    target_config["global"].update(deployment_profile="enterprise", organization_id="org-a")
    _write_json(target / "agents.json", target_config)
    committed = commit_agent_move(target, staged["package_id"])
    assert committed["status"] in {"committed_inactive", "completed"}
    assert (target / "workspaces/zelda-copy").exists()
    target_store = SessionStore(target / "state/sessions.sqlite3", instance_id="HASHI2")
    assert target_store.list_sessions(owner_id="user:8", agent_id="zelda-copy") == []


def test_clone_rejects_altered_staged_source_owner_binding(tmp_path):
    source, target, _ = _roots(tmp_path)
    _authorize_owner(source)
    _authorize_owner(target, 8)
    package_path = tmp_path / "binding.hashi-agent"
    create_agent_move_package(
        source, "zelda", package_path, source_instance="HASHI1",
        operation="clone", transfer_mode="workspace", history_mode="copy",
        include_telegram_secret=False,
    )
    staged = stage_agent_move(
        target, package_path.read_bytes(), expected_sha256=package_sha256(package_path),
        source_instance="HASHI1", operation="clone", target_agent_id="zelda-copy",
    )
    record_path = service._record_dir(target, staged["package_id"]) / "state.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["conversation_owner_binding"]["source_owner_id"] = "owner:unrelated"
    _write_json(record_path, record)
    with pytest.raises(AgentMoveError, match="owner binding changed"):
        commit_agent_move(target, staged["package_id"])
    assert not (target / "workspaces/zelda-copy").exists()


def test_clone_rejects_altered_staged_target_owner_binding(tmp_path):
    source, target, _ = _roots(tmp_path)
    _authorize_owner(source)
    _authorize_owner(target, 8)
    package_path = tmp_path / "target-binding.hashi-agent"
    create_agent_move_package(
        source, "zelda", package_path, source_instance="HASHI1",
        operation="clone", transfer_mode="workspace", history_mode="copy",
        include_telegram_secret=False,
    )
    staged = stage_agent_move(
        target, package_path.read_bytes(), expected_sha256=package_sha256(package_path),
        source_instance="HASHI1", operation="clone", target_agent_id="zelda-copy",
    )
    record_path = service._record_dir(target, staged["package_id"]) / "state.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["conversation_owner_binding"]["target_owner_id"] = "owner:unrelated"
    _write_json(record_path, record)
    with pytest.raises(AgentMoveError, match="owner binding changed"):
        commit_agent_move(target, staged["package_id"])
    assert not (target / "workspaces/zelda-copy").exists()



def test_clone_rejects_jointly_altered_staged_target_owner_binding(tmp_path):
    source, target, _ = _roots(tmp_path)
    _authorize_owner(source)
    _authorize_owner(target, 8)
    source_store = SessionStore(source / "state/sessions.sqlite3", instance_id="HASHI1")
    source_store.ensure_default_session(owner_id="user:7", agent_id="zelda")
    package_path = tmp_path / "joint-target-binding.hashi-agent"
    create_agent_move_package(
        source, "zelda", package_path, source_instance="HASHI1",
        operation="clone", transfer_mode="workspace", history_mode="copy",
        include_telegram_secret=False,
    )
    staged = stage_agent_move(
        target, package_path.read_bytes(), expected_sha256=package_sha256(package_path),
        source_instance="HASHI1", operation="clone", target_agent_id="zelda-copy",
    )
    record_path = service._record_dir(target, staged["package_id"]) / "state.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["conversation_owner_binding"]["target_owner_id"] = "owner:unrelated"
    record["conversation_target_owner_id"] = "owner:unrelated"
    _write_json(record_path, record)

    with pytest.raises(AgentMoveError, match="target data binding"):
        commit_agent_move(target, staged["package_id"])
    assert not (target / "workspaces/zelda-copy").exists()
    target_store = SessionStore(target / "state/sessions.sqlite3", instance_id="HASHI2")
    assert target_store.list_sessions(owner_id="owner:unrelated", agent_id="zelda-copy") == []



def test_clone_rejects_a_missing_staged_target_data_binding(tmp_path):
    source, target, _ = _roots(tmp_path)
    _authorize_owner(source)
    _authorize_owner(target, 8)
    source_store = SessionStore(source / "state/sessions.sqlite3", instance_id="HASHI1")
    source_store.ensure_default_session(owner_id="user:7", agent_id="zelda")
    package_path = tmp_path / "missing-target-binding.hashi-agent"
    create_agent_move_package(
        source, "zelda", package_path, source_instance="HASHI1",
        operation="clone", transfer_mode="workspace", history_mode="copy",
        include_telegram_secret=False,
    )
    staged = stage_agent_move(
        target, package_path.read_bytes(), expected_sha256=package_sha256(package_path),
        source_instance="HASHI1", operation="clone", target_agent_id="zelda-copy",
    )
    (target / "state/personal_data_owner.json").unlink()

    with pytest.raises(AgentMoveError, match="target data binding"):
        commit_agent_move(target, staged["package_id"])
    assert not (target / "workspaces/zelda-copy").exists()



def test_legacy_staged_clone_establishes_target_data_binding_once_on_retry(tmp_path, monkeypatch):
    source, target, _ = _roots(tmp_path)
    _authorize_owner(source)
    _authorize_owner(target, 8)
    source_store = SessionStore(source / "state/sessions.sqlite3", instance_id="HASHI1")
    source_store.ensure_default_session(owner_id="user:7", agent_id="zelda")
    package_path = tmp_path / "legacy-target-binding.hashi-agent"
    create_agent_move_package(
        source, "zelda", package_path, source_instance="HASHI1",
        operation="clone", transfer_mode="workspace", history_mode="copy",
        include_telegram_secret=False,
    )
    staged = stage_agent_move(
        target, package_path.read_bytes(), expected_sha256=package_sha256(package_path),
        source_instance="HASHI1", operation="clone", target_agent_id="zelda-copy",
    )
    record_path = service._record_dir(target, staged["package_id"]) / "state.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record.pop("conversation_owner_binding")
    record.pop("conversation_target_owner_id")
    _write_json(record_path, record)
    (target / "state/personal_data_owner.json").unlink()
    establish = service.establish_personal_target_conversation_owner

    def unavailable(_root):
        raise OSError("temporary storage failure")

    monkeypatch.setattr(service, "establish_personal_target_conversation_owner", unavailable)
    with pytest.raises(OSError, match="temporary storage failure"):
        commit_agent_move(target, staged["package_id"])
    assert "conversation_owner_binding" not in json.loads(record_path.read_text(encoding="utf-8"))
    assert not (target / "workspaces/zelda-copy").exists()

    monkeypatch.setattr(service, "establish_personal_target_conversation_owner", establish)
    assert commit_agent_move(target, staged["package_id"])["status"] == "committed_inactive"
    target_store = SessionStore(target / "state/sessions.sqlite3", instance_id="HASHI2")
    assert len(target_store.list_sessions(owner_id="user:8", agent_id="zelda-copy")) == 1


def test_move_commit_adopts_owned_delivery_state_and_rollback_retires_it(tmp_path):
    source, target, _ = _roots(tmp_path)
    _authorize_owner(source)
    _authorize_owner(target)
    lifecycle_id = "4" * 32
    source_config = json.loads((source / "agents.json").read_text())
    source_config["agents"][0]["agent_lifecycle_id"] = lifecycle_id
    _write_json(source / "agents.json", source_config)
    undelivered = source / "workspaces" / "zelda" / "undelivered"
    undelivered.mkdir()
    (undelivered / "req-owned.md").write_text("owned response", encoding="utf-8")
    _write_json(
        source / "state" / "telegram_delivery_health.json",
        {
            "version": 2,
            "agents": {
                "zelda": {
                    "owner": {
                        "instance_id": "HASHI1",
                        "agent_lifecycle_id": lifecycle_id,
                        "telegram_bot_fingerprint": telegram_bot_fingerprint(
                            "agent-token"
                        ),
                    },
                    "status": "blocked",
                    "incident_id": "move-owned-incident",
                    "per_chat": {
                        "123": {"undelivered_request_ids": ["req-owned"]}
                    },
                }
            },
            "quarantine": [],
        },
    )
    package_path = tmp_path / "owned-state.hashi-agent"
    create_agent_move_package(
        source,
        "zelda",
        package_path,
        source_instance="HASHI1",
        include_agent_secrets=True,
        secret_passphrase="shared-secret",
        transfer_mode="workspace",
    )
    staged = stage_agent_move(
        target,
        package_path.read_bytes(),
        expected_sha256=package_sha256(package_path),
        source_instance="HASHI1",
        secret_passphrase="shared-secret",
    )

    committed = commit_agent_move(
        target, staged["package_id"], secret_passphrase="shared-secret"
    )

    delivery = json.loads(
        (target / "state" / "telegram_delivery_health.json").read_text()
    )
    record = delivery["agents"]["zelda"]
    assert committed["telegram_delivery_state"]["imported"] is True
    assert record["incident_id"] == "move-owned-incident"
    assert record["owner"] == {
        "instance_id": "HASHI2",
        "agent_lifecycle_id": lifecycle_id,
        "telegram_bot_fingerprint": telegram_bot_fingerprint("agent-token"),
    }
    assert (
        target / "workspaces" / "zelda" / "undelivered" / "req-owned.md"
    ).read_text() == "owned response"

    rollback_agent_move(target, staged["package_id"])
    delivery = json.loads(
        (target / "state" / "telegram_delivery_health.json").read_text()
    )
    assert "zelda" not in delivery["agents"]
    assert delivery["quarantine"][-1]["reason"] == "agent_transfer_rolled_back"


@pytest.mark.skipif(
    os.name == "nt",
    reason="agent.md and AGENT.md cannot coexist on a case-insensitive Windows tree",
)
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
        schema_version=2,
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


@pytest.mark.skipif(
    os.name == "nt",
    reason="agent.md and AGENT.md cannot coexist on a case-insensitive Windows tree",
)
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
        schema_version=2,
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


def test_move_stage_reports_credentials_not_packaged_without_rejecting(tmp_path):
    source, target, _ = _roots(tmp_path)
    package_path = tmp_path / "without-secrets.hashi-agent"
    create_agent_move_package(
        source,
        "zelda",
        package_path,
        source_instance="HASHI1",
        include_agent_secrets=False,
    )
    staged = stage_agent_move(
        target,
        package_path.read_bytes(),
        expected_sha256=package_sha256(package_path),
        source_instance="HASHI1",
        operation="move",
        target_agent_id="zelda",
    )

    assert staged["status"] == "staged"
    assert "zelda" in staged["credential_status"]["missing_keys"]
    assert any("credentials are incomplete" in warning for warning in staged["warnings"])
    assert json.loads((source / "secrets.json").read_text())["zelda"]


def test_activation_rollback_does_not_overwrite_an_intervening_writer(
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
    actual_atomic = service._atomic_json
    agents_path = (target / "agents.json").resolve()

    def fail_record_after_external_write(path, value, **kwargs):
        if path.name == "state.json":
            winner = read_config_json(agents_path)
            winner["global"]["concurrent_extension"] = "kept"
            write_config_json(agents_path, winner)
            raise OSError("simulated journal failure")
        return actual_atomic(path, value, **kwargs)

    monkeypatch.setattr(service, "_atomic_json", fail_record_after_external_write)

    with pytest.raises(ConfigConflictError):
        activate_agent_move(target, staged["package_id"])

    current = read_config_json(agents_path)
    assert current["global"]["concurrent_extension"] == "kept"


def test_commit_recovery_does_not_overwrite_an_intervening_writer(
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
    actual_atomic = service._atomic_json
    agents_path = (target / "agents.json").resolve()
    injected = False

    def fail_after_task_publication(path, value, **kwargs):
        nonlocal injected
        result = actual_atomic(path, value, **kwargs)
        if path.name == "tasks.json" and not injected:
            injected = True
            winner = read_config_json(agents_path)
            winner["global"]["concurrent_extension"] = "kept"
            write_config_json(agents_path, winner)
            raise OSError("simulated commit interruption")
        return result

    monkeypatch.setattr(service, "_atomic_json", fail_after_task_publication)

    with pytest.raises(AgentMoveError, match="not the transaction publication"):
        commit_agent_move(
            target,
            staged["package_id"],
            secret_passphrase="shared-secret",
        )

    current = read_config_json(agents_path)
    assert current["global"]["concurrent_extension"] == "kept"


def test_stage_replay_rejects_package_id_reuse_without_changing_target(tmp_path):
    source, target, path = _roots(tmp_path)
    package_id = "stable-stage-replay"
    create_agent_move_package(
        source, "zelda", path, operation="clone", transfer_mode="workspace",
        package_id=package_id,
    )
    arguments = dict(
        expected_sha256=package_sha256(path), source_instance="HASHI1",
        operation="clone", target_agent_id="sheik",
    )
    staged = stage_agent_move(target, path, **arguments)
    assert stage_agent_move(target, path, **arguments) == staged

    record_dir = service._record_dir(target, package_id)
    protected = [
        target / "agents.json", target / "tasks.json", target / "secrets.json",
        record_dir / "state.json", record_dir / "package.hashi-agent",
    ]
    before = {item: item.read_bytes() for item in protected}

    with pytest.raises(AgentMoveError, match="different target Agent ID"):
        stage_agent_move(target, path, **{**arguments, "target_agent_id": "other"})
    with pytest.raises(AgentMoveError, match="does not match the package manifest"):
        stage_agent_move(target, path, **{**arguments, "operation": "move"})

    (source / "workspaces" / "zelda" / "memory" / "facts.md").write_text(
        "different source facts", encoding="utf-8",
    )
    changed = tmp_path / "changed.hashi-agent"
    create_agent_move_package(
        source, "zelda", changed, operation="clone", transfer_mode="workspace",
        package_id=package_id,
    )
    with pytest.raises(AgentMoveError, match="different content"):
        stage_agent_move(
            target, changed, **{**arguments, "expected_sha256": package_sha256(changed)},
        )
    assert {item: item.read_bytes() for item in protected} == before
    assert not list((target / "state" / "agent_moves" / "incoming").glob(".upload-*"))


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


def test_clone_staging_reports_import_problems_together_before_confirmation(tmp_path):
    source, target, path = _roots(tmp_path)
    data = json.loads((source / "agents.json").read_text())
    data["agents"][0]["active_backend"] = "not-in-allowed-backends"
    _write_json(source / "agents.json", data)
    _write_json(target / "tasks.json", [])
    _write_json(target / "secrets.json", [])
    package = create_agent_move_package(
        source, "zelda", path, operation="clone", transfer_mode="workspace",
    )
    before = {p.name: p.read_bytes() for p in target.glob("*.json")}
    with pytest.raises(AgentMoveError) as error:
        stage_agent_move(
            target, path, expected_sha256=package_sha256(path), source_instance="HASHI1",
            operation="clone",
        )
    assert "active_backend" in str(error.value)
    assert "tasks.json" in str(error.value)
    assert "secrets.json" in str(error.value)
    assert {p.name: p.read_bytes() for p in target.glob("*.json")} == before
    assert not list((target / "state/agent_moves/incoming").glob("*/state.json"))
    assert package.manifest["operation"] == "clone"


def test_clone_auto_id_includes_capability_only_reservations(tmp_path):
    source, target, path = _roots(tmp_path)
    _write_json(target / "agent_capabilities.json", {"agents": [{"name": "zelda"}]})
    create_agent_move_package(source, "zelda", path, operation="clone", transfer_mode="workspace")
    staged = stage_agent_move(
        target, path, expected_sha256=package_sha256(path), source_instance="HASHI1", operation="clone",
    )
    assert staged["target_agent_id"] == "zelda_1"


@pytest.mark.parametrize("requested,expected", [("sheik", "sheik"), (None, "clone_CON.txt")])
def test_clone_source_metadata_id_is_not_a_target_windows_filename(tmp_path, requested, expected):
    source, target, path = _roots(tmp_path)
    data = json.loads((source / "agents.json").read_text())
    data["agents"][0]["name"] = "CON.txt"
    _write_json(source / "agents.json", data)
    create_agent_move_package(source, "CON.txt", path, operation="clone", transfer_mode="workspace")
    staged = stage_agent_move(
        target, path, expected_sha256=package_sha256(path), source_instance="HASHI1",
        operation="clone", target_agent_id=requested, target_platform="windows",
    )
    assert staged["target_agent_id"] == expected
    committed = commit_agent_move(target, staged["package_id"], target_platform="windows")
    assert committed["status"] == "committed_inactive"
    assert (target / "workspaces" / expected / "agent.md").is_file()


def test_clone_auto_suffix_fits_maximum_length_source_id(tmp_path):
    _, target, _ = _roots(tmp_path)
    name = "a" * 128
    data = json.loads((target / "agents.json").read_text())
    data["agents"].append({"name": name})
    _write_json(target / "agents.json", data)
    result = resolve_agent_transfer_target(target, name, operation="clone")
    assert result["target_agent_id"] == "a" * 126 + "_1"


def test_target_id_resolution_uses_first_free_suffix_and_rejects_explicit_collision(
    tmp_path,
):
    _, target, _ = _roots(tmp_path)
    agents = json.loads((target / "agents.json").read_text())
    agents["agents"].append(
        {"name": "ZELDA", "workspace_dir": "workspaces/ZELDA", "is_active": True}
    )
    _write_json(target / "agents.json", agents)
    (target / "workspaces" / "zelda_1").mkdir(parents=True)

    resolved = resolve_agent_transfer_target(
        target,
        "zelda",
        operation="move",
    )
    assert resolved["target_agent_id"] == "zelda_2"
    custom = resolve_agent_transfer_target(
        target,
        "zelda",
        operation="clone",
        requested_agent_id="sheik",
    )
    assert custom["target_agent_id"] == "sheik"

    with pytest.raises(AgentMoveError, match="already used"):
        resolve_agent_transfer_target(
            target,
            "zelda",
            operation="clone",
            requested_agent_id="ZeLdA",
        )


def test_staged_transfer_reserves_target_id_for_later_preflight(tmp_path):
    _source, target, package_path = _roots(tmp_path)
    stage_agent_move(
        target,
        package_path.read_bytes(),
        expected_sha256=package_sha256(package_path),
        source_instance="HASHI1",
        target_instance="HASHI2",
        secret_passphrase="shared-secret",
        operation="move",
        target_agent_id="zelda",
    )

    resolved = resolve_agent_transfer_target(
        target,
        "zelda",
        operation="move",
    )

    assert resolved["target_agent_id"] == "zelda_1"


def test_terminal_transfer_journal_does_not_reserve_released_target_id(tmp_path):
    _source, target, package_path = _roots(tmp_path)
    staged = stage_agent_move(
        target,
        package_path.read_bytes(),
        expected_sha256=package_sha256(package_path),
        source_instance="HASHI1",
        target_instance="HASHI2",
        secret_passphrase="shared-secret",
        operation="move",
        target_agent_id="zelda",
    )
    state_path = (
        target
        / "state"
        / "agent_moves"
        / "incoming"
        / staged["package_id"]
        / "state.json"
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["status"] = "completed"
    _write_json(state_path, state)

    resolved = resolve_agent_transfer_target(
        target,
        "zelda",
        operation="move",
    )

    assert resolved["target_agent_id"] == "zelda"


def test_schema3_move_finalization_then_source_cleanup_is_complete(tmp_path):
    source, target, package_path = _roots(tmp_path)
    configured = json.loads((source / "agents.json").read_text())
    configured["agents"][0]["agent_lifecycle_id"] = "a" * 32
    _write_json(source / "agents.json", configured)
    create_agent_move_package(source, "zelda", package_path, source_instance="HASHI1",
        include_agent_secrets=True, secret_passphrase="shared-secret", schema_version=3)
    staged = stage_agent_move(
        target,
        package_path.read_bytes(),
        expected_sha256=package_sha256(package_path),
        source_instance="HASHI1",
        target_instance="HASHI2",
        secret_passphrase="shared-secret",
        operation="move",
        target_agent_id="zelda",
    )
    commit_agent_move(
        target,
        staged["package_id"],
        secret_passphrase="shared-secret",
    )
    deactivate_source_agent(
        source,
        "zelda",
        staged["package_id"],
        target_instance="HASHI2",
    )
    activate_agent_move(target, staged["package_id"])
    finalized = finalize_agent_move(
        target,
        staged["package_id"],
        runtime_online=True,
    )
    assert finalized["status"] == "completed"
    target_agent = json.loads((target / "agents.json").read_text())["agents"][0]
    assert target_agent["is_active"] is True
    assert "transfer_package_id" not in target_agent
    assert not (
        target
        / "state"
        / "agent_moves"
        / "incoming"
        / staged["package_id"]
        / "package.hashi-agent"
    ).exists()

    config = json.loads((source / "agents.json").read_text())
    config["groups"] = {
        "local": {"members": ["zelda", "anchor", "zelda@HASHI2"], "description": "zelda"},
        "dynamic": {"members": "@active", "exclude_from_broadcast": ["zelda", "anchor"]},
    }
    config["agents"][1]["display_name"] = "zelda"
    _write_json(source / "agents.json", config)

    source_package = read_agent_move_package(package_path, verify=True)

    cleaned = cleanup_source_agent(
        source,
        staged["package_id"],
        source_secret_keys=["zelda"],
        target_instance="HASHI2",
        target_agent_id="zelda",
        source_secret_snapshot={"zelda": "agent-token"},
        source_schedule_snapshot=source_package.schedules,
    )
    assert cleaned["status"] == "source_cleaned"
    departures = service.completed_agent_departures(source)
    assert departures["source_instance_id"] == "HASHI1"
    assert len(departures["events"]) == 1
    departure = departures["events"][0]
    assert departure["package_id"] == staged["package_id"]
    assert departure["agent_id"] == "zelda"
    assert departure["agent_lifecycle_id"] == cleaned["source_agent_lifecycle_id"]
    assert departure["state"] == "source_cleaned"
    assert service.completed_agent_departures(target)["events"] == []
    assert cleaned["conversation_history_retirement"]["skipped"] is True
    remaining = json.loads((source / "agents.json").read_text())
    assert remaining["groups"]["local"]["members"] == ["anchor", "zelda@HASHI2"]
    assert remaining["groups"]["local"]["description"] == "zelda"
    assert remaining["groups"]["dynamic"]["members"] == "@active"
    assert remaining["groups"]["dynamic"]["exclude_from_broadcast"] == ["anchor"]
    assert remaining["agents"][0]["display_name"] == "zelda"
    assert [
        row["name"]
        for row in json.loads((source / "agents.json").read_text())["agents"]
    ] == ["anchor"]
    assert not (source / "workspaces" / "zelda").exists()
    assert "zelda" not in json.loads((source / "secrets.json").read_text())
    assert not any(
        item.get("agent") == "zelda"
        for item in json.loads((source / "tasks.json").read_text())["heartbeats"]
    )
    assert moved_agent_destination(source, "ZELDA")["address"] == "zelda@HASHI2"

    # A real Move back preserves lifecycle identity. It must not resurrect old
    # groups, but a frontend may permit a deliberate new assignment after this receipt.
    target_config = json.loads((target / "agents.json").read_text())
    target_config["agents"].append(dict(remaining["agents"][0]))
    _write_json(target / "agents.json", target_config)
    return_path = tmp_path / "return.hashi-agent"
    create_agent_move_package(target, "zelda", return_path, source_instance="HASHI2",
        include_agent_secrets=True, secret_passphrase="shared-secret", schema_version=3)
    returning = stage_agent_move(source, return_path.read_bytes(),
        expected_sha256=package_sha256(return_path), source_instance="HASHI2",
        target_instance="HASHI1", secret_passphrase="shared-secret", operation="move",
        target_agent_id="zelda")
    commit_agent_move(source, returning["package_id"], secret_passphrase="shared-secret")
    assert service.completed_agent_departures(source).get("returns", []) == []
    deactivate_source_agent(target, "zelda", returning["package_id"], target_instance="HASHI1")
    activate_agent_move(source, returning["package_id"])
    finalize_agent_move(source, returning["package_id"], runtime_online=True)
    cleanup_source_agent(target, returning["package_id"], source_secret_keys=["zelda"],
        target_instance="HASHI1", target_agent_id="zelda")
    receipt = service.completed_agent_departures(source)["returns"][0]
    assert receipt["package_id"] == returning["package_id"]
    assert receipt["agent_lifecycle_id"] == departure["agent_lifecycle_id"]
    assert receipt["state"] == "target_completed"

    recreated = json.loads((source / "agents.json").read_text())
    recreated["agents"] = [row for row in recreated["agents"] if row["name"] != "zelda"]
    recreated["agents"].append({
        "name": "zelda", "workspace_dir": "workspaces/zelda-new",
        "type": "flex", "is_active": True,
    })
    _write_json(source / "agents.json", recreated)
    assert moved_agent_destination(source, "zelda") is None
    assert service.completed_agent_departures(source)["events"] == [departure], "recreation cannot erase the departure fact"
    service._record_moved_agent(source, agent_id="zelda", package_id="later-move",
        target_instance="HASHI2", target_agent_id="zelda", agent_lifecycle_id="c" * 32)
    assert service.completed_agent_departures(source)["events"] == [departure], "a later unfinished move cannot hide an earlier completed incarnation"


def test_schema5_move_cleanup_archives_source_history_and_rejects_unknown_owner(
    tmp_path,
    monkeypatch,
):
    source, _, _ = _roots(tmp_path)
    source_store = SessionStore(
        source / "state" / "sessions.sqlite3", instance_id="HASHI1"
    )
    source_session = source_store.resolve_session(
        owner_id="user:7",
        agent_id="zelda",
        surface="workbench",
        channel_key="default",
    )
    _complete_history(
        source_store,
        session_id=source_session["session_id"],
        agent_id="zelda",
        request_id="source-to-retire",
        question="recoverable source question",
        answer="recoverable source answer",
    )
    package_id = "schema5-history-cleanup"
    deactivate_source_agent(
        source,
        "zelda",
        package_id,
        target_instance="HASHI2",
        history_mode="move",
    )
    agents_before = (source / "agents.json").read_bytes()
    workspace_before = source / "workspaces" / "zelda" / "memory" / "facts.md"

    with pytest.raises(
        AgentMoveError, match="authorized conversation owner is not configured"
    ):
        cleanup_source_agent(
            source,
            package_id,
            source_secret_keys=["zelda"],
            target_instance="HASHI2",
            target_agent_id="zelda",
        )

    assert (source / "agents.json").read_bytes() == agents_before
    assert workspace_before.is_file()
    _authorize_owner(source)
    authorized_agents_before = (source / "agents.json").read_bytes()
    real_session_store = service._session_store

    class FailingRetirementStore:
        def archive_agent_conversation_sessions(self, **_kwargs):
            raise OSError("retirement storage unavailable")

    monkeypatch.setattr(
        service,
        "_session_store",
        lambda _root: FailingRetirementStore(),
    )
    with pytest.raises(OSError, match="retirement storage unavailable"):
        cleanup_source_agent(
            source,
            package_id,
            source_secret_keys=["zelda"],
            target_instance="HASHI2",
            target_agent_id="zelda",
        )
    assert (source / "agents.json").read_bytes() == authorized_agents_before
    assert workspace_before.is_file()

    monkeypatch.setattr(service, "_session_store", real_session_store)
    cleaned = cleanup_source_agent(
        source,
        package_id,
        source_secret_keys=["zelda"],
        target_instance="HASHI2",
        target_agent_id="zelda",
    )
    replayed = cleanup_source_agent(
        source,
        package_id,
        source_secret_keys=["zelda"],
        target_instance="HASHI2",
        target_agent_id="zelda",
    )

    retirement = cleaned["conversation_history_retirement"]
    assert retirement["session_ids"] == [source_session["session_id"]]
    assert retirement["replayed"] is False
    assert replayed == cleaned
    archived = source_store.get_session(
        source_session["session_id"], owner_id="user:7"
    )
    assert archived["status"] == "archived"
    assert archived["is_default"] is False
    assert [
        item["text"]
        for item in source_store.messages(
            source_session["session_id"], owner_id="user:7"
        )
    ] == ["recoverable source question", "recoverable source answer"]


def test_move_cleanup_without_telegram_retires_workbench_history(tmp_path):
    source, _, _ = _roots(tmp_path)
    store = SessionStore(source / "state/sessions.sqlite3", instance_id="HASHI1")
    session = store.ensure_default_session(owner_id="user:0", agent_id="zelda")
    accepted = store.accept_run(
        session_id=session["session_id"], owner_id="user:0", agent_id="zelda",
        request_id="workbench-move", text="owned history", source="workbench",
        idempotency_key="workbench-move",
    )
    store.mark_request_running(accepted.request_id, worker_id="fixture")
    store.finish_request(
        accepted.request_id, success=True, assistant_text="owned answer",
        assistant_source="fixture",
    )
    deactivate_source_agent(
        source, "zelda", "workbench-move", target_instance="HASHI2",
        history_mode="move",
    )
    result = cleanup_source_agent(
        source, "workbench-move", source_secret_keys=["zelda"],
        target_instance="HASHI2", target_agent_id="zelda",
    )
    assert result["conversation_history_retirement"]["session_ids"] == [
        session["session_id"]
    ]
    assert store.get_session(session["session_id"], owner_id="user:0")["status"] == "archived"


def test_move_cleanup_uses_source_owner_frozen_for_the_transfer(tmp_path):
    source, _, _ = _roots(tmp_path)
    _authorize_owner(source)
    store = SessionStore(source / "state/sessions.sqlite3", instance_id="HASHI1")
    original = store.ensure_default_session(owner_id="user:7", agent_id="zelda")
    _complete_history(
        store, session_id=original["session_id"], agent_id="zelda",
        request_id="frozen-source", question="original", answer="history",
    )
    other = store.ensure_default_session(owner_id="user:8", agent_id="zelda")
    deactivate_source_agent(
        source, "zelda", "frozen-owner", target_instance="HASHI2",
        history_mode="move", history_source_owner_id="user:7",
    )
    _authorize_owner(source, 8)
    result = cleanup_source_agent(
        source, "frozen-owner", source_secret_keys=["zelda"],
        target_instance="HASHI2", target_agent_id="zelda",
    )
    assert result["conversation_history_retirement"]["session_ids"] == [
        original["session_id"]
    ]
    assert store.get_session(original["session_id"], owner_id="user:7")["status"] == "archived"
    assert store.get_session(other["session_id"], owner_id="user:8")["status"] == "active"


def test_source_deactivation_rejects_history_mode_change_on_retry(tmp_path):
    source, _, _ = _roots(tmp_path)
    deactivate_source_agent(
        source,
        "zelda",
        "history-mode-conflict",
        target_instance="HASHI2",
        history_mode="move",
    )

    with pytest.raises(AgentMoveError, match="history mode conflicts"):
        deactivate_source_agent(
            source,
            "zelda",
            "history-mode-conflict",
            target_instance="HASHI2",
            history_mode="none",
        )


@pytest.mark.parametrize("legacy_nested", [False, True])
def test_schema3_clone_is_active_without_telegram_and_schedules_stay_disabled(
    tmp_path, legacy_nested,
):
    source, target, _ = _roots(tmp_path)
    if legacy_nested:
        config = json.loads((source / "agents.json").read_text())
        config["agents"][0]["agent_lifecycle_id"] = "a" * 32
        config["agents"][0]["extra"] = {"agent_lifecycle_id": "a" * 32, "custom": "keep"}
        _write_json(source / "agents.json", config)
    source_secrets = json.loads((source / "secrets.json").read_text())
    source_secrets["zelda_api_key"] = "safe-agent-key"
    _write_json(source / "secrets.json", source_secrets)
    package_path = tmp_path / "zelda-clone.hashi-agent"
    package = create_agent_move_package(
        source,
        "zelda",
        package_path,
        source_instance="HASHI1",
        operation="clone",
        include_agent_secrets=True,
        include_telegram_secret=False,
        secret_passphrase="shared-secret",
    )

    staged = stage_agent_move(
        target,
        package_path.read_bytes(),
        expected_sha256=package_sha256(package_path),
        source_instance="HASHI1",
        target_instance="HASHI2",
        secret_passphrase="shared-secret",
        operation="clone",
        target_agent_id="zelda_copy",
    )
    commit_agent_move(
        target,
        staged["package_id"],
        secret_passphrase="shared-secret",
    )
    activate_agent_move(target, staged["package_id"])
    finalized = finalize_agent_move(
        target,
        staged["package_id"],
        runtime_online=True,
    )

    assert finalized["status"] == "completed"
    target_agent = json.loads((target / "agents.json").read_text())["agents"][0]
    assert target_agent["name"] == "zelda_copy"
    assert target_agent["is_active"] is True
    target_secrets = json.loads((target / "secrets.json").read_text())
    assert target_agent["telegram_token_key"] not in target_secrets
    assert "zelda_copy" not in target_secrets
    assert target_secrets["zelda_copy_api_key"] == "safe-agent-key"
    imported = [
        item
        for item in json.loads((target / "tasks.json").read_text())["heartbeats"]
        if item.get("agent") == "zelda_copy"
    ]
    assert len(imported) == 1
    assert imported[0]["enabled"] is False
    assert "import_package_id" not in imported[0]
    assert json.loads((source / "agents.json").read_text())["agents"][0][
        "is_active"
    ] is True
    assert package.access_requirements["telegram_secret_included"] is False
    from orchestrator.agent_incarnation import lifecycle_id_from_config
    from orchestrator.config import ConfigManager
    _, loaded, _ = ConfigManager(
        target / "agents.json", target / "secrets.json", bridge_home=target, code_root=target
    ).load()
    cloned = next(agent for agent in loaded if agent.name == "zelda_copy")
    assert lifecycle_id_from_config(cloned) == target_agent["agent_lifecycle_id"]
    if legacy_nested:
        assert target_agent["agent_lifecycle_id"] != "a" * 32
        assert target_agent["extra"] == {"custom": "keep"}
        assert json.loads((source / "agents.json").read_text())["agents"][0]["extra"] == {
            "agent_lifecycle_id": "a" * 32, "custom": "keep"
        }


@pytest.mark.parametrize("runtime_change", ["config", "secret", "schedule", "workspace"])
def test_finalization_accepts_live_target_changes_after_activation(
    tmp_path, runtime_change
):
    source, target, _ = _roots(tmp_path)
    source_secrets = json.loads((source / "secrets.json").read_text())
    source_secrets["zelda_api_key"] = "safe-agent-key"
    _write_json(source / "secrets.json", source_secrets)
    package_path = tmp_path / "zelda-verified.hashi-agent"
    create_agent_move_package(
        source,
        "zelda",
        package_path,
        source_instance="HASHI1",
        operation="clone",
        include_agent_secrets=True,
        include_telegram_secret=False,
        secret_passphrase="shared-secret",
    )
    staged = stage_agent_move(
        target,
        package_path.read_bytes(),
        expected_sha256=package_sha256(package_path),
        source_instance="HASHI1",
        target_instance="HASHI2",
        secret_passphrase="shared-secret",
        operation="clone",
        target_agent_id="zelda_copy",
    )
    commit_agent_move(
        target,
        staged["package_id"],
        secret_passphrase="shared-secret",
    )
    activate_agent_move(target, staged["package_id"])

    if runtime_change == "config":
        agents = json.loads((target / "agents.json").read_text())
        agents["agents"][0]["access_scope"] = "workspace"
        _write_json(target / "agents.json", agents)
    elif runtime_change == "secret":
        secrets = json.loads((target / "secrets.json").read_text())
        secrets["zelda_copy_api_key"] = "changed"
        _write_json(target / "secrets.json", secrets)
    elif runtime_change == "schedule":
        tasks = json.loads((target / "tasks.json").read_text())
        imported = next(
            item
            for item in tasks["heartbeats"]
            if item.get("agent") == "zelda_copy"
        )
        imported["schedule"] = "changed"
        _write_json(target / "tasks.json", tasks)
    else:
        memory = target / "workspaces" / "zelda_copy" / "memory" / "facts.md"
        memory.write_text("normal Memory+ rollover", encoding="utf-8")

    finalized = finalize_agent_move(
        target,
        staged["package_id"],
        runtime_online=True,
    )

    assert finalized["status"] == "completed"
    assert finalized["target_verification"] == {"runtime_online": True}
    if runtime_change == "workspace":
        assert memory.read_text(encoding="utf-8") == "normal Memory+ rollover"


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


def test_source_deactivation_reads_legacy_config_and_normalizes_authoritative_writes(tmp_path):
    source, _, _ = _roots(tmp_path)
    for name in ("agents.json", "tasks.json"):
        _rewrite_legacy_json(source / name)

    deactivate_source_agent(
        source,
        "zelda",
        "legacy-bytes-package",
        target_instance="HASHI2",
    )

    for name in ("agents.json", "tasks.json"):
        raw = (source / name).read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf")
        assert b"\r" not in raw
        assert raw.endswith(b"\n")


def test_source_deactivation_does_not_overwrite_an_intervening_config_writer(
    tmp_path, monkeypatch
):
    source, _, _ = _roots(tmp_path)
    agents_path = (source / "agents.json").resolve()
    actual_write = service.write_agent_config_json
    injected = False

    def interleaved(path, payload, **kwargs):
        nonlocal injected
        if Path(path).resolve() == agents_path and not injected:
            injected = True
            winner = read_config_json(path)
            winner["global"]["concurrent_extension"] = "kept"
            actual_write(path, winner)
        return actual_write(path, payload, **kwargs)

    monkeypatch.setattr(service, "write_agent_config_json", interleaved)

    with pytest.raises(ConfigConflictError):
        deactivate_source_agent(
            source,
            "zelda",
            "stale-source-package",
            target_instance="HASHI2",
        )

    assert injected
    current = read_config_json(agents_path)
    assert current["global"]["concurrent_extension"] == "kept"
    zelda = next(row for row in current["agents"] if row["name"] == "zelda")
    assert zelda["is_active"] is True


def test_imported_source_can_move_again_and_restore_import_ownership(tmp_path):
    source, _, _ = _roots(tmp_path)
    old_package_id = "imported-package-id"
    new_package_id = "return-package-id"
    agents = json.loads((source / "agents.json").read_text())
    imported = agents["agents"][0]
    imported.update(
        {
            "transfer_import_state": "activated_pending_reboot",
            "transfer_package_id": old_package_id,
            "transfer_source_instance": "HASHI2",
        }
    )
    _write_json(source / "agents.json", agents)
    _write_json(
        source
        / "state"
        / "agent_moves"
        / "incoming"
        / old_package_id
        / "state.json",
        {
            "package_id": old_package_id,
            "agent_id": "zelda",
            "status": "activated_pending_reboot",
        },
    )

    disabled = deactivate_source_agent(
        source,
        "zelda",
        new_package_id,
        target_instance="HASHI2",
    )

    assert disabled["previous_transfer_fields"] == {
        "transfer_import_state": "activated_pending_reboot",
        "transfer_package_id": old_package_id,
        "transfer_source_instance": "HASHI2",
    }
    moved = json.loads((source / "agents.json").read_text())["agents"][0]
    assert moved["transfer_package_id"] == new_package_id
    assert moved["transfer_state"] == "moved_out_pending_reboot"
    assert "transfer_import_state" not in moved
    assert "transfer_source_instance" not in moved

    restored = restore_source_agent(source, new_package_id)

    assert restored["status"] == "source_restored_pending_reboot"
    restored_agent = json.loads((source / "agents.json").read_text())["agents"][0]
    assert restored_agent["is_active"] is True
    assert restored_agent["transfer_import_state"] == "activated_pending_reboot"
    assert restored_agent["transfer_package_id"] == old_package_id
    assert restored_agent["transfer_source_instance"] == "HASHI2"
    assert "transfer_state" not in restored_agent
    assert "transfer_target" not in restored_agent


def test_source_with_unproven_prior_move_owner_stays_blocked(tmp_path):
    source, _, _ = _roots(tmp_path)
    agents = json.loads((source / "agents.json").read_text())
    agents["agents"][0].update(
        {
            "transfer_import_state": "activated_pending_reboot",
            "transfer_package_id": "unproven-package",
            "transfer_source_instance": "HASHI2",
        }
    )
    _write_json(source / "agents.json", agents)

    with pytest.raises(AgentMoveError, match="already associated"):
        deactivate_source_agent(
            source,
            "zelda",
            "new-package",
            target_instance="HASHI2",
        )


def test_imported_source_restore_retry_recognises_restored_owner(
    tmp_path, monkeypatch
):
    source, _, _ = _roots(tmp_path)
    old_package_id = "imported-package-retry"
    new_package_id = "return-package-retry"
    agents = json.loads((source / "agents.json").read_text())
    agents["agents"][0].update(
        {
            "transfer_import_state": "activated_pending_reboot",
            "transfer_package_id": old_package_id,
            "transfer_source_instance": "HASHI2",
        }
    )
    _write_json(source / "agents.json", agents)
    _write_json(
        source
        / "state"
        / "agent_moves"
        / "incoming"
        / old_package_id
        / "state.json",
        {
            "package_id": old_package_id,
            "agent_id": "zelda",
            "status": "activated_pending_reboot",
        },
    )
    deactivate_source_agent(
        source,
        "zelda",
        new_package_id,
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
        restore_source_agent(source, new_package_id)
    monkeypatch.setattr(service, "_atomic_json", original_atomic)

    restored = restore_source_agent(source, new_package_id)

    assert restored["status"] == "source_restored_pending_reboot"
    restored_agent = json.loads((source / "agents.json").read_text())["agents"][0]
    assert restored_agent["transfer_package_id"] == old_package_id
    assert restored_agent["transfer_import_state"] == "activated_pending_reboot"


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
    lifecycle_id = disabled["source_agent_lifecycle_id"]
    assert len(lifecycle_id) == 32
    assert json.loads((source / "agents.json").read_text())["agents"][0][
        "agent_lifecycle_id"
    ] == lifecycle_id
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
    assert len(json.loads((source / "agents.json").read_text())["agents"]) == 2
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


def test_clone_does_not_bind_concurrently_created_default_telegram_key(tmp_path, monkeypatch):
    source, target, _ = _roots(tmp_path)
    package_path = tmp_path / "concurrent-clone.hashi-agent"
    package = create_agent_move_package(
        source, "zelda", package_path, source_instance="HASHI1", operation="clone",
        include_agent_secrets=True, secret_passphrase="shared-secret",
        desired_target_active=True,
    )
    staged = stage_agent_move(
        target, package_path.read_bytes(), expected_sha256=package_sha256(package_path),
        source_instance="HASHI1", operation="clone", target_agent_id="zelda-copy",
        secret_passphrase="shared-secret",
    )
    original = service._write_recovery_snapshots

    def another_writer_after_selection(root, record_dir):
        result = original(root, record_dir)
        secrets_path = target / "secrets.json"
        secrets = json.loads(secrets_path.read_text())
        secrets["zelda-copy"] = "another-writers-credential"
        _write_json(secrets_path, secrets)
        return result

    monkeypatch.setattr(service, "_write_recovery_snapshots", another_writer_after_selection)
    commit_agent_move(target, staged["package_id"], secret_passphrase="shared-secret")
    secrets = json.loads((target / "secrets.json").read_text())
    row = next(row for row in json.loads((target / "agents.json").read_text())["agents"]
               if row["name"] == "zelda-copy")
    assert secrets["zelda-copy"] == "another-writers-credential"
    assert row["telegram_token_key"] != "zelda-copy"
    assert row["telegram_token_key"] not in secrets
    assert activate_agent_move(target, package.package_id)["status"] == "activated_pending_reboot"



@pytest.mark.asyncio
async def test_inactive_clone_with_conflict_can_set_own_verified_bot_later(tmp_path):
    from orchestrator.telegram_credential import TelegramCredentialService

    source, target, _ = _roots(tmp_path)
    package_path = tmp_path / "inactive-clone-setup.hashi-agent"
    package = create_agent_move_package(
        source, "zelda", package_path, source_instance="HASHI1", operation="clone",
        include_agent_secrets=True, secret_passphrase="shared-secret",
        desired_target_active=False, retain_telegram_backup=True,
    )
    secrets_path = target / "secrets.json"
    existing = json.loads(secrets_path.read_text())
    existing["zelda-copy"] = "other-existing-credential"
    _write_json(secrets_path, existing)
    staged = stage_agent_move(
        target, package_path.read_bytes(), expected_sha256=package_sha256(package_path),
        source_instance="HASHI1", operation="clone", target_agent_id="zelda-copy",
        secret_passphrase="shared-secret",
    )
    commit_agent_move(target, staged["package_id"], secret_passphrase="shared-secret")
    row = next(row for row in json.loads((target / "agents.json").read_text())["agents"]
               if row["name"] == "zelda-copy")
    token_key = row["telegram_token_key"]
    token = "123456:abcdefghijklmnopqrstuvwxyz_ABCDEFGHI"

    async def verify(_token):
        return {"id": 123456, "username": "synthetic_bot"}

    credentials = TelegramCredentialService(
        config_path=target / "agents.json", secrets_path=secrets_path,
        state_root=target, verify_bot=verify,
    )
    initial = credentials.status("zelda-copy")
    assert initial["configured"] is False
    draft = await credentials.replace(
        "zelda-copy", token=token, expected_revision=initial["credential_revision"],
        replace_existing=False, save_unverified=True,
    )
    assert draft["pending_verification"] is True
    verified = await credentials.verify_pending(
        "zelda-copy", expected_revision=draft["credential_revision"],
    )
    secrets = json.loads(secrets_path.read_text())
    assert verified["configured"] is True and verified["pending_verification"] is False
    assert secrets[token_key] == token
    assert secrets["zelda-copy"] == "other-existing-credential"
    assert next(row for row in json.loads((target / "agents.json").read_text())["agents"]
                if row["name"] == "zelda-copy")["is_active"] is False
    assert package.manifest["retain_telegram_backup"] is True

# HASHI-Fingerprint: hfp_fd528f27466e42869674ed9c995eb9aa



def test_move_without_history_keeps_all_source_sessions_and_negotiates_receiver(tmp_path):
    from orchestrator.agent_move import coordinator

    source, target, _ = _roots(tmp_path)
    store = SessionStore(source / "state/sessions.sqlite3", instance_id="HASHI1")
    sessions = []
    for owner in ("user:7", "user:8"):
        session = store.ensure_default_session(owner_id=owner, agent_id="zelda")
        _complete_history(
            store, session_id=session["session_id"], agent_id="zelda",
            request_id="retained-" + owner, owner_id=owner,
            question="source question " + owner, answer="source answer " + owner,
        )
        sessions.append((owner, session["session_id"]))
    import hashlib
    payload = b"source attachment bytes"
    attachment = store.stage_attachment(
        session_id=sessions[0][1], owner_id=sessions[0][0], filename="retained.txt",
        media_type="text/plain", size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(), idempotency_key="retained-attachment",
    )
    store.upload_attachment_bytes(
        session_id=sessions[0][1], owner_id=sessions[0][0],
        attachment_id=attachment["attachment_id"], payload=payload,
    )
    store.commit_attachment(
        session_id=sessions[0][1], owner_id=sessions[0][0],
        attachment_id=attachment["attachment_id"],
    )
    path = tmp_path / "retain-source.hashi-agent"
    package = create_agent_move_package(
        source, "zelda", path, source_instance="HASHI1",
        operation="move", transfer_mode="workspace",
        history_mode="retain_source", include_agent_secrets=True,
        secret_passphrase="shared-secret",
    )
    assert package.manifest["history_mode"] == "retain_source"
    assert package.conversation_continuity is None
    capability = "agent_move_retain_source_history_v1"
    assert capability in package.manifest["required_receiver_capabilities"]
    client = coordinator._LocalAgentTransferClient(target, "HASHI2", "shared-secret")
    client.ensure_package_compatible(package)
    client.capabilities["capabilities"].remove(capability)
    with pytest.raises(AgentMoveError, match="lacks capabilities"):
        client.ensure_package_compatible(package)

    staged = stage_agent_move(
        target, path.read_bytes(), expected_sha256=package_sha256(path),
        source_instance="HASHI1", secret_passphrase="shared-secret",
        operation="move", target_agent_id="zelda",
    )
    committed = commit_agent_move(
        target, staged["package_id"], secret_passphrase="shared-secret",
    )
    assert committed["history_mode"] == "retain_source"
    deactivate_source_agent(
        source, "zelda", package.package_id, target_instance="HASHI2",
        history_mode="retain_source",
    )
    with pytest.raises(AgentMoveError, match="history mode conflicts"):
        deactivate_source_agent(
            source, "zelda", package.package_id, target_instance="HASHI2",
            history_mode="move",
        )
    cleaned = cleanup_source_agent(
        source, package.package_id, source_secret_keys=["zelda"],
        target_instance="HASHI2", target_agent_id="zelda",
    )
    retention = cleaned["conversation_history_retirement"]
    assert retention["retained_on_source"] is True
    assert set(retention["session_ids"]) == {session_id for _, session_id in sessions}
    for owner, session_id in sessions:
        assert store.get_session(session_id, owner_id=owner)["status"] == "archived"
        assert [row["text"] for row in store.messages(session_id, owner_id=owner)] == [
            "source question " + owner, "source answer " + owner,
        ]
        fresh = store.resolve_primary_session(owner_id=owner, agent_id="zelda", establish=True)
        assert fresh["session_id"] != session_id
        assert store.messages(fresh["session_id"], owner_id=owner) == []
    _, retained_payload = store.attachment_bytes(
        session_id=sessions[0][1], owner_id=sessions[0][0],
        attachment_id=attachment["attachment_id"],
    )
    assert retained_payload == payload
    target_store = SessionStore(target / "state/sessions.sqlite3", instance_id="HASHI2")
    assert target_store.list_sessions(owner_id="user:7", agent_id="zelda") == []
    assert cleanup_source_agent(
        source, package.package_id, source_secret_keys=["zelda"],
        target_instance="HASHI2", target_agent_id="zelda",
    ) == cleaned



def test_retain_source_archival_recovers_after_cleanup_failure_without_rearchiving(tmp_path, monkeypatch):
    source, _, _ = _roots(tmp_path)
    store = SessionStore(source / "state/sessions.sqlite3", instance_id="HASHI1")
    session = store.ensure_default_session(owner_id="user:7", agent_id="zelda")
    _complete_history(
        store, session_id=session["session_id"], agent_id="zelda",
        request_id="retention-retry", question="preserve question", answer="preserve answer",
    )
    deactivate_source_agent(
        source, "zelda", "retention-retry", target_instance="HASHI2", history_mode="retain_source",
    )
    agents_path = source / "agents.json"
    original = service._atomic_json
    failed = False

    def fail_after_history(path, payload, **kwargs):
        nonlocal failed
        if Path(path) == agents_path and not failed:
            failed = True
            raise OSError("simulated source registry failure")
        return original(path, payload, **kwargs)

    monkeypatch.setattr(service, "_atomic_json", fail_after_history)
    with pytest.raises(OSError, match="simulated source registry failure"):
        cleanup_source_agent(
            source, "retention-retry", source_secret_keys=["zelda"],
            target_instance="HASHI2", target_agent_id="zelda",
        )
    assert store.get_session(session["session_id"], owner_id="user:7")["status"] == "archived"
    assert (source / "workspaces" / "zelda" / "memory" / "facts.md").is_file()
    fresh = store.ensure_default_session(owner_id="user:7", agent_id="zelda")
    assert fresh["session_id"] != session["session_id"]

    result = cleanup_source_agent(
        source, "retention-retry", source_secret_keys=["zelda"],
        target_instance="HASHI2", target_agent_id="zelda",
    )
    receipt = result["conversation_history_retirement"]
    assert receipt["replayed"] is True
    assert receipt["session_ids"] == [session["session_id"]]
    assert store.get_session(fresh["session_id"], owner_id="user:7")["status"] == "active"
    assert [row["text"] for row in store.messages(session["session_id"], owner_id="user:7")] == [
        "preserve question", "preserve answer",
    ]


def test_identity_move_retains_pending_delivery_and_source_workspace_on_cleanup(tmp_path):
    source, target, _ = _roots(tmp_path)
    _authorize_owner(source)
    _authorize_owner(target)
    lifecycle_id = "4" * 32
    config = json.loads((source / "agents.json").read_text())
    config["agents"][0]["agent_lifecycle_id"] = lifecycle_id
    _write_json(source / "agents.json", config)
    undelivered = source / "workspaces" / "zelda" / "undelivered"
    undelivered.mkdir()
    (undelivered / "req-owned.md").write_text("preserve this response", encoding="utf-8")
    _write_json(undelivered / "req-owned.json", {"request_id": "req-owned", "chat_id": 123})
    delivery_record = {
        "owner": {"instance_id": "HASHI1", "agent_lifecycle_id": lifecycle_id,
                  "telegram_bot_fingerprint": telegram_bot_fingerprint("agent-token")},
        "status": "blocked", "incident_id": "owned-incident",
        "per_chat": {"123": {"undelivered_request_ids": ["req-owned"]}},
    }
    _write_json(source / "state" / "telegram_delivery_health.json",
                {"version": 2, "agents": {"zelda": delivery_record}, "quarantine": []})
    package_path = tmp_path / "identity-pending.hashi-agent"
    package = create_agent_move_package(
        source, "zelda", package_path, source_instance="HASHI1",
        include_agent_secrets=True, secret_passphrase="shared-secret",
        transfer_mode="identity_memory", history_mode="retain_source",
    )
    staged = stage_agent_move(
        target, package_path.read_bytes(), expected_sha256=package_sha256(package_path),
        source_instance="HASHI1", secret_passphrase="shared-secret", operation="move",
    )
    assert any("undelivered" in warning and "source" in warning for warning in staged["warnings"])
    committed = commit_agent_move(target, staged["package_id"], secret_passphrase="shared-secret")
    assert committed["telegram_delivery_state"]["imported"] is False
    assert not (target / "workspaces" / "zelda" / "undelivered").exists()

    deactivate_source_agent(source, "zelda", package.package_id,
                            target_instance="HASHI2", history_mode="retain_source")
    cleaned = cleanup_source_agent(
        source, package.package_id, source_secret_keys=["zelda"],
        target_instance="HASHI2", target_agent_id="zelda",
        source_secret_snapshot={"zelda": "agent-token"}, source_schedule_snapshot=package.schedules,
    )
    retained = Path(cleaned["retained_source_workspace"])
    assert (retained / "undelivered" / "req-owned.md").read_text() == "preserve this response"
    assert (retained / "undelivered" / "req-owned.json").is_file()
    retained_delivery = cleaned["retained_source_delivery"]
    assert retained_delivery["record"]["owner"] == delivery_record["owner"]
    assert retained_delivery["record"]["per_chat"] == delivery_record["per_chat"]
    assert retained_delivery["pending_count"] == 1
    assert retained_delivery["workspace_path"] == str(retained)
    repeated = cleanup_source_agent(
        source, package.package_id, source_secret_keys=[],
        target_instance="HASHI2", target_agent_id="zelda",
    )
    assert repeated["retained_source_delivery"] == retained_delivery
    assert "zelda" not in json.loads((source / "state" / "telegram_delivery_health.json").read_text())["agents"]


def test_delivery_retention_failure_leaves_owned_record_for_safe_retry(tmp_path, monkeypatch):
    source, _, package_path = _roots(tmp_path)
    lifecycle_id = "4" * 32
    config = json.loads((source / "agents.json").read_text())
    config["agents"][0]["agent_lifecycle_id"] = lifecycle_id
    _write_json(source / "agents.json", config)
    record = {
        "owner": {"instance_id": "HASHI1", "agent_lifecycle_id": lifecycle_id,
                  "telegram_bot_fingerprint": telegram_bot_fingerprint("agent-token")},
        "per_chat": {"123": {"undelivered_request_ids": ["req-owned"]}},
    }
    _write_json(source / "state" / "telegram_delivery_health.json",
                {"version": 2, "agents": {"zelda": record}, "quarantine": []})
    package = read_agent_move_package(package_path, verify=True)
    deactivate_source_agent(source, "zelda", package.package_id, target_instance="HASHI2")
    original = service._atomic_json
    failed = False

    def fail_before_delivery_retirement(path, value, **kwargs):
        nonlocal failed
        if not failed and isinstance(value, dict) and value.get("retained_source_delivery"):
            failed = True
            raise OSError("injected journal failure")
        return original(path, value, **kwargs)

    monkeypatch.setattr(service, "_atomic_json", fail_before_delivery_retirement)
    with pytest.raises(OSError, match="injected journal failure"):
        cleanup_source_agent(source, package.package_id, source_secret_keys=["zelda"],
                             target_instance="HASHI2", target_agent_id="zelda")
    health = json.loads((source / "state" / "telegram_delivery_health.json").read_text())
    assert health["agents"]["zelda"]["per_chat"] == record["per_chat"]
    monkeypatch.setattr(service, "_atomic_json", original)
    cleaned = cleanup_source_agent(source, package.package_id, source_secret_keys=["zelda"],
                                   target_instance="HASHI2", target_agent_id="zelda")
    assert cleaned["retained_source_delivery"]["record"]["owner"] == record["owner"]
    assert cleaned["retained_source_delivery"]["pending_count"] == 1


def test_inactive_clone_retains_encrypted_bot_backup_without_installing_consumer(tmp_path):
    from orchestrator.agent_move import service
    from orchestrator.agent_move.package import _decrypt_json

    source, target, _ = _roots(tmp_path)
    package_path = tmp_path / "inactive-clone.hashi-agent"
    package = create_agent_move_package(
        source, "zelda", package_path, source_instance="HASHI1", operation="clone",
        include_agent_secrets=True, secret_passphrase="shared-secret",
        desired_target_active=False, retain_telegram_backup=True,
    )
    staged = stage_agent_move(
        target, package_path.read_bytes(), expected_sha256=package_sha256(package_path),
        source_instance="HASHI1", operation="clone", target_agent_id="zelda-copy",
        secret_passphrase="shared-secret",
    )
    assert staged["desired_target_active"] is False
    committed = commit_agent_move(target, staged["package_id"], secret_passphrase="shared-secret")
    assert committed["status"] == "committed_inactive"
    backup = committed["retained_telegram_backup"]
    blob = target / backup["storage_path"]
    assert backup["status"] == "quarantined" and blob.is_file()
    assert b"agent-token" not in blob.read_bytes()
    transport_blob = target / backup["transport_backup_path"]
    assert _decrypt_json(transport_blob.read_bytes(), "shared-secret")["zelda"] == "agent-token"
    assert service._recover_quarantined_telegram_token(target, package.package_id) == "agent-token"
    assert "zelda-copy" not in json.loads((target / "secrets.json").read_text())
    target_row = next(row for row in json.loads((target / "agents.json").read_text())["agents"]
                      if row["name"] == "zelda-copy")
    assert target_row["is_active"] is False
    assert target_row["telegram_token_key"] not in json.loads((target / "secrets.json").read_text())
    with pytest.raises(AgentMoveError, match="inactive"):
        activate_agent_move(target, package.package_id)
    finalized = finalize_agent_move(target, package.package_id, runtime_online=False)
    assert finalized["status"] == "completed" and blob.is_file()
    assert "zelda-copy" not in json.loads((target / "secrets.json").read_text())


@pytest.mark.parametrize("insert_at", ["before_stage", "after_stage"])
def test_inactive_clone_backup_does_not_bind_or_replace_existing_target_credential(
    tmp_path, insert_at,
):
    source, target, _ = _roots(tmp_path)
    package_path = tmp_path / "inactive-clone-conflict.hashi-agent"
    create_agent_move_package(
        source, "zelda", package_path, source_instance="HASHI1", operation="clone",
        include_agent_secrets=True, secret_passphrase="shared-secret",
        desired_target_active=False, retain_telegram_backup=True,
    )
    secrets_path = target / "secrets.json"

    def add_existing():
        secrets = json.loads(secrets_path.read_text())
        secrets["zelda-copy"] = "other-existing-credential"
        _write_json(secrets_path, secrets)

    if insert_at == "before_stage":
        add_existing()
    staged = stage_agent_move(
        target, package_path.read_bytes(), expected_sha256=package_sha256(package_path),
        source_instance="HASHI1", operation="clone", target_agent_id="zelda-copy",
        secret_passphrase="shared-secret",
    )
    if insert_at == "after_stage":
        add_existing()
    committed = commit_agent_move(target, staged["package_id"], secret_passphrase="shared-secret")
    secrets = json.loads(secrets_path.read_text())
    row = next(row for row in json.loads((target / "agents.json").read_text())["agents"]
               if row["name"] == "zelda-copy")
    assert secrets["zelda-copy"] == "other-existing-credential"
    assert row["telegram_token_key"] != "zelda-copy"
    assert row["telegram_token_key"] not in secrets
    assert row["is_active"] is False
    assert committed["retained_telegram_backup"]["live_connector"] is False
    assert (target / committed["retained_telegram_backup"]["storage_path"]).is_file()


@pytest.mark.parametrize("collision", ["existing_secret", "other_agent_reference"])
def test_active_clone_never_adopts_an_unrelated_target_bot_credential(tmp_path, collision):
    source, target, _ = _roots(tmp_path)
    package_path = tmp_path / "active-clone-conflict.hashi-agent"
    package = create_agent_move_package(
        source, "zelda", package_path, source_instance="HASHI1", operation="clone",
        include_agent_secrets=True, secret_passphrase="shared-secret",
        desired_target_active=True,
    )
    secrets_path = target / "secrets.json"
    config_path = target / "agents.json"
    if collision == "existing_secret":
        secrets = json.loads(secrets_path.read_text())
        secrets["zelda-copy"] = "other-existing-credential"
        _write_json(secrets_path, secrets)
    else:
        config = json.loads(config_path.read_text())
        config["agents"].append({"name": "other", "telegram_token_key": "zelda-copy"})
        _write_json(config_path, config)
    staged = stage_agent_move(
        target, package_path.read_bytes(), expected_sha256=package_sha256(package_path),
        source_instance="HASHI1", operation="clone", target_agent_id="zelda-copy",
        secret_passphrase="shared-secret",
    )
    committed = commit_agent_move(target, staged["package_id"], secret_passphrase="shared-secret")
    row = next(row for row in json.loads(config_path.read_text())["agents"]
               if row["name"] == "zelda-copy")
    secrets = json.loads(secrets_path.read_text())
    assert row["telegram_token_key"] != "zelda-copy"
    assert row["telegram_token_key"] not in secrets
    if collision == "existing_secret":
        assert secrets["zelda-copy"] == "other-existing-credential"
    else:
        assert next(row for row in json.loads(config_path.read_text())["agents"]
                    if row["name"] == "other")["telegram_token_key"] == "zelda-copy"
    assert committed["status"] == "committed_inactive"
    activated = activate_agent_move(target, package.package_id)
    assert activated["status"] == "activated_pending_reboot"
    row = next(row for row in json.loads(config_path.read_text())["agents"]
               if row["name"] == "zelda-copy")
    assert row["telegram_token_key"] not in json.loads(secrets_path.read_text())


def test_inactive_clone_backup_publish_failure_is_retryable(tmp_path, monkeypatch):
    from orchestrator.agent_move import service
    source, target, _ = _roots(tmp_path)
    package_path = tmp_path / "inactive-retry.hashi-agent"
    create_agent_move_package(
        source, "zelda", package_path, source_instance="HASHI1", operation="clone",
        include_agent_secrets=True, secret_passphrase="shared-secret",
        desired_target_active=False, retain_telegram_backup=True,
    )
    staged = stage_agent_move(
        target, package_path.read_bytes(), expected_sha256=package_sha256(package_path),
        source_instance="HASHI1", operation="clone", target_agent_id="zelda-copy",
        secret_passphrase="shared-secret",
    )
    original_link = service.os.link
    def fail_publication(src, dst, *args, **kwargs):
        if str(dst).endswith("agent.enc"):
            raise OSError("injected publication failure")
        return original_link(src, dst, *args, **kwargs)
    monkeypatch.setattr(service.os, "link", fail_publication)
    with pytest.raises(OSError, match="injected publication failure"):
        commit_agent_move(target, staged["package_id"], secret_passphrase="shared-secret")
    backup_dir = target / "state" / "agent_moves" / "incoming" / staged["package_id"] / "retained-telegram-backup"
    assert not (backup_dir / "agent.enc").exists()
    assert not (target / "workspaces" / "zelda-copy").exists()
    monkeypatch.setattr(service.os, "link", original_link)
    result = commit_agent_move(target, staged["package_id"], secret_passphrase="shared-secret")
    assert result["retained_telegram_backup"]["status"] == "quarantined"
    assert (target / result["retained_telegram_backup"]["storage_path"]).is_file()


def test_receiver_advertises_member_budget_and_staged_package_survives_lowering(
    tmp_path, monkeypatch,
):
    from orchestrator.agent_move import package as owner

    source, target, _previous = _roots(tmp_path)
    for index in range(6):
        (source / "workspaces/zelda" / f"file-{index}.txt").write_text(str(index))
    monkeypatch.setattr(owner, "MAX_ARCHIVE_MEMBERS", 8)
    monkeypatch.setenv("HASHI_AGENT_MOVE_MAX_ARCHIVE_MEMBERS", "64")
    assert service.receiver_capabilities(target)["max_archive_members"] == 64
    archive = tmp_path / "large-member-count.hashi-agent"
    package = create_agent_move_package(
        source, "zelda", archive, operation="move", transfer_mode="workspace",
        history_mode="retain_source", max_archive_members=64,
    )
    count = len(package.names)
    assert count > 8

    monkeypatch.setenv("HASHI_AGENT_MOVE_MAX_ARCHIVE_MEMBERS", str(count - 1))
    with pytest.raises(AgentMoveError, match="member"):
        stage_agent_move(target, archive, expected_sha256=package_sha256(archive),
                         source_instance="HASHI1", target_instance="HASHI2")
    assert json.loads((target / "agents.json").read_text())["agents"] == []
    monkeypatch.setenv("HASHI_AGENT_MOVE_MAX_ARCHIVE_MEMBERS", str(count))
    staged = stage_agent_move(target, archive, expected_sha256=package_sha256(archive),
                              source_instance="HASHI1", target_instance="HASHI2")
    monkeypatch.setenv("HASHI_AGENT_MOVE_MAX_ARCHIVE_MEMBERS", "1")
    committed = commit_agent_move(target, staged["package_id"])
    assert committed["status"] == "committed_inactive"


@pytest.mark.parametrize("bad", ["0", "-1", "1.2", "none"])
def test_receiver_rejects_invalid_configured_archive_member_budget(tmp_path, monkeypatch, bad):
    _source, target, _archive = _roots(tmp_path)
    monkeypatch.setenv("HASHI_AGENT_MOVE_MAX_ARCHIVE_MEMBERS", bad)
    with pytest.raises(AgentMoveError, match="member"):
        service.receiver_capabilities(target)


def test_legacy_staged_package_survives_lower_member_configuration(tmp_path, monkeypatch):
    from orchestrator.agent_move import package as owner

    source, target, archive = _roots(tmp_path)
    monkeypatch.setattr(owner, "MAX_ARCHIVE_MEMBERS", 100)
    staged = stage_agent_move(target, archive, expected_sha256=package_sha256(archive),
                              source_instance="HASHI1", target_instance="HASHI2",
                              secret_passphrase="shared-secret")
    records = list((target / "state/agent_moves/incoming").glob("*/state.json"))
    assert len(records) == 1
    record = json.loads(records[0].read_text())
    record.pop("archive_member_count")
    record.pop("archive_unpacked_bytes")
    record.pop("archive_workspace_bytes")
    records[0].write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setenv("HASHI_AGENT_MOVE_MAX_ARCHIVE_MEMBERS", "1")
    monkeypatch.setenv("HASHI_AGENT_MOVE_MAX_UNPACKED_BYTES", "1")
    monkeypatch.setenv("HASHI_AGENT_MOVE_MAX_WORKSPACE_BYTES", "1")
    committed = commit_agent_move(target, staged["package_id"], secret_passphrase="shared-secret")
    assert committed["status"] == "committed_inactive"

def test_receiver_advertises_and_enforces_exact_compressed_budget(tmp_path, monkeypatch):
    _source, target, archive = _roots(tmp_path)
    size = archive.stat().st_size
    monkeypatch.setenv("HASHI_AGENT_MOVE_MAX_PACKAGE_BYTES", str(size - 1))
    assert service.receiver_capabilities(target)["max_package_bytes"] == size - 1
    with pytest.raises(AgentMoveError, match=rf"{size}.*{size - 1}"):
        stage_agent_move(target, archive, expected_sha256=package_sha256(archive),
                         source_instance="HASHI1", target_instance="HASHI2",
                         secret_passphrase="shared-secret")
    assert json.loads((target / "agents.json").read_text())["agents"] == []
    monkeypatch.setenv("HASHI_AGENT_MOVE_MAX_PACKAGE_BYTES", str(size))
    assert service.receiver_capabilities(target)["max_package_bytes"] == size
    result = stage_agent_move(target, archive, expected_sha256=package_sha256(archive),
                              source_instance="HASHI1", target_instance="HASHI2",
                              secret_passphrase="shared-secret")
    assert result["status"] == "staged"


@pytest.mark.parametrize("bad", ["0", "-1", "2.5", "unlimited"])
def test_receiver_rejects_invalid_configured_compressed_budget(tmp_path, monkeypatch, bad):
    _source, target, _archive = _roots(tmp_path)
    monkeypatch.setenv("HASHI_AGENT_MOVE_MAX_PACKAGE_BYTES", bad)
    with pytest.raises(AgentMoveError, match="package byte"):
        service.receiver_capabilities(target)

def test_receiver_expanded_budget_is_advertised_and_staged_transaction_survives_lowering(
    tmp_path, monkeypatch,
):
    from orchestrator.agent_move import package as owner

    source, target, _previous = _roots(tmp_path)
    (source / "workspaces/zelda/memory/big.txt").write_text("synthetic data " * 1024)
    monkeypatch.setattr(owner, "MAX_UNPACKED_BYTES", 2048)
    monkeypatch.setenv("HASHI_AGENT_MOVE_MAX_UNPACKED_BYTES", str(128 * 1024))
    assert service.receiver_capabilities(target)["max_unpacked_bytes"] == 128 * 1024
    archive = tmp_path / "expanded-budget.hashi-agent"
    package = create_agent_move_package(source, "zelda", archive, operation="move",
                                        transfer_mode="workspace", history_mode="retain_source",
                                        max_unpacked_bytes=128 * 1024)
    count = package.unpacked_bytes
    monkeypatch.setenv("HASHI_AGENT_MOVE_MAX_UNPACKED_BYTES", str(count - 1))
    with pytest.raises(AgentMoveError, match="expanded"):
        stage_agent_move(target, archive, expected_sha256=package_sha256(archive),
                         source_instance="HASHI1", target_instance="HASHI2")
    assert json.loads((target / "agents.json").read_text())["agents"] == []
    monkeypatch.setenv("HASHI_AGENT_MOVE_MAX_UNPACKED_BYTES", str(count))
    staged = stage_agent_move(target, archive, expected_sha256=package_sha256(archive),
                              source_instance="HASHI1", target_instance="HASHI2")
    monkeypatch.setenv("HASHI_AGENT_MOVE_MAX_UNPACKED_BYTES", "1")
    assert commit_agent_move(target, staged["package_id"])["status"] == "committed_inactive"

@pytest.mark.parametrize("bad", ["0", "-1", "2.5", "unlimited"])
def test_receiver_rejects_invalid_configured_expanded_budget(tmp_path, monkeypatch, bad):
    _source, target, _archive = _roots(tmp_path)
    monkeypatch.setenv("HASHI_AGENT_MOVE_MAX_UNPACKED_BYTES", bad)
    with pytest.raises(AgentMoveError, match="expanded"):
        service.receiver_capabilities(target)

def test_receiver_workspace_budget_is_advertised_and_staged_transaction_survives_lowering(
    tmp_path, monkeypatch,
):
    from orchestrator.agent_move import package as owner

    source, target, _previous = _roots(tmp_path)
    (source / "workspaces/zelda/memory/big.txt").write_text("synthetic work " * 1024)
    monkeypatch.setattr(owner, "WORKSPACE_LIMIT_BYTES", 1024)
    monkeypatch.setenv("HASHI_AGENT_MOVE_MAX_WORKSPACE_BYTES", str(128 * 1024))
    assert service.receiver_capabilities(target)["max_workspace_bytes"] == 128 * 1024
    archive = tmp_path / "workspace-budget.hashi-agent"
    package = create_agent_move_package(source, "zelda", archive, operation="move",
                                        transfer_mode="workspace", history_mode="retain_source",
                                        max_workspace_bytes=128 * 1024)
    count = package.workspace_bytes
    monkeypatch.setenv("HASHI_AGENT_MOVE_MAX_WORKSPACE_BYTES", str(count - 1))
    with pytest.raises(AgentMoveError, match="workspace.*limit"):
        stage_agent_move(target, archive, expected_sha256=package_sha256(archive),
                         source_instance="HASHI1", target_instance="HASHI2")
    assert json.loads((target / "agents.json").read_text())["agents"] == []
    monkeypatch.setenv("HASHI_AGENT_MOVE_MAX_WORKSPACE_BYTES", str(count))
    staged = stage_agent_move(target, archive, expected_sha256=package_sha256(archive),
                              source_instance="HASHI1", target_instance="HASHI2")
    monkeypatch.setenv("HASHI_AGENT_MOVE_MAX_WORKSPACE_BYTES", "1")
    assert commit_agent_move(target, staged["package_id"])["status"] == "committed_inactive"


@pytest.mark.parametrize("bad", ["0", "-1", "2.5", "unlimited"])
def test_receiver_rejects_invalid_configured_workspace_budget(tmp_path, monkeypatch, bad):
    _source, target, _archive = _roots(tmp_path)
    monkeypatch.setenv("HASHI_AGENT_MOVE_MAX_WORKSPACE_BYTES", bad)
    with pytest.raises(AgentMoveError, match="workspace"):
        service.receiver_capabilities(target)



def test_inactive_bot_backup_survives_transfer_token_rotation(tmp_path):
    from orchestrator.agent_move import service

    source, target, _ = _roots(tmp_path)
    package_path = tmp_path / "backup-rotation.hashi-agent"
    package = create_agent_move_package(
        source, "zelda", package_path, source_instance="HASHI1", operation="clone",
        include_agent_secrets=True, secret_passphrase="shared-secret",
        desired_target_active=False, retain_telegram_backup=True,
    )
    staged = stage_agent_move(
        target, package_path, expected_sha256=package_sha256(package_path),
        source_instance="HASHI1", operation="clone", target_agent_id="zelda-copy",
        secret_passphrase="shared-secret",
    )
    committed = commit_agent_move(target, staged["package_id"], secret_passphrase="shared-secret")
    backup = committed["retained_telegram_backup"]
    assert backup["status"] == "quarantined" and backup["live_connector"] is False
    blob = target / backup["storage_path"]
    assert b"agent-token" not in blob.read_bytes()
    assert "agent-token" not in json.dumps(backup)

    secrets_path = target / "secrets.json"
    secrets = json.loads(secrets_path.read_text())
    secrets["hashi_remote_shared_token"] = "new-synthetic-shared-token"
    _write_json(secrets_path, secrets)
    finalize_agent_move(target, package.package_id, runtime_online=False)
    assert service._recover_quarantined_telegram_token(target, package.package_id) == "agent-token"
    row = next(item for item in json.loads((target / "agents.json").read_text())["agents"]
               if item["name"] == "zelda-copy")
    assert row["is_active"] is False
    assert row["telegram_token_key"] not in json.loads(secrets_path.read_text())

    changed = bytearray(blob.read_bytes())
    changed[-1] ^= 1
    blob.write_bytes(changed)
    with pytest.raises(AgentMoveError, match="backup"):
        service._recover_quarantined_telegram_token(target, package.package_id)
    assert row["telegram_token_key"] not in json.loads(secrets_path.read_text())



def test_inactive_backup_reuses_its_key_and_lifecycle_after_interrupted_commit(tmp_path, monkeypatch):
    from orchestrator.agent_move import service

    source, target, _ = _roots(tmp_path)
    package_path = tmp_path / "backup-interrupted.hashi-agent"
    package = create_agent_move_package(
        source, "zelda", package_path, source_instance="HASHI1", operation="clone",
        include_agent_secrets=True, secret_passphrase="shared-secret",
        desired_target_active=False, retain_telegram_backup=True,
    )
    staged = stage_agent_move(
        target, package_path, expected_sha256=package_sha256(package_path),
        source_instance="HASHI1", operation="clone", target_agent_id="zelda-copy",
        secret_passphrase="shared-secret",
    )
    snapshots = service._write_recovery_snapshots

    def fail_once(*args, **kwargs):
        raise OSError("synthetic snapshot failure")

    monkeypatch.setattr(service, "_write_recovery_snapshots", fail_once)
    with pytest.raises(OSError, match="synthetic snapshot failure"):
        commit_agent_move(target, staged["package_id"], secret_passphrase="shared-secret")
    directory = target / "state" / "agent_moves" / "incoming" / package.package_id
    before = json.loads((directory / "state.json").read_text())
    blob = directory / "retained-telegram-backup" / "local-telegram.enc"
    original_ciphertext = blob.read_bytes()
    assert not (target / "workspaces" / "zelda-copy").exists()

    # A corrupted retained file must never be replaced or silently accepted.
    blob.write_bytes(original_ciphertext[:-1] + b"!")
    monkeypatch.setattr(service, "_write_recovery_snapshots", snapshots)
    with pytest.raises(AgentMoveError, match="backup"):
        commit_agent_move(target, staged["package_id"], secret_passphrase="shared-secret")
    assert blob.read_bytes() == original_ciphertext[:-1] + b"!"
    blob.write_bytes(original_ciphertext)

    result = commit_agent_move(target, staged["package_id"], secret_passphrase="shared-secret")
    assert result["retained_telegram_backup"]["agent_lifecycle_id"] == before["imported_agent_lifecycle_id"]
    assert blob.read_bytes() == original_ciphertext
    assert service._recover_quarantined_telegram_token(target, package.package_id) == "agent-token"


def test_inactive_backup_does_not_overwrite_an_existing_key_name(tmp_path):
    from orchestrator.agent_move import service

    source, target, _ = _roots(tmp_path)
    secrets_path = target / "secrets.json"
    secrets = json.loads(secrets_path.read_text())
    secrets[service._RETAINED_BACKUP_KEY] = "existing-user-value"
    _write_json(secrets_path, secrets)
    package_path = tmp_path / "backup-key-collision.hashi-agent"
    create_agent_move_package(
        source, "zelda", package_path, source_instance="HASHI1", operation="clone",
        include_agent_secrets=True, secret_passphrase="shared-secret",
        desired_target_active=False, retain_telegram_backup=True,
    )
    staged = stage_agent_move(
        target, package_path, expected_sha256=package_sha256(package_path),
        source_instance="HASHI1", operation="clone", target_agent_id="zelda-copy",
        secret_passphrase="shared-secret",
    )
    with pytest.raises(AgentMoveError, match="backup key"):
        commit_agent_move(target, staged["package_id"], secret_passphrase="shared-secret")
    assert json.loads(secrets_path.read_text())[service._RETAINED_BACKUP_KEY] == "existing-user-value"
    assert not (target / "workspaces" / "zelda-copy").exists()



def test_imported_credential_cannot_replace_backup_encryption_key(tmp_path, monkeypatch):
    from orchestrator.agent_move import service

    source, target, _ = _roots(tmp_path)
    path = tmp_path / "backup-import-key-collision.hashi-agent"
    create_agent_move_package(
        source, "zelda", path, source_instance="HASHI1", operation="clone",
        include_agent_secrets=True, secret_passphrase="shared-secret",
        desired_target_active=False, retain_telegram_backup=True,
    )
    staged = stage_agent_move(
        target, path, expected_sha256=package_sha256(path), source_instance="HASHI1",
        operation="clone", target_agent_id="zelda-copy", secret_passphrase="shared-secret",
    )
    secrets_path = target / "secrets.json"
    original_secrets = secrets_path.read_bytes()
    original_remap = service._remap_agent_secrets

    def collision(*args, **kwargs):
        values, mapping = original_remap(*args, **kwargs)
        values[service._RETAINED_BACKUP_KEY] = "synthetic-imported-credential"
        return values, mapping

    monkeypatch.setattr(service, "_remap_agent_secrets", collision)
    with pytest.raises(AgentMoveError, match="collides with local encrypted backup key"):
        commit_agent_move(target, staged["package_id"], secret_passphrase="shared-secret")
    assert secrets_path.read_bytes() == original_secrets
    assert not (target / "workspaces" / "zelda-copy").exists()

    # Even an otherwise replaceable Agent credential must not replace an
    # existing shared backup key used by other retained clones.
    with pytest.raises(AgentMoveError, match="collides with local encrypted backup key"):
        service._merge_secrets_preview(
            {service._RETAINED_BACKUP_KEY: "existing-key"},
            {service._RETAINED_BACKUP_KEY: "replacement"},
            replace_conflicts_for_agent="_hashi_agent_move_backup",
        )


def test_inactive_backup_must_be_recoverable_before_finalization(tmp_path):
    source, target, _ = _roots(tmp_path)
    path = tmp_path / "backup-verify.hashi-agent"
    package = create_agent_move_package(
        source, "zelda", path, source_instance="HASHI1", operation="clone",
        include_agent_secrets=True, secret_passphrase="shared-secret",
        desired_target_active=False, retain_telegram_backup=True,
    )
    staged = stage_agent_move(
        target, path, expected_sha256=package_sha256(path), source_instance="HASHI1",
        operation="clone", target_agent_id="zelda-copy", secret_passphrase="shared-secret",
    )
    committed = commit_agent_move(target, staged["package_id"], secret_passphrase="shared-secret")
    blob = target / committed["retained_telegram_backup"]["storage_path"]
    original = blob.read_bytes()
    blob.write_bytes(original[:-1] + b"!")
    with pytest.raises(AgentMoveError, match="backup"):
        finalize_agent_move(target, package.package_id, runtime_online=False)
    journal = target / "state" / "agent_moves" / "incoming" / package.package_id
    assert (journal / "package.hashi-agent").is_file()
    assert json.loads((journal / "state.json").read_text())["status"] == "committed_inactive"
    blob.write_bytes(original)
    finalized = finalize_agent_move(target, package.package_id, runtime_online=False)
    assert finalized["status"] == "completed"
    assert finalized["target_verification"]["telegram_backup_recoverable"] is True



def test_quarantined_backup_cannot_be_swapped_between_target_agents(tmp_path):
    from hashlib import sha256
    from orchestrator.agent_move import service

    source, target, _ = _roots(tmp_path)
    copies = []
    for suffix in ("a", "b"):
        archive = tmp_path / ("clone-" + suffix + ".hashi-agent")
        package = create_agent_move_package(
            source, "zelda", archive, source_instance="HASHI1", operation="clone",
            include_agent_secrets=True, secret_passphrase="shared-secret",
            desired_target_active=False, retain_telegram_backup=True,
        )
        staged = stage_agent_move(
            target, archive, expected_sha256=package_sha256(archive), source_instance="HASHI1",
            operation="clone", target_agent_id="zelda-" + suffix, secret_passphrase="shared-secret",
        )
        committed = commit_agent_move(target, staged["package_id"], secret_passphrase="shared-secret")
        copies.append((package.package_id, committed["retained_telegram_backup"]))

    first_id, first = copies[0]
    second_id, second = copies[1]
    first_path = target / first["storage_path"]
    second_path = target / second["storage_path"]
    substituted = second_path.read_bytes()
    first_path.write_bytes(substituted)
    state_path = target / "state" / "agent_moves" / "incoming" / first_id / "state.json"
    state = json.loads(state_path.read_text())
    state["retained_telegram_backup"]["sha256"] = sha256(substituted).hexdigest()
    _write_json(state_path, state)
    with pytest.raises(AgentMoveError, match="backup"):
        service._recover_quarantined_telegram_token(target, first_id)
    assert service._recover_quarantined_telegram_token(target, second_id) == "agent-token"
    rows = json.loads((target / "agents.json").read_text())["agents"]
    assert all(not row["is_active"] for row in rows if row["name"].startswith("zelda-"))


def test_governed_cross_instance_clone_maps_authenticated_target_user(tmp_path):
    # Governed cross-instance clone: the target has a verified destination user
    # and the history is attributed to that user, not the source owner or the
    # instance token. Source and target local owner IDs differ and must not be
    # required to match.
    from types import SimpleNamespace

    from orchestrator.frontend_command_context import bind_frontend_command_principal

    source, target, _ = _roots(tmp_path)
    _authorize_owner(source)
    _authorize_owner(target, 8)
    target_config = json.loads((target / "agents.json").read_text())
    target_config["global"].update(deployment_profile="enterprise", organization_id="org-a")
    _write_json(target / "agents.json", target_config)
    source_store = SessionStore(source / "state/sessions.sqlite3", instance_id="HASHI1")
    source_store.ensure_default_session(owner_id="user:7", agent_id="zelda")
    package_path = tmp_path / "governed-mapped.hashi-agent"
    create_agent_move_package(
        source, "zelda", package_path, source_instance="HASHI1", operation="clone",
        transfer_mode="workspace", history_mode="copy", include_telegram_secret=False,
    )
    runtime = SimpleNamespace(global_config=SimpleNamespace(
        project_root=target, instance_id="HASHI2", deployment_profile="enterprise",
    ))
    with bind_frontend_command_principal(runtime, "enterprise:user-8"):
        staged = stage_agent_move(
            target, package_path, expected_sha256=package_sha256(package_path),
            source_instance="HASHI1", operation="clone", target_agent_id="zelda-copy",
        )
        assert staged["status"] == "staged"
        assert staged.get("history_mode") == "copy"
        _, record = service._load_record(target, staged["package_id"])
        binding = record["conversation_owner_binding"]
        assert binding["target_owner_id"] == "enterprise:user-8"
        assert binding["source_owner_id"] == "user:7"
        assert record["conversation_target_owner_origin"] == "authenticated_governed"
        committed = commit_agent_move(target, staged["package_id"])
    assert committed["status"] == "committed_inactive"
    target_store = SessionStore(target / "state/sessions.sqlite3", instance_id="HASHI2")
    sessions = target_store.list_sessions(owner_id="enterprise:user-8", agent_id="zelda-copy")
    assert len(sessions) == 1
    assert target_store.list_sessions(owner_id="user:7", agent_id="zelda-copy") == []


def test_governed_cross_instance_clone_rejects_changed_destination_identity(tmp_path):
    # If the authenticated governed destination user changes between stage and
    # commit, the history must not be silently reassigned to the new identity.
    # The commit is rejected rather than substituting another user's history.
    from types import SimpleNamespace

    from orchestrator.frontend_command_context import bind_frontend_command_principal

    source, target, _ = _roots(tmp_path)
    _authorize_owner(source)
    _authorize_owner(target, 8)
    target_config = json.loads((target / "agents.json").read_text())
    target_config["global"].update(deployment_profile="enterprise", organization_id="org-a")
    _write_json(target / "agents.json", target_config)
    source_store = SessionStore(source / "state/sessions.sqlite3", instance_id="HASHI1")
    source_store.ensure_default_session(owner_id="user:7", agent_id="zelda")
    package_path = tmp_path / "governed-changed.hashi-agent"
    create_agent_move_package(
        source, "zelda", package_path, source_instance="HASHI1", operation="clone",
        transfer_mode="workspace", history_mode="copy", include_telegram_secret=False,
    )

    def _runtime(owner_project_root):
        return SimpleNamespace(global_config=SimpleNamespace(
            project_root=owner_project_root, instance_id="HASHI2",
            deployment_profile="enterprise",
        ))

    with bind_frontend_command_principal(_runtime(target), "enterprise:user-8"):
        staged = stage_agent_move(
            target, package_path, expected_sha256=package_sha256(package_path),
            source_instance="HASHI1", operation="clone", target_agent_id="zelda-copy",
        )
    assert staged["status"] == "staged"

    # Commit with a different authenticated destination user.
    with bind_frontend_command_principal(_runtime(target), "enterprise:user-9"):
        with pytest.raises(AgentMoveError, match="governed destination user changed"):
            commit_agent_move(target, staged["package_id"])
    assert not (target / "workspaces/zelda-copy").exists()
