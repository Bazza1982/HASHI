from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.config_json import read_config_json, write_config_json
from orchestrator.telegram_access import TelegramAccessError, TelegramAccessService


def _instance(root: Path, *, owner: int | None = 42, global_users=None):
    config_path = root / "agents.json"
    secrets_path = root / "secrets.json"
    global_config = {"instance_id": "TEST", "unrelated": "preserve"}
    if global_users is not None:
        global_config["authorized_telegram_user_ids"] = global_users
    write_config_json(config_path, {
        "global": global_config,
        "agents": [
            {"name": "lily", "extra": {"authorized_telegram_ids": [75]}, "other": "keep"},
            {"name": "momo", "extra": {}, "other": "keep"},
        ],
        "unrelated": {"preserve": True},
    })
    secrets = {"unrelated": "preserve"}
    if owner is not None:
        secrets["authorized_telegram_id"] = owner
    write_config_json(secrets_path, secrets)
    return TelegramAccessService(config_path=config_path, secrets_path=secrets_path), config_path, secrets_path


def test_global_and_agent_grants_are_additive_and_revisioned(tmp_path):
    service, config_path, secrets_path = _instance(tmp_path)
    before_secret = secrets_path.read_bytes()
    lily = service.status("lily")
    assert lily["primary_user_id"] == "42"
    assert lily["global_user_ids"] == ["42"]
    assert lily["agent_user_ids"] == ["75"]

    after_global = service.change("lily", expected_revision=lily["revision"],
                                  scope="global", action="add", user_id=" 100 ")
    assert after_global["global_user_ids"] == ["42", "100"]
    assert after_global["agent_user_ids"] == ["75"]
    momo = service.status("momo")
    assert momo["global_user_ids"] == ["42", "100"]
    assert momo["agent_user_ids"] == []
    after_agent = service.change("momo", expected_revision=momo["revision"],
                                 scope="agent", action="add", user_id="200")
    assert after_agent["agent_user_ids"] == ["200"]
    assert service.status("lily")["agent_user_ids"] == ["75"]
    assert secrets_path.read_bytes() == before_secret
    saved = read_config_json(config_path)
    assert saved["global"]["unrelated"] == "preserve"
    assert saved["agents"][0]["other"] == "keep"
    assert saved["unrelated"] == {"preserve": True}


def test_removal_respects_independent_grants_and_cannot_remove_primary(tmp_path):
    service, _config_path, _secrets_path = _instance(tmp_path, global_users=[100])
    first = service.status("lily")
    added = service.change("lily", expected_revision=first["revision"],
                           scope="agent", action="add", user_id="100")
    result = service.change("lily", expected_revision=added["revision"],
                            scope="global", action="remove", user_id="100")
    assert result["global_user_ids"] == ["42"]
    assert result["agent_user_ids"] == ["75", "100"]
    with pytest.raises(TelegramAccessError) as caught:
        service.change("lily", expected_revision=result["revision"],
                       scope="global", action="remove", user_id="42")
    assert caught.value.error_code == "telegram_access_primary_required"


def test_first_global_user_preserves_existing_owner_scope(tmp_path):
    service, config_path, secrets_path = _instance(tmp_path, owner=None)
    before = secrets_path.read_bytes()
    state = service.status("lily")
    assert state["primary_user_id"] is None
    assert state["global_user_ids"] == []
    result = service.change("lily", expected_revision=state["revision"],
                            scope="global", action="add", user_id="9001")
    assert result["primary_user_id"] == "9001"
    assert result["global_user_ids"] == ["9001"]
    config = read_config_json(config_path)
    assert config["global"]["telegram_owner_id"] == 9001
    assert config["global"]["owner_scope_id"] == "user:0"
    assert secrets_path.read_bytes() == before


def test_stale_snapshot_cannot_overwrite_unrelated_change(tmp_path):
    service, config_path, _secrets_path = _instance(tmp_path)
    state = service.status("lily")
    config = read_config_json(config_path)
    config["agents"][1]["other"] = "concurrent-change"
    write_config_json(config_path, config)
    with pytest.raises(TelegramAccessError) as caught:
        service.change("lily", expected_revision=state["revision"],
                       scope="global", action="add", user_id="300")
    assert caught.value.error_code == "telegram_access_changed"
    assert read_config_json(config_path)["agents"][1]["other"] == "concurrent-change"
    assert service.status("lily")["global_user_ids"] == ["42"]


