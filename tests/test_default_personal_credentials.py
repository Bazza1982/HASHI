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


def test_credential_bootstrap_keeps_revision_conflicts(tmp_path, monkeypatch):
    monkeypatch.delenv("HASHI_REMOTE_SHARED_TOKEN", raising=False)
    path = tmp_path / "secrets.json"
    path.write_text("{}", encoding="utf-8")
    snapshot = read_config_json(path)
    path.write_text('{"concurrent": "winner"}', encoding="utf-8")
    with pytest.raises(ConfigConflictError):
        ensure_local_api_credential(snapshot, profile="personal", remote_runtime_sids=())
    assert json.loads(path.read_text(encoding="utf-8")) == {"concurrent": "winner"}


@pytest.mark.parametrize("profile", ["team", "enterprise"])
def test_governed_bootstrap_creates_only_private_scheduler_signing_key(tmp_path, profile, monkeypatch):
    from orchestrator import workbench_auth

    monkeypatch.setattr(workbench_auth, "resolve_remote_runtime_sids",
                        lambda _: pytest.fail("Scheduler bootstrap must not query Remote"))
    path = tmp_path / "secrets.json"
    original = {"provider_key": "keep", "workbench_admin_token": "legacy-key"}
    path.write_text(json.dumps(original), encoding="utf-8")
    first = ensure_local_api_credential(read_config_json(path), profile=profile)
    assert all(first[key] == value for key, value in original.items())
    key = workbench_auth.scheduler_delegation_key(tmp_path)
    assert len(key) >= 48 and key != first["workbench_admin_token"]
    assert "scheduler_delegation_key" not in first
    assert "hashi_remote_shared_token" not in first
    published = path.read_bytes()
    again = ensure_local_api_credential(read_config_json(path), profile=profile)
    assert dict(again) == dict(first) and path.read_bytes() == published
    assert workbench_auth.scheduler_delegation_key(tmp_path) == key


def test_governed_scheduler_key_creation_is_safe_under_concurrency(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from orchestrator.workbench_auth import scheduler_delegation_key

    with ThreadPoolExecutor(max_workers=4) as pool:
        values = list(pool.map(lambda _: scheduler_delegation_key(tmp_path, create=True), range(4)))
    assert len(set(values)) == 1 and len(values[0]) >= 48


def test_governed_scheduler_key_follows_instance_home_not_secrets_directory(tmp_path):
    from orchestrator.workbench_auth import scheduler_delegation_key

    secret_dir, instance = tmp_path / "secrets", tmp_path / "instance"
    secret_dir.mkdir()
    path = secret_dir / "secrets.json"
    path.write_text("{}", encoding="utf-8")
    ensure_local_api_credential(read_config_json(path), profile="enterprise", scheduler_root=instance)
    assert scheduler_delegation_key(instance)
    assert scheduler_delegation_key(secret_dir) == ""


def test_governed_config_bootstrap_and_worker_read_same_instance_key(tmp_path):
    from orchestrator.config import ConfigManager
    from orchestrator.workbench_auth import scheduler_delegation_key
    from tests.test_config import _minimal_flex_agent, _write_base_files

    instance, secret_dir = tmp_path / "instance", tmp_path / "secrets"
    instance.mkdir()
    secret_dir.mkdir()
    config, initial_secrets = _write_base_files(instance, _minimal_flex_agent())
    document = json.loads(config.read_text(encoding="utf-8"))
    document["global"].update(deployment_profile="enterprise", organization_id="ORG-001",
                              enterprise_bootstrap_complete=True)
    config.write_text(json.dumps(document), encoding="utf-8")
    separate_secrets = secret_dir / "secrets.json"
    separate_secrets.write_bytes(initial_secrets.read_bytes())
    manager = ConfigManager(config, separate_secrets, bridge_home=instance)
    manager.load(bootstrap_local_api_credential=True)
    key = scheduler_delegation_key(instance)
    cfg, _, _ = manager.load()
    assert key and scheduler_delegation_key(cfg.bridge_home) == key
    assert scheduler_delegation_key(secret_dir) == ""


def test_scheduler_key_failure_does_not_block_governed_startup(tmp_path, monkeypatch, caplog):
    from orchestrator import workbench_auth

    path = tmp_path / "secrets.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(workbench_auth, "scheduler_delegation_key",
                        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("sensitive detail")))
    document = read_config_json(path)
    assert ensure_local_api_credential(document, profile="enterprise") is document
    assert "Scheduler delegation key unavailable" in caplog.text
    assert "sensitive detail" not in caplog.text


