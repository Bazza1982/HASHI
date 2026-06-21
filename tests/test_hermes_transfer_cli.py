from __future__ import annotations

import json
from pathlib import Path

import yaml

from orchestrator.hermes_transfer import create_transfer_package
from orchestrator.hermes_transfer.schema import new_manifest
from scripts.hermes_transfer import main


def _write_json(path: Path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _hashi_root(tmp_path):
    root = tmp_path / "hashi"
    root.mkdir()
    workspace = root / "workspaces" / "zelda"
    workspace.mkdir(parents=True)
    (workspace / "agent.md").write_text("# Zelda\n", encoding="utf-8")
    _write_json(
        root / "agents.json",
        {
            "agents": [
                {
                    "name": "zelda",
                    "display_name": "Zelda",
                    "system_md": "workspaces/zelda/agent.md",
                    "workspace_dir": "workspaces/zelda",
                    "is_active": True,
                }
            ]
        },
    )
    _write_json(root / "tasks.json", {"version": 1, "heartbeats": [], "crons": [], "nudges": []})
    return root


def _hermes_target(tmp_path):
    profile = tmp_path / "hermes" / "profiles" / "zelda"
    bridge = tmp_path / "bridge"
    bridge.mkdir(parents=True)
    (bridge / "agents.yaml").write_text(yaml.safe_dump({"agents": {}}), encoding="utf-8")
    return profile, bridge


def _hashi_to_hermes_package(tmp_path):
    package = tmp_path / "zelda.hashi-hermes-agent"
    create_transfer_package(
        package,
        manifest=new_manifest(
            source_runtime="hashi",
            target_runtime="hermes",
            agent_id="zelda",
            display_name="Zelda",
        ),
        normalized_agent={
            "agent_id": "zelda",
            "display_name": "Zelda",
            "identity_text_path": "identity/agent.md",
            "capabilities": {"hchat": True},
            "memory": {
                "strategy": "portable_files_first",
                "notes_path": "memory/import_notes.json",
                "session_state_included": False,
            },
            "secrets": {"included": False, "encrypted": False, "keys": []},
        },
        files={"identity/agent.md": "# Zelda\n"},
    )
    return package


def test_cli_plan_hashi_export_outputs_json(tmp_path, capsys):
    root = _hashi_root(tmp_path)

    code = main(["plan-hashi-export", "--hashi-root", str(root), "--agent", "zelda"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["command"] == "plan-hashi-export"
    assert payload["dry_run_plan"]["operation"] == "hashi_export"


def test_cli_import_hermes_creates_profile(tmp_path, capsys):
    profile, bridge = _hermes_target(tmp_path)
    package = _hashi_to_hermes_package(tmp_path)

    code = main(
        [
            "import-hermes",
            "--profile-dir",
            str(profile),
            "--bridge-home",
            str(bridge),
            "--package",
            str(package),
        ]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["target_profile_name"] == "zelda"
    assert (profile / "AGENT.md").exists()


def test_cli_finalize_move_requires_verified_gate(tmp_path, capsys):
    root = _hashi_root(tmp_path)

    code = main(
        [
            "finalize-move-source",
            "--direction",
            "hashi-to-hermes",
            "--hashi-root",
            str(root),
            "--agent",
            "zelda",
        ]
    )

    assert code == 2
    captured = capsys.readouterr()
    assert "target_verified=True" in captured.err
