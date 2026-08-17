from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from orchestrator.automation_runner import run_automation
from orchestrator.skill_manager import SkillManager


def _write_skill(root: Path, directory: str, frontmatter: str, body: str = "# Instructions") -> None:
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
    assert "--full-auto" in (
        project_root / "skills" / "codex" / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert "--approval-mode yolo" in (
        project_root / "skills" / "gemini" / "SKILL.md"
    ).read_text(encoding="utf-8")


def test_loads_only_standard_skill_packages_and_resolves_legacy_underscore_alias(tmp_path: Path):
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

    assert [skill.id for skill in manager.list_skills()] == ["portable-skill"]
    assert manager.skill_validation_errors() == []


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
    assert manager.set_toggle_state(workspace, "not-a-setting", enabled=True)[0] is False
    assert manager.get_active_toggle_ids(workspace) == {"debug", "recall"}
    assert [skill.id for skill in manager.get_active_toggle_skills(workspace)] == ["debug"]


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
    script = tmp_path / "skills" / "memory-consolidation" / "scripts" / "memory_consolidation.py"
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
    assert output.splitlines() == ["memory-consolidation", "memory_consolidation", "run"]


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
async def test_jobs_automation_runner_is_single_flight_and_times_out_cleanly(tmp_path: Path):
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