def test_existing_personal_credentials_do_not_depend_on_remote_scheduler(tmp_path, monkeypatch):
    from orchestrator import workbench_auth

    path = tmp_path / "secrets.json"
    initial = {"workbench_admin_token": "existing-local", "hashi_remote_shared_token": "existing-network"}
    path.write_text(json.dumps(initial), encoding="utf-8")
    before = path.read_bytes()
    monkeypatch.setattr(workbench_auth, "resolve_remote_runtime_sids",
                        lambda _root: pytest.fail("local startup queried optional Remote"))
    result = ensure_local_api_credential(read_config_json(path), profile="personal")
    assert dict(result) == initial
    assert path.read_bytes() == before


def test_needed_credential_write_still_requires_accurate_remote_consumer(tmp_path, monkeypatch):
    from orchestrator import workbench_auth

    monkeypatch.delenv("HASHI_REMOTE_SHARED_TOKEN", raising=False)
    path = tmp_path / "secrets.json"
    path.write_text('{"workbench_admin_token":"existing-local"}', encoding="utf-8")
    before = path.read_bytes()
    def unavailable(_root):
        raise RuntimeError("synthetic Remote principal failure")
    monkeypatch.setattr(workbench_auth, "resolve_remote_runtime_sids", unavailable)
    with pytest.raises(RuntimeError, match="synthetic Remote principal"):
        ensure_local_api_credential(read_config_json(path), profile="personal")
    assert path.read_bytes() == before


def test_explicit_remote_acl_repair_does_not_rewrite_existing_credentials(tmp_path, monkeypatch):
    from tools import private_files

    path = tmp_path / "secrets.json"
    initial = {"workbench_admin_token": "existing-local", "hashi_remote_shared_token": "existing-network"}
    path.write_text(json.dumps(initial), encoding="utf-8")
    before = path.read_bytes()
    calls = []
    monkeypatch.setattr(private_files, "protect_private_file",
                        lambda target, **kw: calls.append((target, kw)))
    ensure_local_api_credential(read_config_json(path), profile="personal",
                                remote_runtime_sids=("S-1-5-19",))
    assert calls == [(path, {"additional_full_control_sids": ("S-1-5-19",)})]
    assert path.read_bytes() == before



def test_remote_runtime_access_uses_exact_resolved_consumer(tmp_path, monkeypatch):
    from orchestrator import workbench_auth
    from tools import private_files

    path = tmp_path / "secrets.json"
    path.write_text("{}", encoding="utf-8")
    calls = []
    monkeypatch.setattr(workbench_auth, "resolve_remote_runtime_sids",
                        lambda root: ("S-1-5-19",) if root == tmp_path else pytest.fail("wrong root"))
    monkeypatch.setattr(private_files, "protect_private_file",
                        lambda target, **kw: calls.append((target, kw)))
    workbench_auth.ensure_remote_runtime_access(path, tmp_path)
    assert calls == [(path, {"additional_full_control_sids": ("S-1-5-19",)})]


def test_remote_runtime_access_never_repairs_with_unresolved_consumer(tmp_path, monkeypatch):
    from orchestrator import workbench_auth
    from tools import private_files

    path = tmp_path / "secrets.json"
    path.write_text('{"fixture":"keep"}', encoding="utf-8")
    before = path.read_bytes()
    def unavailable(_root):
        raise RuntimeError("synthetic Remote principal failure")
    monkeypatch.setattr(workbench_auth, "resolve_remote_runtime_sids", unavailable)
    monkeypatch.setattr(private_files, "protect_private_file",
                        lambda *_args, **_kw: pytest.fail("ACL was changed"))
    with pytest.raises(RuntimeError, match="synthetic Remote principal"):
        workbench_auth.ensure_remote_runtime_access(path, tmp_path)
    assert path.read_bytes() == before
