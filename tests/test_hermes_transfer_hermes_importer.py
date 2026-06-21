from __future__ import annotations

import json

import pytest
import yaml

from orchestrator.hermes_transfer import (
    HermesImportError,
    HermesImportOptions,
    create_transfer_package,
    import_hermes_agent,
    plan_hermes_import,
)
from orchestrator.hermes_transfer.schema import new_manifest


def _normalized_agent(agent_id="zelda") -> dict:
    return {
        "agent_id": agent_id,
        "display_name": "Zelda",
        "emoji": "Z",
        "description": "Imported HASHI agent.",
        "identity_text_path": "identity/agent.md",
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


def _hashi_to_hermes_package(tmp_path, *, target_runtime="hermes", source_runtime="hashi", agent_id="zelda"):
    package = tmp_path / f"{agent_id}.hashi-hermes-agent"
    create_transfer_package(
        package,
        manifest=new_manifest(
            source_runtime=source_runtime,
            target_runtime=target_runtime,
            agent_id=agent_id,
            display_name="Zelda",
        ),
        normalized_agent=_normalized_agent(agent_id),
        files={
            "identity/agent.md": "# Zelda\n\nHASHI instructions.\n",
            "source/hashi_agent_config.json": '{"name": "zelda"}\n',
            "memory/import_notes.json": '{"accepted": ["MEMORY.md"], "skipped": []}\n',
            "memory/files/MEMORY.md": "portable memory\n",
            "schedules/tasks.json": json.dumps(
                {
                    "version": 1,
                    "heartbeats": [{"id": "zelda-loop", "agent": "zelda", "enabled": True}],
                    "crons": [{"id": "zelda-cron", "agent": "zelda", "enabled": True}],
                    "nudges": [],
                }
            ),
            "skills/hchat/SKILL.md": "# HChat\n",
        },
        migration_report="# Migration Report\n",
        post_migration_self_check="# Self Check\n",
    )
    return package


def _hermes_target(tmp_path):
    bridge = tmp_path / "hermes_bridge"
    profile = tmp_path / "hermes" / "profiles" / "zelda"
    bridge.mkdir(parents=True)
    (bridge / "agents.yaml").write_text(
        yaml.safe_dump({"instance": {"id": "HERMES"}, "agents": {}}, sort_keys=True),
        encoding="utf-8",
    )
    return profile, bridge


def test_plan_hermes_import_builds_dry_run_without_writes(tmp_path):
    profile, bridge = _hermes_target(tmp_path)
    package = _hashi_to_hermes_package(tmp_path)

    plan = plan_hermes_import(profile, bridge, package)

    assert plan.target_profile_name == "zelda"
    assert not profile.exists()
    assert plan.bridge_agent_entry["enabled"] is False
    assert plan.profile_config["hashi_transfer"]["review_required"] is True
    assert plan.profile_config["platforms"]["hashi_hchat"]["enabled"] is False
    assert any(item.path.endswith("AGENT.md") for item in plan.dry_run_report.planned_writes)


def test_import_hermes_agent_creates_disabled_profile_and_bridge_entry(tmp_path):
    profile, bridge = _hermes_target(tmp_path)
    package = _hashi_to_hermes_package(tmp_path)

    plan = import_hermes_agent(profile, bridge, package)

    assert (profile / "AGENT.md").read_text(encoding="utf-8").startswith("# Zelda")
    config = yaml.safe_load((profile / "config.yaml").read_text(encoding="utf-8"))
    assert config["hashi_transfer"]["disabled_until_review"] is True
    assert config["platforms"]["hashi_hchat"]["review_required"] is True
    agents = yaml.safe_load((bridge / "agents.yaml").read_text(encoding="utf-8"))
    assert agents["agents"]["zelda"]["enabled"] is False
    assert agents["agents"]["zelda"]["review_required"] is True
    assert (plan.rollback_dir / "agents.yaml").exists()
    assert (profile / "hashi_import" / "manifest.json").exists()
    assert (profile / "hashi_import" / "audit" / "migration_report.md").exists()


def test_import_hermes_agent_writes_memory_skills_and_paused_schedules(tmp_path):
    profile, bridge = _hermes_target(tmp_path)
    package = _hashi_to_hermes_package(tmp_path)

    import_hermes_agent(profile, bridge, package)

    assert (profile / "imported_memory").exists()
    assert list((profile / "imported_memory").glob("*/files/MEMORY.md"))
    assert (profile / "skills" / "hchat" / "SKILL.md").read_text(encoding="utf-8") == "# HChat\n"
    [schedule_file] = list((profile / "imported_schedules").glob("*/tasks.paused.json"))
    schedules = json.loads(schedule_file.read_text(encoding="utf-8"))
    assert schedules["heartbeats"][0]["enabled"] is False
    assert schedules["heartbeats"][0]["import_state"] == "paused_review_draft"
    assert schedules["crons"][0]["enabled"] is False


def test_plan_hermes_import_rejects_existing_profile_without_overwrite(tmp_path):
    profile, bridge = _hermes_target(tmp_path)
    profile.mkdir(parents=True)
    (profile / "AGENT.md").write_text("# Existing\n", encoding="utf-8")
    package = _hashi_to_hermes_package(tmp_path)

    with pytest.raises(HermesImportError, match="already exists"):
        plan_hermes_import(profile, bridge, package)


def test_import_hermes_agent_can_overwrite_existing_profile_and_preserve_rollback(tmp_path):
    profile, bridge = _hermes_target(tmp_path)
    profile.mkdir(parents=True)
    (profile / "AGENT.md").write_text("# Existing\n", encoding="utf-8")
    package = _hashi_to_hermes_package(tmp_path)

    plan = import_hermes_agent(profile, bridge, package, options=HermesImportOptions(overwrite=True))

    assert (profile / "AGENT.md").read_text(encoding="utf-8").startswith("# Zelda")
    assert (plan.rollback_dir / "profile" / "AGENT.md").read_text(encoding="utf-8").startswith("# Existing")


def test_plan_hermes_import_rejects_wrong_runtime_direction(tmp_path):
    profile, bridge = _hermes_target(tmp_path)
    package = _hashi_to_hermes_package(tmp_path, target_runtime="hashi", source_runtime="hermes")

    with pytest.raises(HermesImportError, match="target_runtime"):
        plan_hermes_import(profile, bridge, package)
