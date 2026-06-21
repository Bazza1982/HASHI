from __future__ import annotations

import json

import pytest

from orchestrator.hermes_transfer import (
    HashiExportError,
    HashiExportOptions,
    export_hashi_agent,
    plan_hashi_export,
    read_transfer_package,
)


def _write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _fixture_hashi_root(tmp_path):
    root = tmp_path / "hashi"
    root.mkdir()
    workspace = root / "workspaces" / "zelda"
    (workspace / "memory").mkdir(parents=True)
    (workspace / "agent.md").write_text("# Zelda\n\nA careful HASHI agent.\n", encoding="utf-8")
    (workspace / "state.json").write_text('{"mode":"ready"}\n', encoding="utf-8")
    (workspace / "MEMORY.md").write_text("stable memory\n", encoding="utf-8")
    (workspace / "memory" / "project.md").write_text("project memory\n", encoding="utf-8")
    (workspace / "bridge_memory.sqlite").write_bytes(b"sqlite")

    _write_json(
        root / "agents.json",
        {
            "global": {"instance_id": "HASHI1"},
            "agents": [
                {
                    "name": "zelda",
                    "display_name": "Zelda",
                    "emoji": "Z",
                    "type": "flex",
                    "active_backend": "codex-cli",
                    "model": "gpt-5.5",
                    "system_md": "workspaces/zelda/agent.md",
                    "workspace_dir": "workspaces/zelda",
                    "access_scope": "project",
                    "is_active": True,
                },
                {"name": "lily", "workspace_dir": "workspaces/lily"},
            ],
        },
    )
    _write_json(
        root / "tasks.json",
        {
            "version": 1,
            "heartbeats": [
                {
                    "id": "zelda-loop",
                    "agent": "zelda",
                    "enabled": True,
                    "interval_seconds": 600,
                    "prompt": "check",
                },
                {"id": "lily-loop", "agent": "lily", "enabled": True},
            ],
            "crons": [
                {
                    "id": "zelda-cron",
                    "agent": "zelda",
                    "enabled": True,
                    "schedule": "0 7 * * *",
                    "prompt": "daily",
                }
            ],
            "nudges": [],
        },
    )
    _write_json(root / "secrets.json", {"zelda_token": "secret", "lily_token": "secret"})
    return root


def test_plan_hashi_export_builds_dry_run_without_package(tmp_path):
    root = _fixture_hashi_root(tmp_path)
    package = tmp_path / "out.hashi-hermes-agent"

    plan = plan_hashi_export(root, "zelda", package)

    assert not package.exists()
    assert plan.manifest["source_runtime"] == "hashi"
    assert plan.manifest["target_runtime"] == "hermes"
    assert plan.normalized_agent["preferred_backend"]["engine"] == "codex-cli"
    assert plan.files["identity/agent.md"].startswith("# Zelda")
    assert "source/hashi_agent_config.json" in plan.files
    assert "memory/files/MEMORY.md" in plan.files
    assert "workspace/state.json" in plan.files
    assert "schedules/tasks.json" in plan.files
    assert "secrets excluded by default" in "\n".join(plan.warnings)
    assert any(item.path == "identity/agent.md" for item in plan.dry_run_report.planned_writes)


def test_export_hashi_agent_writes_readable_package_with_paused_schedules(tmp_path):
    root = _fixture_hashi_root(tmp_path)
    package = tmp_path / "zelda.hashi-hermes-agent"

    export_hashi_agent(root, "zelda", package)
    transfer = read_transfer_package(package)

    assert transfer.manifest["agent_id"] == "zelda"
    assert "source/hashi_agent_config.json" in transfer.names
    assert "identity/agent.md" in transfer.names
    assert "schedules/tasks.json" in transfer.names

    import zipfile

    with zipfile.ZipFile(package, "r") as archive:
        schedules = json.loads(archive.read("schedules/tasks.json"))
        secrets_summary = json.loads(archive.read("source/secrets_summary.json"))
    assert schedules["heartbeats"][0]["enabled"] is False
    assert schedules["heartbeats"][0]["export_state"] == "paused_review_draft"
    assert schedules["crons"][0]["enabled"] is False
    assert secrets_summary["included"] is False
    assert secrets_summary["matching_keys"] == ["zelda_token"]


def test_plan_hashi_export_skips_large_memory_file(tmp_path):
    root = _fixture_hashi_root(tmp_path)

    plan = plan_hashi_export(
        root,
        "zelda",
        tmp_path / "small.hashi-hermes-agent",
        options=HashiExportOptions(max_file_bytes=4),
    )

    assert "memory/sqlite/bridge_memory.sqlite" not in plan.files
    assert any("exceeds max_file_bytes" in warning for warning in plan.warnings)


def test_plan_hashi_export_refuses_secret_export_in_phase_2(tmp_path):
    root = _fixture_hashi_root(tmp_path)

    with pytest.raises(HashiExportError, match="secret export is not implemented"):
        plan_hashi_export(root, "zelda", options=HashiExportOptions(include_secrets=True))


def test_plan_hashi_export_requires_existing_agent(tmp_path):
    root = _fixture_hashi_root(tmp_path)

    with pytest.raises(HashiExportError, match="agent not found"):
        plan_hashi_export(root, "missing")
