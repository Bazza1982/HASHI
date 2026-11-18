"""Optional Remote preparation must not hold up an already-provisioned local API."""
from __future__ import annotations

import json

import pytest

from orchestrator import workbench_auth
from orchestrator.config import ConfigManager
from orchestrator.config_json import ConfigConflictError, read_config_json
from remote.security.shared_token import load_shared_token
from tests.test_config import _minimal_flex_agent, _write_base_files


@pytest.fixture
def provisioned_instance(tmp_path, monkeypatch):
    monkeypatch.delenv("HASHI_REMOTE_SHARED_TOKEN", raising=False)
    monkeypatch.delenv("HASHI_REMOTE_ROOT", raising=False)
    config, path = _write_base_files(tmp_path, _minimal_flex_agent())
    path.write_text(json.dumps({
        "workbench_admin_token": "existing-local-fixture",
        "provider_key": "unrelated-fixture",
    }), encoding="utf-8")
    return config, path


def test_existing_local_api_starts_while_optional_remote_principal_is_unavailable(
    provisioned_instance, tmp_path, monkeypatch,
):
    config, path = provisioned_instance
    before = path.read_bytes()

    def unavailable(_root):
        raise RuntimeError("synthetic principal lookup failure")

    monkeypatch.setattr(workbench_auth, "resolve_remote_runtime_sids", unavailable)
    _, _, secrets = ConfigManager(config, path, bridge_home=tmp_path).load(
        bootstrap_local_api_credential=True,
    )
    assert secrets["workbench_admin_token"] == "existing-local-fixture"
    assert "hashi_remote_shared_token" not in secrets
    assert path.read_bytes() == before

    with pytest.raises(RuntimeError, match="principal lookup failure"):
        workbench_auth.ensure_remote_runtime_access(path, tmp_path)
    assert path.read_bytes() == before

    monkeypatch.setattr(workbench_auth, "resolve_remote_runtime_sids", lambda _root: ())
    workbench_auth.ensure_remote_runtime_access(path, tmp_path)
    updated = read_config_json(path)
    assert updated["workbench_admin_token"] == "existing-local-fixture"
    assert updated["provider_key"] == "unrelated-fixture"
    assert len(updated["hashi_remote_shared_token"]) >= 48
    assert load_shared_token(tmp_path) == updated["hashi_remote_shared_token"]
    published = path.read_bytes()
    workbench_auth.ensure_remote_runtime_access(path, tmp_path)
    assert path.read_bytes() == published


def test_deferral_does_not_write_to_a_separate_remote_root(
    provisioned_instance, tmp_path, monkeypatch,
):
    config, path = provisioned_instance
    separate_root = tmp_path / "separate"
    separate_root.mkdir()
    monkeypatch.setenv("HASHI_REMOTE_ROOT", str(separate_root))
    monkeypatch.setattr(workbench_auth, "resolve_remote_runtime_sids",
                        lambda _root: (_ for _ in ()).throw(RuntimeError("principal unavailable")))
    before = path.read_bytes()
    _, _, secrets = ConfigManager(config, path, bridge_home=tmp_path).load(
        bootstrap_local_api_credential=True,
    )
    assert secrets["workbench_admin_token"] == "existing-local-fixture"
    assert "hashi_remote_shared_token" not in secrets
    assert path.read_bytes() == before
    assert not (separate_root / "secrets.json").exists()


def test_disabled_remote_neither_queries_principal_nor_creates_network_token(
    provisioned_instance, tmp_path, monkeypatch,
):
    _config, path = provisioned_instance
    remote_config = tmp_path / "remote" / "config.yaml"
    remote_config.parent.mkdir()
    remote_config.write_text("lifecycle:\n  remote_enabled: false\n", encoding="utf-8")
    before = path.read_bytes()
    monkeypatch.setattr(workbench_auth, "resolve_remote_runtime_sids",
                        lambda _root: pytest.fail("disabled Remote queried its principal"))
    workbench_auth.ensure_remote_runtime_access(path, tmp_path)
    assert path.read_bytes() == before


def test_concurrent_secret_update_is_preserved_on_remote_preparation_conflict(
    provisioned_instance, tmp_path, monkeypatch,
):
    _config, path = provisioned_instance
    monkeypatch.setattr(workbench_auth, "resolve_remote_runtime_sids", lambda _root: ())
    real_write = workbench_auth.write_config_json
    calls = []

    def concurrent_write(target, document, **kwargs):
        if not calls:
            calls.append(True)
            concurrent = read_config_json(target)
            concurrent["concurrent_fixture"] = "must-survive"
            real_write(target, concurrent)
        return real_write(target, document, **kwargs)

    monkeypatch.setattr(workbench_auth, "write_config_json", concurrent_write)
    with pytest.raises(ConfigConflictError):
        workbench_auth.ensure_remote_runtime_access(path, tmp_path)
    assert read_config_json(path)["concurrent_fixture"] == "must-survive"
    assert "hashi_remote_shared_token" not in read_config_json(path)

    monkeypatch.setattr(workbench_auth, "write_config_json", real_write)
    workbench_auth.ensure_remote_runtime_access(path, tmp_path)
    assert read_config_json(path)["concurrent_fixture"] == "must-survive"
    assert load_shared_token(tmp_path)
