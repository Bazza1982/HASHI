from __future__ import annotations

import json
from types import SimpleNamespace

from orchestrator.config_admin import ConfigAdmin


def _admin(tmp_path, agents):
    path = tmp_path / "agents.json"
    path.write_text(
        json.dumps({"global": {"instance_id": "HASHI1"}, "agents": agents}),
        encoding="utf-8",
    )
    return ConfigAdmin(
        SimpleNamespace(
            config_path=path,
            workspaces_root=tmp_path / "workspaces",
        )
    ), path


def test_config_admin_refuses_to_deactivate_or_delete_last_active_agent(tmp_path):
    admin, path = _admin(
        tmp_path,
        [
            {"name": "last", "is_active": True},
            {"name": "dormant", "is_active": False},
        ],
    )
    before = path.read_bytes()

    assert admin.set_agent_active("last", False) is False
    assert path.read_bytes() == before
    assert admin.delete_agent_from_config("last") is False
    assert path.read_bytes() == before


def test_config_admin_allows_change_after_another_agent_is_active(tmp_path):
    admin, path = _admin(
        tmp_path,
        [
            {"name": "one", "is_active": True},
            {"name": "two", "is_active": False},
        ],
    )

    assert admin.set_agent_active("two", True) is True
    assert admin.set_agent_active("one", False) is True
    assert admin.delete_agent_from_config("one") is True
    rows = json.loads(path.read_text(encoding="utf-8-sig"))["agents"]
    assert rows == [{"name": "two", "is_active": True}]


def test_config_admin_duplicate_delete_cannot_remove_every_active_row(tmp_path):
    admin, path = _admin(
        tmp_path,
        [
            {"name": "duplicate", "is_active": True},
            {"name": "duplicate", "is_active": True},
            {"name": "dormant", "is_active": False},
        ],
    )
    before = path.read_bytes()

    assert admin.delete_agent_from_config("duplicate") is False
    assert path.read_bytes() == before
