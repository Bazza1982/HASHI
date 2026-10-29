"""Permission configuration must preserve legacy state and revision conflicts."""
import json
from types import SimpleNamespace

import pytest

from orchestrator.config_admin import ConfigAdmin
from orchestrator.config_json import ConfigConflictError, ConfigDurabilityError
from orchestrator.permissions.models import PermissionLevel, PermissionSettings


def make_admin(tmp_path, agents):
    path = tmp_path / "agents.json"
    path.write_text(json.dumps({"global": {"instance_id": "TEST"}, "agents": agents}), encoding="utf-8")
    return ConfigAdmin(SimpleNamespace(config_path=path, workspaces_root=tmp_path / "workspaces")), path


def test_missing_legacy_setting_reads_l0_without_creating_or_rewriting_anything(tmp_path):
    admin, path = make_admin(tmp_path, [{"name": "agent", "type": "flex"}])
    before = path.read_bytes()
    snapshot = admin.read_agent_permission_settings("agent")
    assert snapshot.settings.requested_level is PermissionLevel.L0
    assert snapshot.revision
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


def test_explicit_level_is_saved_only_for_the_selected_agent(tmp_path):
    admin, path = make_admin(tmp_path, [{"name": "one"}, {"name": "two"}])
    snapshot = admin.read_agent_permission_settings("one")
    updated = admin.write_agent_permission_settings(
        "one", PermissionSettings(requested_level=PermissionLevel.L1),
        expected_revision=snapshot.revision,
    )
    assert updated.settings.requested_level is PermissionLevel.L1
    assert updated.revision != snapshot.revision
    rows = json.loads(path.read_text(encoding="utf-8-sig"))["agents"]
    assert rows[0]["permission_settings"]["requested_level"] == "L1"
    assert "permission_settings" not in rows[1]


def test_stale_config_revision_does_not_overwrite_another_edit(tmp_path):
    admin, path = make_admin(tmp_path, [{"name": "agent", "display_name": "old"}])
    snapshot = admin.read_agent_permission_settings("agent")
    newer = admin.load_raw_config()
    newer["agents"][0]["display_name"] = "new"
    admin.write_raw_config(newer)
    before = path.read_bytes()
    with pytest.raises(ConfigConflictError):
        admin.write_agent_permission_settings(
            "agent", PermissionSettings(requested_level=PermissionLevel.L1),
            expected_revision=snapshot.revision,
        )
    assert path.read_bytes() == before
    assert json.loads(before)["agents"][0]["display_name"] == "new"


def test_concurrent_change_between_read_and_publish_is_not_replayed(tmp_path, monkeypatch):
    admin, path = make_admin(tmp_path, [{"name": "agent", "display_name": "old"}])
    snapshot = admin.read_agent_permission_settings("agent")
    original = admin.write_raw_config
    calls = []

    def racing_write(document):
        calls.append(1)
        other = admin.load_raw_config()
        other["agents"][0]["display_name"] = "concurrent"
        original(other)
        return original(document)

    monkeypatch.setattr(admin, "write_raw_config", racing_write)
    with pytest.raises(ConfigConflictError):
        admin.write_agent_permission_settings(
            "agent", PermissionSettings(requested_level=PermissionLevel.L1),
            expected_revision=snapshot.revision,
        )
    assert len(calls) == 1
    row = json.loads(path.read_text(encoding="utf-8-sig"))["agents"][0]
    assert row["display_name"] == "concurrent"
    assert "permission_settings" not in row


def test_committed_durability_error_is_not_retried_or_rolled_back(tmp_path, monkeypatch):
    admin, path = make_admin(tmp_path, [{"name": "agent"}])
    snapshot = admin.read_agent_permission_settings("agent")
    original = admin.write_raw_config
    calls = []

    def committed_but_unconfirmed(document):
        calls.append(1)
        original(document)
        raise ConfigDurabilityError("publication happened but durability was not confirmed")

    monkeypatch.setattr(admin, "write_raw_config", committed_but_unconfirmed)
    with pytest.raises(ConfigDurabilityError):
        admin.write_agent_permission_settings(
            "agent", PermissionSettings(requested_level=PermissionLevel.L1),
            expected_revision=snapshot.revision,
        )
    assert len(calls) == 1
    assert json.loads(path.read_text(encoding="utf-8-sig"))["agents"][0]["permission_settings"]["requested_level"] == "L1"