# HASHI-Fingerprint: hfp_f550bf3d7d95433fb0ab06bdad735ebc



def test_existing_environment_network_token_is_not_persisted(
    provisioned_instance, tmp_path, monkeypatch,
):
    _config, path = provisioned_instance
    monkeypatch.setenv("HASHI_REMOTE_SHARED_TOKEN", "environment-fixture")
    monkeypatch.setattr(workbench_auth, "resolve_remote_runtime_sids", lambda _root: ())
    before = path.read_bytes()
    workbench_auth.ensure_remote_runtime_access(path, tmp_path)
    assert path.read_bytes() == before
    assert load_shared_token(tmp_path) == "environment-fixture"


@pytest.mark.asyncio
async def test_optional_lifecycle_recovers_without_disrupting_local_services(
    provisioned_instance, tmp_path, monkeypatch,
):
    from types import SimpleNamespace

    from orchestrator import remote_lifecycle
    from orchestrator.startup_manager import StartupManager

    config, path = provisioned_instance
    global_config, _, _ = ConfigManager(config, path, bridge_home=tmp_path).load(
        bootstrap_local_api_credential=True,
    )
    kernel = SimpleNamespace(global_config=global_config,
                             startup_status={"services_ready": True})
    manager = StartupManager(kernel, console_handler=None)
    starts = []

    async def start(root, **_kwargs):
        starts.append(load_shared_token(root))
        return {"ok": True, "action": "already_running",
                "settings": SimpleNamespace(port=8766)}

    monkeypatch.setattr(remote_lifecycle, "ensure_remote_started", start)
    monkeypatch.setattr(workbench_auth, "resolve_remote_runtime_sids",
                        lambda _root: (_ for _ in ()).throw(RuntimeError("unavailable")))
    await manager._ensure_remote_lifecycle()
    assert kernel.startup_status["services_ready"] is True
    assert kernel.startup_status["degraded"] is True
    assert not starts

    monkeypatch.setattr(workbench_auth, "resolve_remote_runtime_sids", lambda _root: ())
    await manager._ensure_remote_lifecycle()
    assert kernel.startup_status["services_ready"] is True
    assert starts == [load_shared_token(tmp_path)]
    assert starts[0]



@pytest.mark.asyncio
async def test_first_empty_personal_install_starts_local_api_while_remote_identity_is_unavailable(
    tmp_path, monkeypatch,
):
    from aiohttp.test_utils import TestClient, TestServer
    from orchestrator.workbench_api import WorkbenchApiServer

    monkeypatch.delenv("HASHI_REMOTE_SHARED_TOKEN", raising=False)
    monkeypatch.delenv("HASHI_REMOTE_ROOT", raising=False)
    config, path = _write_base_files(tmp_path, _minimal_flex_agent())
    path.write_text("{}", encoding="utf-8")

    def unavailable(_root):
        raise RuntimeError("synthetic Remote principal unavailable")

    monkeypatch.setattr(workbench_auth, "resolve_remote_runtime_sids", unavailable)
    manager = ConfigManager(config, path, bridge_home=tmp_path)
    global_config, _, credentials = manager.load(bootstrap_local_api_credential=True)
    local_key = credentials["workbench_admin_token"]
    assert len(local_key) >= 48
    assert "hashi_remote_shared_token" not in credentials
    assert manager.load()[2]["workbench_admin_token"] == local_key

    server = WorkbenchApiServer(config, global_config, secrets=credentials,
                                reconcile_session_runs=False)
    async with TestClient(TestServer(server.app)) as client:
        url = "/api/v1/workbench-local-paths/verify"
        assert (await client.post(url, json={"attachments": []})).status == 401
        response = await client.post(
            url, json={"attachments": []}, headers={"Authorization": f"Bearer {local_key}"},
        )
        # WSL supports this empty verification request; native Windows reports
        # its platform-specific reselect result after successful authentication.
        body = await response.json()
        assert response.status in {200, 400}
        if response.status == 400:
            assert body["error_code"] == "attachment_reselect_required"
        else:
            assert body["ok"] is True

    before = path.read_bytes()
    with pytest.raises(RuntimeError, match="principal unavailable"):
        workbench_auth.ensure_remote_runtime_access(path, tmp_path)
    assert path.read_bytes() == before

    monkeypatch.setattr(workbench_auth, "resolve_remote_runtime_sids", lambda _root: ())
    workbench_auth.ensure_remote_runtime_access(path, tmp_path)
    completed = read_config_json(path)
    assert completed["workbench_admin_token"] == local_key
    assert len(completed["hashi_remote_shared_token"]) >= 48
    assert load_shared_token(tmp_path) == completed["hashi_remote_shared_token"]


