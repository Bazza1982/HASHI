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


def test_config_admin_read_does_not_rewrite_bom_and_next_save_removes_it(tmp_path):
    admin, path = _admin(tmp_path, [{"name": "小能", "is_active": True}])
    original = b'\xef\xbb\xbf' + path.read_bytes().replace(b'\n', b'\r\n')
    path.write_bytes(original)
    raw = admin.load_raw_config()
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]
    raw["global"]["ui_language"] = "zh"
    admin.write_raw_config(raw)
    encoded = path.read_bytes()
    assert not encoded.startswith(b'\xef\xbb\xbf')
    assert b'\r' not in encoded
    assert json.loads(encoded.decode("utf-8"))["agents"][0]["name"] == "小能"


def test_config_admin_stale_writer_cannot_overwrite_newer_edit(tmp_path):
    import pytest
    from orchestrator.config_json import ConfigConflictError

    admin, path = _admin(tmp_path, [{"name": "one", "is_active": True}])
    other = ConfigAdmin(admin.paths)
    stale = admin.load_raw_config()
    newer = other.load_raw_config()
    newer["global"]["ui_language"] = "zh"
    other.write_raw_config(newer)
    before = path.read_bytes()
    stale["global"]["another_setting"] = True
    with pytest.raises(ConfigConflictError):
        admin.write_raw_config(stale)
    assert path.read_bytes() == before


def test_config_admin_publish_failure_keeps_exact_original_bytes(tmp_path, monkeypatch):
    import pytest
    from orchestrator import config_json

    admin, path = _admin(tmp_path, [{"name": "one", "is_active": True}])
    before = path.read_bytes()
    raw = admin.load_raw_config()
    raw["global"]["ui_language"] = "zh"

    def fail(*args):
        raise OSError("cannot replace")

    monkeypatch.setattr(config_json.os, "replace", fail)
    with pytest.raises(OSError, match="cannot replace"):
        admin.write_raw_config(raw)
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("*.tmp"))


def test_config_admin_concurrent_deactivation_preserves_one_active_agent(tmp_path, monkeypatch):
    import pytest
    from orchestrator.config_json import ConfigConflictError

    admin, path = _admin(tmp_path, [
        {"name": "one", "is_active": True},
        {"name": "two", "is_active": True},
    ])
    other = ConfigAdmin(admin.paths)
    read = admin.load_raw_config

    def interleaved_read():
        snapshot = read()
        assert other.set_agent_active("two", False)
        return snapshot

    monkeypatch.setattr(admin, "load_raw_config", interleaved_read)
    with pytest.raises(ConfigConflictError):
        admin.set_agent_active("one", False)
    assert other.load_raw_config()["agents"] == [
        {"name": "one", "is_active": True},
        {"name": "two", "is_active": False},
    ]