def test_malformed_or_ambiguous_agent_config_cannot_be_overwritten(tmp_path):
    admin, path = make_admin(tmp_path, [{"name": "agent"}, {"name": "agent"}])
    before = path.read_bytes()
    with pytest.raises(ValueError):
        admin.read_agent_permission_settings("agent")
    assert path.read_bytes() == before
    path.write_text("{invalid", encoding="utf-8")
    bad = path.read_bytes()
    with pytest.raises(ValueError):
        admin.read_agent_permission_settings("agent")
    assert path.read_bytes() == bad


def test_invalid_existing_permission_level_is_not_treated_as_legacy_l0(tmp_path):
    admin, path = make_admin(tmp_path, [{"name": "agent", "permission_settings": {"requested_level": "L3"}}])
    before = path.read_bytes()
    with pytest.raises(ValueError):
        admin.read_agent_permission_settings("agent")
    assert path.read_bytes() == before


def test_nested_permission_settings_read_and_update_in_place(tmp_path):
    admin, path = make_admin(tmp_path, [{
        "name": "agent", "extra": {
            "permission_settings": {"requested_level": "L2"}, "other": {"keep": True},
        },
    }])
    before = path.read_bytes()
    snapshot = admin.read_agent_permission_settings("agent")
    assert snapshot.settings.requested_level is PermissionLevel.L2
    assert path.read_bytes() == before
    admin.write_agent_permission_settings(
        "agent", PermissionSettings(requested_level=PermissionLevel.L1),
        expected_revision=snapshot.revision,
    )
    row = json.loads(path.read_text(encoding="utf-8-sig"))["agents"][0]
    assert "permission_settings" not in row
    assert row["extra"]["permission_settings"]["requested_level"] == "L1"
    assert row["extra"]["other"] == {"keep": True}


@pytest.mark.parametrize("nested", [None, {"requested_level": "L1"}])
def test_duplicate_top_level_and_nested_settings_are_never_read_or_overwritten(tmp_path, nested):
    admin, path = make_admin(tmp_path, [{
        "name": "agent",
        "permission_settings": {"requested_level": "L0"},
        "extra": {"permission_settings": nested, "other": "keep"},
    }])
    before = path.read_bytes()
    revision = admin.load_raw_config().revision
    with pytest.raises(ValueError):
        admin.read_agent_permission_settings("agent")
    with pytest.raises(ValueError):
        admin.write_agent_permission_settings(
            "agent", PermissionSettings(requested_level=PermissionLevel.L1), expected_revision=revision,
        )
    assert path.read_bytes() == before


@pytest.mark.parametrize("nested", [None, [], {"requested_level": "L3"}])
def test_invalid_nested_permission_settings_cannot_default_or_be_overwritten(tmp_path, nested):
    admin, path = make_admin(tmp_path, [{"name": "agent", "extra": {"permission_settings": nested}}])
    before = path.read_bytes()
    revision = admin.load_raw_config().revision
    with pytest.raises(ValueError):
        admin.read_agent_permission_settings("agent")
    with pytest.raises(ValueError):
        admin.write_agent_permission_settings(
            "agent", PermissionSettings(requested_level=PermissionLevel.L1), expected_revision=revision,
        )
    assert path.read_bytes() == before


@pytest.mark.parametrize("extra", ["invalid", [], ["invalid"]])
def test_malformed_extra_is_not_used_as_a_default_or_write_source(tmp_path, extra):
    admin, path = make_admin(tmp_path, [{"name": "agent", "extra": extra}])
    before = path.read_bytes()
    revision = admin.load_raw_config().revision
    with pytest.raises(ValueError):
        admin.read_agent_permission_settings("agent")
    with pytest.raises(ValueError):
        admin.write_agent_permission_settings(
            "agent", PermissionSettings(requested_level=PermissionLevel.L1), expected_revision=revision,
        )
    assert path.read_bytes() == before


def test_stale_nested_settings_update_preserves_a_concurrent_change(tmp_path):
    admin, path = make_admin(tmp_path, [{
        "name": "agent", "extra": {"permission_settings": {"requested_level": "L2"}, "other": "old"},
    }])
    snapshot = admin.read_agent_permission_settings("agent")
    changed = admin.load_raw_config()
    changed["agents"][0]["extra"]["other"] = "new"
    admin.write_raw_config(changed)
    before = path.read_bytes()
    with pytest.raises(ConfigConflictError):
        admin.write_agent_permission_settings(
            "agent", PermissionSettings(requested_level=PermissionLevel.L1),
            expected_revision=snapshot.revision,
        )
    assert path.read_bytes() == before
    row = json.loads(before)["agents"][0]
    assert row["extra"]["other"] == "new"
    assert row["extra"]["permission_settings"]["requested_level"] == "L2"