def test_deferred_empty_bootstrap_cannot_discard_existing_secret_content(tmp_path, monkeypatch):
    monkeypatch.delenv("HASHI_REMOTE_SHARED_TOKEN", raising=False)
    path = tmp_path / "secrets.json"
    path.write_text('{"provider_key":"must-survive"}', encoding="utf-8")
    before = path.read_bytes()

    def unavailable(_root):
        raise RuntimeError("synthetic Remote principal unavailable")

    monkeypatch.setattr(workbench_auth, "resolve_remote_runtime_sids", unavailable)
    with pytest.raises(RuntimeError, match="principal unavailable"):
        workbench_auth.ensure_local_api_credential(
            read_config_json(path), profile="personal", defer_missing_remote=True,
        )
    assert path.read_bytes() == before



def test_empty_bootstrap_respects_concurrent_existing_secret_update(tmp_path, monkeypatch):
    monkeypatch.delenv("HASHI_REMOTE_SHARED_TOKEN", raising=False)
    path = tmp_path / "secrets.json"
    path.write_text("{}", encoding="utf-8")
    observed_empty = read_config_json(path)
    path.write_text('{"provider_key":"concurrent-winner"}', encoding="utf-8")
    with pytest.raises(ConfigConflictError):
        workbench_auth.ensure_local_api_credential(
            observed_empty, profile="personal", defer_missing_remote=True,
        )
    assert read_config_json(path) == {"provider_key": "concurrent-winner"}


def test_empty_bootstrap_does_not_copy_network_credential_from_environment(tmp_path, monkeypatch):
    path = tmp_path / "secrets.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("HASHI_REMOTE_SHARED_TOKEN", "synthetic-env-token")
    monkeypatch.setattr(
        workbench_auth, "resolve_remote_runtime_sids",
        lambda _root: pytest.fail("optional Remote principal was queried"),
    )
    result = workbench_auth.ensure_local_api_credential(
        read_config_json(path), profile="personal", defer_missing_remote=True,
    )
    assert result["workbench_admin_token"]
    assert "hashi_remote_shared_token" not in result
    assert "synthetic-env-token" not in path.read_text(encoding="utf-8")



@pytest.mark.asyncio
@pytest.mark.parametrize("initial", [
    {},
    {"workbench_admin_token": "existing-local", "provider_key": "unrelated"},
])
async def test_separate_remote_root_never_blocks_local_api_bootstrap_for_supported_cases(
    tmp_path, monkeypatch, initial,
):
    from aiohttp.test_utils import TestClient, TestServer
    from orchestrator.workbench_api import WorkbenchApiServer

    monkeypatch.delenv("HASHI_REMOTE_SHARED_TOKEN", raising=False)
    config, path = _write_base_files(tmp_path, _minimal_flex_agent())
    path.write_text(json.dumps(initial), encoding="utf-8")
    separate = tmp_path / "remote-instance"
    separate.mkdir()
    remote_path = separate / "secrets.json"
    remote_path.write_text('{"hashi_remote_shared_token":"separate-fixture"}', encoding="utf-8")
    remote_before = remote_path.read_bytes()
    monkeypatch.setenv("HASHI_REMOTE_ROOT", str(separate))
    monkeypatch.setattr(
        workbench_auth, "resolve_remote_runtime_sids",
        lambda _root: pytest.fail("local startup queried optional Remote principal"),
    )

    global_config, _agents, local = ConfigManager(config, path, bridge_home=tmp_path).load(
        bootstrap_local_api_credential=True,
    )
    assert local["workbench_admin_token"]
    if initial:
        assert local["workbench_admin_token"] == "existing-local"
        assert local["provider_key"] == "unrelated"
    assert "hashi_remote_shared_token" not in local
    assert remote_path.read_bytes() == remote_before
    assert "separate-fixture" not in path.read_text(encoding="utf-8")

    server = WorkbenchApiServer(config, global_config, secrets=local,
                                reconcile_session_runs=False)
    async with TestClient(TestServer(server.app)) as client:
        url = "/api/v1/workbench-local-paths/verify"
        assert (await client.post(url, json={"attachments": []})).status == 401
        response = await client.post(
            url, json={"attachments": []},
            headers={"Authorization": f"Bearer {local['workbench_admin_token']}"},
        )
        assert response.status in {200, 400}
        body = await response.json()
        if response.status == 400:
            assert body["error_code"] == "attachment_reselect_required"
        else:
            assert body["ok"] is True

    local_before = path.read_bytes()
    monkeypatch.setattr(workbench_auth, "resolve_remote_runtime_sids", lambda _root: ())
    workbench_auth.ensure_remote_runtime_access(path, separate)
    assert path.read_bytes() == local_before
    assert remote_path.read_bytes() == remote_before
