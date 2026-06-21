from __future__ import annotations

import json

import pytest

from orchestrator.hermes_transfer import (
    HashiImportError,
    HashiImportOptions,
    create_transfer_package,
    import_hashi_agent,
    plan_hashi_import,
    read_transfer_package,
)
from orchestrator.hermes_transfer.schema import new_manifest


def _write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _hashi_root(tmp_path):
    root = tmp_path / "hashi"
    root.mkdir()
    (root / "workspaces").mkdir()
    _write_json(root / "agents.json", {"global": {"instance_id": "HASHI1"}, "agents": []})
    _write_json(root / "tasks.json", {"version": 1, "heartbeats": [], "crons": [], "nudges": []})
    return root


def _normalized_agent(agent_id="xiaoye") -> dict:
    return {
        "agent_id": agent_id,
        "display_name": "Xiaoye",
        "emoji": "",
        "identity_text_path": "identity/hermes_instructions.md",
        "preferred_backend": {"engine": "codex-cli", "model": "gpt-5.5"},
        "capabilities": {"hchat": True, "remote": True},
        "memory": {
            "strategy": "portable_files_first",
            "notes_path": "memory/import_notes.json",
            "max_chars_per_item": 2200,
            "session_state_included": False,
        },
        "secrets": {"included": False, "encrypted": False, "keys": []},
    }


def _hermes_package(tmp_path, *, target_runtime="hashi", agent_id="xiaoye"):
    package = tmp_path / f"{agent_id}.hashi-hermes-agent"
    manifest = new_manifest(
        source_runtime="hermes",
        target_runtime=target_runtime,
        agent_id=agent_id,
        display_name="Xiaoye",
    )
    create_transfer_package(
        package,
        manifest=manifest,
        normalized_agent=_normalized_agent(agent_id),
        files={
            "identity/hermes_instructions.md": "# Xiaoye\n\nHermes profile instructions.\n",
            "source/hermes_profile_config.yaml": "name: xiaoye\n",
            "source/hermes_agents_entry.yaml": "xiaoye:\n  display_name: Xiaoye\n",
            "memory/import_notes.json": '{"accepted": [], "skipped": []}\n',
            "schedules/tasks.json": json.dumps(
                {
                    "version": 1,
                    "heartbeats": [
                        {
                            "id": "xiaoye-watch",
                            "agent": "xiaoye",
                            "enabled": True,
                            "prompt": "check",
                        }
                    ],
                    "crons": [],
                    "nudges": [],
                }
            ),
        },
        migration_report="# Migration Report\n",
        post_migration_self_check="# Self Check\n",
    )
    return package


def _hashi_target_hermes_package(tmp_path):
    package = tmp_path / "hashi-to-hermes.hashi-hermes-agent"
    manifest = new_manifest(
        source_runtime="hashi",
        target_runtime="hermes",
        agent_id="zelda",
        display_name="Zelda",
    )
    create_transfer_package(
        package,
        manifest=manifest,
        normalized_agent=_normalized_agent("zelda"),
        files={"identity/agent.md": "# Zelda\n"},
    )
    return package


def test_plan_hashi_import_builds_dry_run_without_writes(tmp_path):
    root = _hashi_root(tmp_path)
    package = _hermes_package(tmp_path)

    plan = plan_hashi_import(root, package)

    assert plan.target_agent_id == "xiaoye"
    assert not (root / "workspaces" / "xiaoye").exists()
    assert any(item.path == "agents.json" for item in plan.dry_run_report.planned_writes)
    assert plan.agent_config["is_active"] is False
    assert plan.agent_config["import_review_required"] is True


def test_import_hashi_agent_creates_disabled_agent_workspace_and_rollback(tmp_path):
    root = _hashi_root(tmp_path)
    package = _hermes_package(tmp_path)

    plan = import_hashi_agent(root, package)

    agent_md = root / "workspaces" / "xiaoye" / "agent.md"
    assert agent_md.read_text(encoding="utf-8").startswith("# Xiaoye")
    imported = root / "workspaces" / "xiaoye" / "hermes_import"
    assert (imported / "source" / "hermes_profile_config.yaml").exists()
    assert (imported / "normalized_agent.json").exists()
    assert (imported / "audit" / "migration_report.md").exists()
    assert (imported / "audit" / "post_migration_self_check.md").exists()
    assert (plan.rollback_dir / "agents.json").exists()
    assert (plan.rollback_dir / "tasks.json").exists()

    agents = json.loads((root / "agents.json").read_text(encoding="utf-8"))
    [agent] = agents["agents"]
    assert agent["name"] == "xiaoye"
    assert agent["is_active"] is False
    assert agent["system_md"] == "workspaces/xiaoye/agent.md"


def test_import_hashi_agent_imports_schedule_drafts_disabled(tmp_path):
    root = _hashi_root(tmp_path)
    package = _hermes_package(tmp_path)

    import_hashi_agent(root, package)

    tasks = json.loads((root / "tasks.json").read_text(encoding="utf-8"))
    [heartbeat] = tasks["heartbeats"]
    assert heartbeat["agent"] == "xiaoye"
    assert heartbeat["enabled"] is False
    assert heartbeat["import_state"] == "disabled_review_draft"


def test_plan_hashi_import_rejects_existing_agent_without_overwrite(tmp_path):
    root = _hashi_root(tmp_path)
    _write_json(
        root / "agents.json",
        {"agents": [{"name": "xiaoye", "workspace_dir": "workspaces/xiaoye"}]},
    )
    package = _hermes_package(tmp_path)

    with pytest.raises(HashiImportError, match="already exists"):
        plan_hashi_import(root, package)


def test_import_hashi_agent_can_enable_only_when_requested(tmp_path):
    root = _hashi_root(tmp_path)
    package = _hermes_package(tmp_path)

    import_hashi_agent(root, package, options=HashiImportOptions(enable=True))

    agents = json.loads((root / "agents.json").read_text(encoding="utf-8"))
    [agent] = agents["agents"]
    assert agent["is_active"] is True
    assert agent["import_review_required"] is False


def test_plan_hashi_import_rejects_non_hashi_target_package(tmp_path):
    root = _hashi_root(tmp_path)
    package = _hashi_target_hermes_package(tmp_path)

    with pytest.raises(HashiImportError, match="target_runtime"):
        plan_hashi_import(root, package)


def test_imported_package_remains_readable_after_import(tmp_path):
    root = _hashi_root(tmp_path)
    package = _hermes_package(tmp_path)

    import_hashi_agent(root, package)

    assert read_transfer_package(package).manifest["target_runtime"] == "hashi"
