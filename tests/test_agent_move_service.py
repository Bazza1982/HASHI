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


def test_move_commit_adopts_owned_delivery_state_and_rollback_retires_it(tmp_path):
    source, target, _ = _roots(tmp_path)
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


def test_schema3_move_finalization_then_source_cleanup_is_complete(tmp_path):
    source, target, package_path = _roots(tmp_path)
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

    # A real credential consumer must block cleanup without changing any files.
    config["agents"][1]["telegram_token_key"] = "zelda"
    _write_json(source / "agents.json", config)
    before = (source / "agents.json").read_bytes()
    with pytest.raises(AgentMoveError, match="remaining Agents reference"):
        cleanup_source_agent(source, staged["package_id"], source_secret_keys=["zelda"],
                             target_instance="HASHI2", target_agent_id="zelda")
    assert (source / "agents.json").read_bytes() == before
    assert (source / "workspaces" / "zelda").exists()
    del config["agents"][1]["telegram_token_key"]
    _write_json(source / "agents.json", config)

    cleaned = cleanup_source_agent(
        source,
        staged["package_id"],
        source_secret_keys=["zelda"],
        target_instance="HASHI2",
        target_agent_id="zelda",
    )
    assert cleaned["status"] == "source_cleaned"
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


def test_schema3_clone_is_active_without_telegram_and_schedules_stay_disabled(
    tmp_path,
):
    source, target, _ = _roots(tmp_path)
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
    assert target_agent["telegram_token_key"] == "zelda_copy"
    target_secrets = json.loads((target / "secrets.json").read_text())
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


@pytest.mark.parametrize("tamper", ["config", "secret", "schedule"])
def test_finalization_rejects_target_state_changed_after_commit(tmp_path, tamper):
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

    if tamper == "config":
        agents = json.loads((target / "agents.json").read_text())
        agents["agents"][0]["access_scope"] = "workspace"
        _write_json(target / "agents.json", agents)
    elif tamper == "secret":
        secrets = json.loads((target / "secrets.json").read_text())
        secrets["zelda_copy_api_key"] = "changed"
        _write_json(target / "secrets.json", secrets)
    else:
        tasks = json.loads((target / "tasks.json").read_text())
        imported = next(
            item
            for item in tasks["heartbeats"]
            if item.get("agent") == "zelda_copy"
        )
        imported["schedule"] = "changed"
        _write_json(target / "tasks.json", tasks)

    with pytest.raises(AgentMoveError, match="differ"):
        finalize_agent_move(
            target,
            staged["package_id"],
            runtime_online=True,
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
    actual_write = write_config_json
    injected = False

    def interleaved(path, payload, **kwargs):
        nonlocal injected
        if Path(path).resolve() == agents_path and not injected:
            injected = True
            winner = read_config_json(path)
            winner["global"]["concurrent_extension"] = "kept"
            actual_write(path, winner)
        return actual_write(path, payload, **kwargs)

    monkeypatch.setattr(service, "write_config_json", interleaved)

    with pytest.raises(ConfigConflictError):
        deactivate_source_agent(
            source,
            "zelda",
            "stale-source-package",
            target_instance="HASHI2",
        )

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
