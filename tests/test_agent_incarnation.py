from __future__ import annotations

import json

import pytest

from orchestrator import agent_incarnation


def _config(tmp_path, lifecycle=None):
    row = {"name": "zelda", "is_active": True}
    if lifecycle is not None:
        row["agent_lifecycle_id"] = lifecycle
    path = tmp_path / "agents.json"
    path.write_text(
        json.dumps({"global": {"instance_id": "HASHI1"}, "agents": [row]}),
        encoding="utf-8",
    )
    return path


def test_missing_incarnation_is_assigned_once_and_reused(tmp_path):
    path = _config(tmp_path)

    first = agent_incarnation.ensure_agent_lifecycle_id(path, "zelda")
    second = agent_incarnation.ensure_agent_lifecycle_id(path, "zelda")

    assert first == second
    assert agent_incarnation.valid_agent_lifecycle_id(first)
    assert json.loads(path.read_text())["agents"][0]["agent_lifecycle_id"] == first


def test_malformed_existing_incarnation_is_not_silently_replaced(tmp_path):
    path = _config(tmp_path, "not-a-stable-id")
    before = path.read_bytes()

    with pytest.raises(ValueError, match="malformed lifecycle identity"):
        agent_incarnation.ensure_agent_lifecycle_id(path, "zelda")

    assert path.read_bytes() == before
