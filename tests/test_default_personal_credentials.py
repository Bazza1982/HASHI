from __future__ import annotations

import json

import pytest

from orchestrator.config_json import ConfigConflictError, read_config_json
from orchestrator.workbench_auth import ensure_local_api_credential


@pytest.mark.parametrize("initial", [{}, {"provider_key": "retain-provider-key"}])
def test_personal_install_prepares_instance_credentials_once(tmp_path, monkeypatch, initial):
    monkeypatch.delenv("HASHI_REMOTE_SHARED_TOKEN", raising=False)
    path = tmp_path / "secrets.json"
    path.write_text(json.dumps(initial), encoding="utf-8")
    first = ensure_local_api_credential(
        read_config_json(path), profile="personal", remote_runtime_sids=(),
    )
    assert len(first["hashi_remote_shared_token"]) >= 48
    assert len(first["workbench_admin_token"]) >= 48
    assert first["hashi_remote_shared_token"] != first["workbench_admin_token"]
    assert all(first[key] == value for key, value in initial.items())
    published = path.read_bytes()
    again = ensure_local_api_credential(
        read_config_json(path), profile="personal", remote_runtime_sids=(),
    )
    assert dict(again) == dict(first)
    assert path.read_bytes() == published


def test_environment_network_key_is_not_copied_into_instance_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HASHI_REMOTE_SHARED_TOKEN", "environment-network-fixture")
    path = tmp_path / "secrets.json"
    path.write_text("{}", encoding="utf-8")
    result = ensure_local_api_credential(
        read_config_json(path), profile="personal", remote_runtime_sids=(),
    )
    assert result["workbench_admin_token"]
    assert "hashi_remote_shared_token" not in result
    assert "environment-network-fixture" not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("field", ["hashi_remote_shared_token", "workbench_admin_token"])
def test_invalid_existing_credential_is_reported_without_overwriting(tmp_path, monkeypatch, field):
    monkeypatch.delenv("HASHI_REMOTE_SHARED_TOKEN", raising=False)
    path = tmp_path / "secrets.json"
    original = json.dumps({field: ["invalid"], "provider_key": "keep"})
    path.write_text(original, encoding="utf-8")
    with pytest.raises(ValueError, match=field):
        ensure_local_api_credential(
            read_config_json(path), profile="personal", remote_runtime_sids=(),
        )
    assert path.read_text(encoding="utf-8") == original


def test_personal_credential_bootstrap_keeps_revision_conflicts_and_enterprise_policy(tmp_path, monkeypatch):
    monkeypatch.delenv("HASHI_REMOTE_SHARED_TOKEN", raising=False)
    path = tmp_path / "secrets.json"
    path.write_text("{}", encoding="utf-8")
    snapshot = read_config_json(path)
    assert ensure_local_api_credential(snapshot, profile="enterprise", remote_runtime_sids=()) is snapshot
    assert path.read_text(encoding="utf-8") == "{}"
    path.write_text('{"concurrent": "winner"}', encoding="utf-8")
    with pytest.raises(ConfigConflictError):
        ensure_local_api_credential(snapshot, profile="personal", remote_runtime_sids=())
    assert json.loads(path.read_text(encoding="utf-8")) == {"concurrent": "winner"}
