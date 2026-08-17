from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from orchestrator.automation_runner import run_automation
from orchestrator.skill_manager import SkillManager


def _write_skill(
    root: Path, directory: str, frontmatter: str, body: str = "# Instructions"
) -> None:
    skill_dir = root / "skills" / directory
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\n{frontmatter}\n---\n\n{body}\n",
        encoding="utf-8",
    )


def test_repository_catalog_is_standard_and_keeps_high_autonomy_templates():
    project_root = Path(__file__).resolve().parents[1]
    manager = SkillManager(project_root, project_root / "tasks.json")

    ids = {skill.id for skill in manager.list_skills()}

    assert {
        "agent-audit",
        "claude",
        "codex",
        "debug",
        "gemini",
        "hermes-memory-import",
        "library-pick",
        "memory-consolidation",
        "msn",
        "ngr",
    } <= ids
    assert ids.isdisjoint({"cron", "heartbeat", "dream", "recall"})
    assert manager.skill_validation_errors() == []
    assert "--dangerously-skip-permissions" in (
        project_root / "skills" / "claude" / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert "--full-auto" in (project_root / "skills" / "codex" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "--approval-mode yolo" in (
        project_root / "skills" / "gemini" / "SKILL.md"
    ).read_text(encoding="utf-8")


def test_loads_only_standard_skill_packages_and_resolves_legacy_underscore_alias(
    tmp_path: Path,
):
    _write_skill(
        tmp_path,
        "memory-consolidation",
        "name: memory-consolidation\ndescription: Use when consolidating memory.",
        "# Memory Consolidation\n\nFollow the workflow.",
    )
    legacy_dir = tmp_path / "skills" / "legacy"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "skill.md").write_text("legacy content", encoding="utf-8")
    manager = SkillManager(tmp_path, tmp_path / "tasks.json")

    skills = manager.list_skills()

    assert [skill.id for skill in skills] == ["memory-consolidation"]
    assert manager.get_skill("memory_consolidation") == skills[0]
    assert manager.skill_validation_errors() == []


def test_accepts_agent_skills_optional_frontmatter(tmp_path: Path):
    _write_skill(
        tmp_path,
        "portable-skill",
        "\n".join(
            [
                "name: portable-skill",
                "description: Use when testing a portable Agent Skills package.",
                "license: Apache-2.0",
                "compatibility: Requires git and network access.",
                "metadata:",
                "  author: example-org",
                '  version: "1.0"',
                "allowed-tools: Bash(git:*) Read",
            ]
        ),
    )
    manager = SkillManager(tmp_path, tmp_path / "tasks.json")

    skills = manager.list_skills()

    assert [skill.id for skill in skills] == ["portable-skill"]
    assert skills[0].license == "Apache-2.0"
    assert skills[0].compatibility == "Requires git and network access."
    assert skills[0].metadata == {"author": "example-org", "version": "1.0"}
    assert skills[0].allowed_tools == "Bash(git:*) Read"
    assert skills[0].source_type == "project"
    assert skills[0].scope == "project"
    assert skills[0].managed is False
    assert manager.skill_validation_errors() == []


def test_standard_skill_enable_state_is_per_workspace(tmp_path: Path):
    _write_skill(
        tmp_path,
        "portable-skill",
        "name: portable-skill\ndescription: Use when testing package state.",
    )
    manager = SkillManager(tmp_path, tmp_path / "tasks.json")
    first_workspace = tmp_path / "workspaces" / "first"
    second_workspace = tmp_path / "workspaces" / "second"

    assert manager.is_skill_enabled(first_workspace, "portable-skill") is True
    ok, message = manager.set_skill_enabled(
        first_workspace,
        "portable-skill",
        enabled=False,
    )

    assert ok is True
    assert "disabled" in message.lower()
    assert manager.is_skill_enabled(first_workspace, "portable-skill") is False
    assert manager.is_skill_enabled(second_workspace, "portable-skill") is True
    assert manager.get_skill("portable-skill") is not None

    manager.set_skill_enabled(first_workspace, "portable-skill", enabled=True)
    assert manager.is_skill_enabled(first_workspace, "portable-skill") is True


def test_installed_skill_is_registered_and_uninstalled_to_recovery_area(tmp_path: Path):
    project_root = tmp_path / "project"
    source_root = tmp_path / "incoming"
    _write_skill(
        source_root,
        "portable-skill",
        (
            "name: portable-skill\n"
            "description: Use when testing managed installation.\n"
            "metadata:\n"
            '  version: "2.0"'
        ),
    )
    manager = SkillManager(project_root, project_root / "tasks.json")

    ok, message, installed = manager.install_skill(
        source_root / "skills" / "portable-skill",
        actor="tester",
    )

    assert ok is True, message
    assert installed is not None
    assert installed.source_type == "installed"
    assert installed.scope == "project"
    assert installed.managed is True
    assert installed.version == "2.0"
    assert manager.can_uninstall_skill(installed) is True
    registry = json.loads(manager.skill_registry_path.read_text(encoding="utf-8"))
    assert registry["skills"]["portable-skill"]["source_type"] == "installed"
    assert registry["skills"]["portable-skill"]["content_sha256"]

    removed, remove_message, recovery_path = manager.uninstall_skill("portable-skill")

    assert removed is True, remove_message
    assert recovery_path is not None and recovery_path.is_dir()
    assert manager.get_skill("portable-skill") is None
    assert (source_root / "skills" / "portable-skill" / "SKILL.md").is_file()


def test_linked_skill_unlinks_without_touching_source(tmp_path: Path):
    project_root = tmp_path / "project"
    source_root = tmp_path / "incoming"
    _write_skill(
        source_root,
        "linked-skill",
        "name: linked-skill\ndescription: Use when testing a linked package.",
    )
    source = source_root / "skills" / "linked-skill"
    manager = SkillManager(project_root, project_root / "tasks.json")

    ok, message, linked = manager.install_skill(source, link=True, actor="tester")

    assert ok is True, message
    assert linked is not None and linked.source_type == "linked"
    assert linked.skill_dir.is_symlink()

    removed, remove_message, recovery_path = manager.uninstall_skill("linked-skill")

    assert removed is True, remove_message
    assert recovery_path is None
    assert source.joinpath("SKILL.md").is_file()
    assert not (project_root / "skills" / "linked-skill").exists()


def test_delete_moves_project_skills_to_recovery_and_blocks_job_dependencies(
    tmp_path: Path,
):
    project_root = tmp_path / "project"
    _write_skill(
        project_root,
        "project-skill",
        "name: project-skill\ndescription: Use when testing a built-in package.",
    )
    source_root = tmp_path / "incoming"
    _write_skill(
        source_root,
        "managed-skill",
        "name: managed-skill\ndescription: Use when testing dependency protection.",
    )
    tasks_path = project_root / "tasks.json"
    tasks_path.write_text(
        json.dumps(
            {
                "version": 1,
                "heartbeats": [],
                "nudges": [],
                "crons": [
                    {
                        "id": "managed-nightly",
                        "agent": "momo",
                        "enabled": True,
                        "action": "skill:managed_skill",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    manager = SkillManager(project_root, tasks_path)
    ok, message, _ = manager.install_skill(
        source_root / "skills" / "managed-skill",
        actor="tester",
    )
    assert ok is True, message

    deleted, deleted_message, recovery_path = manager.uninstall_skill("project-skill")
    blocked, blocked_message, _ = manager.uninstall_skill("managed-skill")

    assert deleted is True, deleted_message
    assert recovery_path is not None and recovery_path.is_dir()
    assert "deleted" in deleted_message.lower()
    assert manager.get_skill("project-skill") is None
    assert blocked is False
    assert "managed-nightly" in blocked_message
    dependencies = manager.skill_dependencies("managed-skill")
    assert dependencies == [
        {
            "kind": "cron",
            "id": "managed-nightly",
            "agent": "momo",
            "enabled": True,
            "action": "skill:managed_skill",
        }
    ]
    assert manager.get_skill("managed-skill") is not None


def test_skill_usage_stats_merge_new_ledger_and_historical_token_audit(
    tmp_path: Path,
):
    _write_skill(
        tmp_path,
        "measured-skill",
        "name: measured-skill\ndescription: Use when testing usage metrics.",
    )
    manager = SkillManager(tmp_path, tmp_path / "tasks.json")

    first_event = manager.record_skill_usage(
        "measured-skill",
        agent="momo",
        request_id="req-new-1",
        source="skill:measured-skill",
    )
    second_event = manager.record_skill_usage(
        "measured_skill",
        agent="zelda",
        request_id="automation-job-1",
        source="scheduler-automation",
        task_id="job-1",
    )
    audit_path = tmp_path / "workspaces" / "momo" / "token_audit.jsonl"
    audit_path.parent.mkdir(parents=True)
    audit_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "ts": "2026-01-01T00:00:00+00:00",
                        "agent": "momo",
                        "source": "skill:measured-skill",
                        "success": True,
                    }
                ),
                json.dumps(
                    {
                        "ts": "2026-01-02T00:00:00+00:00",
                        "agent": "momo",
                        "source": "skill:measured-skill",
                        "skill_id": "measured-skill",
                        "skill_usage_event_id": first_event,
                        "success": True,
                    }
                ),
                json.dumps(
                    {
                        "ts": "2026-01-03T00:00:00+00:00",
                        "agent": "lulu",
                        "source": "scheduler-skill",
                        "skill_id": "measured-skill",
                        "success": True,
                    }
                ),
                "{invalid-json",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    stats = manager.skill_usage_stats("measured-skill")
    usage_records = [
        json.loads(line)
        for line in manager.skill_usage_path.read_text(encoding="utf-8").splitlines()
    ]

    assert first_event and second_event
    assert all(
        "prompt" not in record and "skill_body" not in record
        for record in usage_records
    )
    assert stats["total"] == 4
    assert stats["tracked"] == 2
    assert stats["historical"] == 2
    assert stats["agents"] == 3
    assert stats["by_agent"] == {"lulu": 1, "momo": 2, "zelda": 1}
    assert stats["last_used_at"] is not None


def test_dependency_scan_includes_managed_active_heartbeats(tmp_path: Path):
    _write_skill(
        tmp_path,
        "heartbeat-skill",
        "name: heartbeat-skill\ndescription: Use when testing active heartbeat references.",
    )
    (tmp_path / "managed_active_heartbeats.json").write_text(
        json.dumps(
            {
                "heartbeats": [
                    {
                        "id": "momo-active-heartbeat",
                        "agent": "momo",
                        "enabled": True,
                        "action": "skill:heartbeat_skill",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    manager = SkillManager(tmp_path, tmp_path / "tasks.json")

    assert manager.skill_dependencies("heartbeat-skill", enabled_only=True)[0][
        "id"
    ] == ("momo-active-heartbeat")


def test_skill_callback_keys_keep_max_length_names_within_telegram_limit(
    tmp_path: Path,
):
    skill_id = "a" * 64
    _write_skill(
        tmp_path,
        skill_id,
        f"name: {skill_id}\ndescription: Use when testing callback identifiers.",
    )
    manager = SkillManager(tmp_path, tmp_path / "tasks.json")
    skill = manager.get_skill(skill_id)
    assert skill is not None

    callback_key = manager.skill_callback_key(skill.id)

    assert len(f"skill:xc:{callback_key}".encode("utf-8")) <= 64
    assert manager.get_skill_by_callback_key(callback_key) == skill


@pytest.mark.parametrize(
    ("directory", "frontmatter", "expected"),
    [
        (
            "bad-name",
            "name: different-name\ndescription: Use when testing.",
            "directory `bad-name` must match skill name `different-name`",
        ),
        (
            "legacy-action",
            "name: legacy-action\ndescription: Use when testing.\ntype: action",
            "unsupported frontmatter keys: type",
        ),
        (
            "Bad_Name",
            "name: Bad_Name\ndescription: Use when testing.",
            "name` must be lowercase kebab-case",
        ),
    ],
)
def test_reports_invalid_standard_packages(
    tmp_path: Path,
    directory: str,
    frontmatter: str,
    expected: str,
):
    _write_skill(tmp_path, directory, frontmatter)
    manager = SkillManager(tmp_path, tmp_path / "tasks.json")

    assert manager.list_skills() == []
    assert expected in manager.skill_validation_errors()[0]


def test_runtime_toggles_are_not_skill_types(tmp_path: Path):
    _write_skill(
        tmp_path,
        "debug",
        "name: debug\ndescription: Use when debugging a scoped failure.",
    )
    manager = SkillManager(tmp_path, tmp_path / "tasks.json")
    workspace = tmp_path / "workspace"

    assert manager.set_toggle_state(workspace, "debug", enabled=True)[0] is True
    assert manager.set_toggle_state(workspace, "recall", enabled=True)[0] is True
    assert (
        manager.set_toggle_state(workspace, "not-a-setting", enabled=True)[0] is False
    )
    assert manager.get_active_toggle_ids(workspace) == {"debug", "recall"}
    assert [skill.id for skill in manager.get_active_toggle_skills(workspace)] == [
        "debug"
    ]


def test_skill_prompt_includes_package_root_for_relative_resources(tmp_path: Path):
    _write_skill(
        tmp_path,
        "resource-reader",
        "name: resource-reader\ndescription: Use when reading a bundled reference.",
        "Read references/guide.md before answering.",
    )
    manager = SkillManager(tmp_path, tmp_path / "tasks.json")
    skill = manager.get_skill("resource-reader")
    assert skill is not None

    prompt = manager.build_prompt_for_skill(skill, "Apply the guide.")

    assert f"Skill package root: {skill.skill_dir}" in prompt
    assert "Resolve relative resource paths against that package root." in prompt
    assert "Read references/guide.md before answering." in prompt
    assert prompt.endswith("--- USER REQUEST ---\nApply the guide.")


@pytest.mark.asyncio
async def test_jobs_automation_runner_accepts_legacy_underscore_id(tmp_path: Path):
    script = (
        tmp_path
        / "skills"
        / "memory-consolidation"
        / "scripts"
        / "memory_consolidation.py"
    )
    script.parent.mkdir(parents=True)
    script.write_text(
        "import os, sys\n"
        "print(os.environ['BRIDGE_AUTOMATION_ID'])\n"
        "print(os.environ['BRIDGE_SKILL_ID'])\n"
        "print(sys.argv[1])\n",
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    ok, output = await run_automation(
        project_root=tmp_path,
        workspace_dir=workspace,
        automation_id="memory_consolidation",
        args="run",
    )

    assert ok is True
    assert output.splitlines() == [
        "memory-consolidation",
        "memory_consolidation",
        "run",
    ]


@pytest.mark.asyncio
async def test_jobs_remote_guard_accepts_machine_local_legacy_path(tmp_path: Path):
    script = tmp_path / "skills" / "remote_guard" / "remote_guard.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('legacy remote guard')\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    ok, output = await run_automation(
        project_root=tmp_path,
        workspace_dir=workspace,
        automation_id="remote-guard",
    )

    assert ok is True
    assert output == "legacy remote guard"


@pytest.mark.asyncio
async def test_jobs_automation_runner_reports_nonzero_exit_without_running_arbitrary_paths(
    tmp_path: Path,
):
    script = tmp_path / "skills" / "agent-audit" / "scripts" / "agent_audit.py"
    script.parent.mkdir(parents=True)
    script.write_text(
        "import sys\nprint('partial output')\nprint('failure detail', file=sys.stderr)\nraise SystemExit(7)\n",
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    unknown_ok, unknown_output = await run_automation(
        project_root=tmp_path,
        workspace_dir=workspace,
        automation_id="../../arbitrary",
    )
    failed_ok, failed_output = await run_automation(
        project_root=tmp_path,
        workspace_dir=workspace,
        automation_id="agent-audit",
    )

    assert unknown_ok is False
    assert unknown_output == "Unknown automation: ../../arbitrary"
    assert failed_ok is False
    assert "partial output" in failed_output
    assert "stderr:\nfailure detail" in failed_output
    assert "exit_code=7" in failed_output


@pytest.mark.asyncio
async def test_jobs_automation_runner_is_single_flight_and_times_out_cleanly(
    tmp_path: Path,
):
    script = tmp_path / "skills" / "agent-audit" / "scripts" / "agent_audit.py"
    script.parent.mkdir(parents=True)
    script.write_text("import time\ntime.sleep(0.3)\nprint('done')\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    first = asyncio.create_task(
        run_automation(
            project_root=tmp_path,
            workspace_dir=workspace,
            automation_id="agent-audit",
        )
    )
    await asyncio.sleep(0.05)
    duplicate = await run_automation(
        project_root=tmp_path,
        workspace_dir=workspace,
        automation_id="agent_audit",
    )
    assert duplicate == (False, "Automation 'agent-audit' is already running.")
    assert await first == (True, "done")

    timed_out = await run_automation(
        project_root=tmp_path,
        workspace_dir=workspace,
        automation_id="agent-audit",
        timeout_s=0.01,
    )
    assert timed_out == (False, "Automation 'agent-audit' timed out after 0.01s.")