def test_stale_owner_and_corrupt_data_fail_without_replacement(tmp_path):
    service, config_path, secrets_path = _instance(tmp_path)
    old = service.status("lily")
    secrets = read_config_json(secrets_path)
    secrets["authorized_telegram_id"] = 99
    write_config_json(secrets_path, secrets)
    config_bytes = config_path.read_bytes()
    with pytest.raises(TelegramAccessError) as caught:
        service.change("lily", expected_revision=old["revision"],
                       scope="global", action="add", user_id="300")
    assert caught.value.error_code == "telegram_access_changed"
    assert config_path.read_bytes() == config_bytes
    secrets_path.write_text("{broken", encoding="utf-8")
    with pytest.raises(TelegramAccessError):
        service.change("lily", expected_revision=old["revision"],
                       scope="global", action="add", user_id="300")
    assert config_path.read_bytes() == config_bytes


@pytest.mark.parametrize("value", ["", "0", "-1", "abc", "4503599627370496", 7, True])
def test_rejects_invalid_user_ids_without_change(tmp_path, value):
    service, config_path, _secrets_path = _instance(tmp_path)
    state = service.status("lily")
    before = config_path.read_bytes()
    with pytest.raises(TelegramAccessError):
        service.change("lily", expected_revision=state["revision"],
                       scope="agent", action="add", user_id=value)
    assert config_path.read_bytes() == before


def test_corrupt_allowlist_never_becomes_writable_fallback(tmp_path):
    service, config_path, _secrets_path = _instance(tmp_path, global_users={"bad": "shape"})
    before = config_path.read_bytes()
    with pytest.raises(TelegramAccessError):
        service.status("lily")
    assert config_path.read_bytes() == before


def test_revision_is_bound_to_the_selected_agent(tmp_path):
    service, config_path, _secrets_path = _instance(tmp_path)
    lily = service.status("lily")
    momo = service.status("momo")
    assert lily["revision"] != momo["revision"]
    before = config_path.read_bytes()
    with pytest.raises(TelegramAccessError) as caught:
        service.change("lily", expected_revision=momo["revision"],
                       scope="agent", action="add", user_id="300")
    assert caught.value.error_code == "telegram_access_changed"
    assert config_path.read_bytes() == before


@pytest.mark.parametrize("scope, action", [([], "add"), ("agent", []), (None, "add")])
def test_invalid_operation_is_rejected(tmp_path, scope, action):
    service, _config_path, _secrets_path = _instance(tmp_path)
    with pytest.raises(TelegramAccessError):
        service.change("lily", expected_revision=service.status("lily")["revision"],
                       scope=scope, action=action, user_id="300")

def test_concurrent_update_during_save_is_never_overwritten(tmp_path, monkeypatch):
    import orchestrator.telegram_access as access

    service, config_path, _ = _instance(tmp_path)
    state = service.status("lily")
    original = access.write_config_json

    def conflict(path, document):
        concurrent = read_config_json(path)
        concurrent["unrelated"]["new"] = "preserve"
        original(path, concurrent)
        return original(path, document)

    monkeypatch.setattr(access, "write_config_json", conflict)
    with pytest.raises(TelegramAccessError) as caught:
        service.change("lily", expected_revision=state["revision"],
                       scope="agent", action="add", user_id="300")
    assert caught.value.error_code == "telegram_access_changed"
    assert read_config_json(config_path)["unrelated"]["new"] == "preserve"
    assert service.status("lily")["agent_user_ids"] == ["75"]


def test_uncertain_commit_is_reported_without_repeating_write(tmp_path, monkeypatch):
    import orchestrator.telegram_access as access
    from orchestrator.config_json import ConfigDurabilityError

    service, _config_path, _ = _instance(tmp_path)
    state = service.status("lily")
    original = access.write_config_json
    calls = []

    def uncertain(path, document):
        calls.append(1)
        original(path, document)
        raise ConfigDurabilityError("published but durability is uncertain")

    monkeypatch.setattr(access, "write_config_json", uncertain)
    with pytest.raises(TelegramAccessError) as caught:
        service.change("lily", expected_revision=state["revision"],
                       scope="agent", action="add", user_id="300")
    assert caught.value.error_code == "telegram_access_outcome_unknown"
    assert calls == [1]
    assert service.status("lily")["agent_user_ids"] == ["75", "300"]
