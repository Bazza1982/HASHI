from __future__ import annotations

import json
import zipfile

import pytest
import yaml

from orchestrator.hermes_transfer import (
    HermesExportError,
    HermesExportOptions,
    export_hermes_agent,
    plan_hermes_export,
    read_transfer_package,
)


def _fixture_hermes(tmp_path):
    profile = tmp_path / "hermes" / "profiles" / "xiaoye"
    bridge = tmp_path / "hermes_bridge"
    (profile / "skills" / "hchat").mkdir(parents=True)
    (profile / "memories").mkdir()
    (profile / "cron").mkdir()
    (profile / "sessions").mkdir()
    (profile / "plugins" / "cache").mkdir(parents=True)
    bridge.mkdir(parents=True)

    (profile / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "display_name": "Xiaoye Profile",
                "model": "gpt-5.5",
                "send_message": {"telegram_token": "do-not-copy"},
            },
            sort_keys=True,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    (profile / "AGENT.md").write_text("# Xiaoye\n\nHermes profile instructions.\n", encoding="utf-8")
    (profile / "skills" / "hchat" / "SKILL.md").write_text("# HChat\n", encoding="utf-8")
    (profile / "memories" / "small.md").write_text("portable memory\n", encoding="utf-8")
    (profile / "memories" / "large.md").write_text("x" * 2201, encoding="utf-8")
    (profile / "cron" / "daily.yaml").write_text("schedule: '0 8 * * *'\nprompt: daily\n", encoding="utf-8")
    (profile / "sessions" / "sessions.db").write_bytes(b"sqlite")
    (profile / "plugins" / "cache" / "blob").write_bytes(b"cache")
    (profile / "message_queue.json").write_text("[]\n", encoding="utf-8")

    (bridge / "agents.yaml").write_text(
        yaml.safe_dump(
            {
                "instance": {"id": "HERMES"},
                "agents": {
                    "xiaoye": {
                        "display_name": "Xiaoye",
                        "emoji": "",
                        "description": "Hermes profile exposed to HASHI.",
                    }
                },
            },
            sort_keys=True,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return profile, bridge


def test_plan_hermes_export_builds_dry_run_without_package(tmp_path):
    profile, bridge = _fixture_hermes(tmp_path)
    package = tmp_path / "xiaoye.hashi-hermes-agent"

    plan = plan_hermes_export(profile, bridge, "xiaoye", package)

    assert not package.exists()
    assert plan.manifest["source_runtime"] == "hermes"
    assert plan.manifest["target_runtime"] == "hashi"
    assert plan.normalized_agent["identity_text_path"] == "identity/hermes_instructions.md"
    assert plan.files["identity/hermes_instructions.md"].startswith("# Xiaoye")
    assert "source/hermes_profile_config.yaml" in plan.files
    assert "source/hermes_agents_entry.yaml" in plan.files
    assert "memory/files/small.md" in plan.files
    assert "memory/files/large.md" not in plan.files
    assert "schedules/tasks.json" in plan.files
    assert "source/hermes_cron/daily.yaml" in plan.files
    assert "skills/hchat/SKILL.md" in plan.files
    assert "sessions/sessions.db" not in plan.files
    assert "plugins/cache/blob" not in plan.files
    warning_text = "\n".join(plan.warnings)
    assert "skipped Hermes memory" in warning_text
    assert "blocked Hermes runtime state excluded" in warning_text
    assert "credentials and delivery configuration excluded" in warning_text
    assert any(item.path == "identity/hermes_instructions.md" for item in plan.dry_run_report.planned_writes)


def test_export_hermes_agent_writes_readable_package_with_paused_cron_drafts(tmp_path):
    profile, bridge = _fixture_hermes(tmp_path)
    package = tmp_path / "xiaoye.hashi-hermes-agent"

    export_hermes_agent(profile, bridge, "xiaoye", package)
    transfer = read_transfer_package(package)

    assert transfer.manifest["agent_id"] == "xiaoye"
    assert transfer.manifest["target_runtime"] == "hashi"
    assert "identity/hermes_instructions.md" in transfer.names
    assert "source/hermes_cron/daily.yaml" in transfer.names
    assert "skills/hchat/SKILL.md" in transfer.names

    with zipfile.ZipFile(package, "r") as archive:
        schedules = json.loads(archive.read("schedules/tasks.json"))
        secrets_policy = json.loads(archive.read("secrets.policy.json"))
        secrets_summary = json.loads(archive.read("source/secrets_summary.json"))
        memory_notes = json.loads(archive.read("memory/import_notes.json"))

    [cron] = schedules["crons"]
    assert cron["enabled"] is False
    assert cron["import_state"] == "paused_review_draft"
    assert secrets_policy["included"] is False
    assert secrets_summary["included"] is False
    assert memory_notes["accepted"] == ["small.md"]
    assert memory_notes["skipped"] == ["large.md"]


def test_plan_hermes_export_can_disable_optional_sections(tmp_path):
    profile, bridge = _fixture_hermes(tmp_path)

    plan = plan_hermes_export(
        profile,
        bridge,
        "xiaoye",
        options=HermesExportOptions(include_skills=False, include_memories=False, include_cron=False),
    )

    assert not any(name.startswith("skills/") for name in plan.files)
    assert not any(name.startswith("memory/files/") for name in plan.files)
    assert not any(name.startswith("source/hermes_cron/") for name in plan.files)
    assert json.loads(plan.files["schedules/tasks.json"])["crons"] == []


def test_plan_hermes_export_requires_existing_bridge_agent(tmp_path):
    profile, bridge = _fixture_hermes(tmp_path)

    with pytest.raises(HermesExportError, match="bridge agent not found"):
        plan_hermes_export(profile, bridge, "missing")


def test_plan_hermes_export_requires_existing_paths(tmp_path):
    profile, bridge = _fixture_hermes(tmp_path)

    with pytest.raises(HermesExportError, match="profile directory not found"):
        plan_hermes_export(profile / "missing", bridge, "xiaoye")
    with pytest.raises(HermesExportError, match="bridge home not found"):
        plan_hermes_export(profile, bridge / "missing", "xiaoye")
