from __future__ import annotations

import json

import pytest
import yaml

from orchestrator.hermes_transfer import (
    MoveFinalizeOptions,
    TransferMoveError,
    finalize_hashi_to_hermes_move_source,
    finalize_hermes_to_hashi_move_source,
)


def _write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def test_finalize_hashi_to_hermes_move_requires_verified_target(tmp_path):
    root = tmp_path / "hashi"
    root.mkdir()
    _write_json(root / "agents.json", {"agents": [{"name": "zelda", "is_active": True}]})

    with pytest.raises(TransferMoveError, match="target_verified=True"):
        finalize_hashi_to_hermes_move_source(root, "zelda")


def test_finalize_hashi_to_hermes_move_disables_agent_and_tasks(tmp_path):
    root = tmp_path / "hashi"
    root.mkdir()
    _write_json(root / "agents.json", {"agents": [{"name": "zelda", "is_active": True}]})
    _write_json(
        root / "tasks.json",
        {
            "version": 1,
            "heartbeats": [{"id": "zelda-loop", "agent": "zelda", "enabled": True}],
            "crons": [{"id": "zelda-cron", "agent": "zelda", "enabled": True}],
            "nudges": [{"id": "lily-nudge", "agent": "lily", "enabled": True}],
        },
    )

    result = finalize_hashi_to_hermes_move_source(
        root,
        "zelda",
        options=MoveFinalizeOptions(target_verified=True, package_id="pkg-123"),
    )

    agents = json.loads((root / "agents.json").read_text(encoding="utf-8"))
    [agent] = agents["agents"]
    assert agent["is_active"] is False
    assert agent["transfer_disabled"] is True
    assert agent["transfer_package_id"] == "pkg-123"
    tasks = json.loads((root / "tasks.json").read_text(encoding="utf-8"))
    assert tasks["heartbeats"][0]["enabled"] is False
    assert tasks["crons"][0]["enabled"] is False
    assert tasks["nudges"][0]["enabled"] is True
    assert {path.name for path in result.changed_paths} == {"agents.json", "tasks.json"}


def test_finalize_hermes_to_hashi_move_requires_verified_target(tmp_path):
    profile = tmp_path / "hermes" / "profiles" / "xiaoye"
    bridge = tmp_path / "bridge"
    profile.mkdir(parents=True)
    bridge.mkdir()

    with pytest.raises(TransferMoveError, match="target_verified=True"):
        finalize_hermes_to_hashi_move_source(profile, bridge, "xiaoye")


def test_finalize_hermes_to_hashi_move_disables_profile_and_bridge_entry(tmp_path):
    profile = tmp_path / "hermes" / "profiles" / "xiaoye"
    bridge = tmp_path / "bridge"
    profile.mkdir(parents=True)
    bridge.mkdir()
    (profile / "config.yaml").write_text("model: gpt-5.5\n", encoding="utf-8")
    (bridge / "agents.yaml").write_text(
        yaml.safe_dump({"agents": {"xiaoye": {"enabled": True, "display_name": "Xiaoye"}}}),
        encoding="utf-8",
    )

    result = finalize_hermes_to_hashi_move_source(
        profile,
        bridge,
        "xiaoye",
        options=MoveFinalizeOptions(target_verified=True, package_id="pkg-456"),
    )

    agents = yaml.safe_load((bridge / "agents.yaml").read_text(encoding="utf-8"))
    assert agents["agents"]["xiaoye"]["enabled"] is False
    assert agents["agents"]["xiaoye"]["transfer_disabled"] is True
    assert agents["agents"]["xiaoye"]["transfer_package_id"] == "pkg-456"
    config = yaml.safe_load((profile / "config.yaml").read_text(encoding="utf-8"))
    assert config["hashi_transfer"]["source_disabled"] is True
    assert config["hashi_transfer"]["package_id"] == "pkg-456"
    assert (profile / "DISABLED_BY_HASHI_TRANSFER.md").exists()
    assert {path.name for path in result.changed_paths} == {
        "agents.yaml",
        "config.yaml",
        "DISABLED_BY_HASHI_TRANSFER.md",
    }
