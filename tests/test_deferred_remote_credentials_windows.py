"""Windows ACL checks for deferred optional Remote credential publication."""
from __future__ import annotations

import json
import os
import subprocess

import pytest

from orchestrator import workbench_auth
from orchestrator.config_json import read_config_json
from remote.security.shared_token import load_shared_token
from tools.private_files import protect_private_file

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows ACL contract")


def test_deferred_remote_key_publishes_only_to_selected_consumer(tmp_path, monkeypatch):
    monkeypatch.delenv("HASHI_REMOTE_SHARED_TOKEN", raising=False)
    path = tmp_path / "secrets.json"
    path.write_text(json.dumps({
        "workbench_admin_token": "local-fixture",
        "provider_key": "unrelated-fixture",
    }), encoding="utf-8")
    protect_private_file(path)
    monkeypatch.setattr(workbench_auth, "resolve_remote_runtime_sids",
                        lambda root: ("S-1-5-19",) if root == tmp_path else pytest.fail("wrong root"))

    workbench_auth.ensure_remote_runtime_access(path, tmp_path)

    updated = read_config_json(path)
    assert updated["workbench_admin_token"] == "local-fixture"
    assert updated["provider_key"] == "unrelated-fixture"
    assert len(updated["hashi_remote_shared_token"]) >= 48
    assert load_shared_token(tmp_path) == updated["hashi_remote_shared_token"]
    acl = subprocess.run(["icacls.exe", str(path)], capture_output=True,
                         text=True, check=True).stdout
    assert "NT AUTHORITY\\LOCAL SERVICE:(F)" in acl
    assert "Everyone:(F)" not in acl
    before = path.read_bytes()
    workbench_auth.ensure_remote_runtime_access(path, tmp_path)
    assert path.read_bytes() == before

@pytest.mark.asyncio
async def test_first_local_api_bootstrap_and_backend_work_when_remote_disabled(tmp_path, monkeypatch):
    """A disabled optional Remote must not require its Windows task principal."""
    from aiohttp.test_utils import TestClient, TestServer

    from orchestrator import workbench_api
    from orchestrator.config import ConfigManager
    from tests.test_config import _minimal_flex_agent, _write_base_files

    monkeypatch.delenv("HASHI_REMOTE_SHARED_TOKEN", raising=False)
    monkeypatch.delenv("HASHI_REMOTE_ROOT", raising=False)
    config, path = _write_base_files(tmp_path, _minimal_flex_agent())
    path.write_text(json.dumps({
        "hashi_remote_shared_token": "network-fixture",
        "provider_key": "provider-fixture",
    }), encoding="utf-8")
    remote_config = tmp_path / "remote" / "config.yaml"
    remote_config.parent.mkdir()
    remote_config.write_text(
        "lifecycle:\n  remote_enabled: false\n  remote_supervised: false\n",
        encoding="utf-8",
    )

    def unexpected_query(_root):
        pytest.fail("disabled Remote queried Task Scheduler")

    monkeypatch.setattr(workbench_auth, "_windows_remote_runtime_sids", unexpected_query)
    # Also catches the original Backend API's direct private-function import.
    monkeypatch.setattr(workbench_api, "_windows_remote_runtime_sids", unexpected_query, raising=False)
    manager = ConfigManager(config, path, bridge_home=tmp_path)
    global_config, _, credentials = manager.load(bootstrap_local_api_credential=True)
    key = credentials["workbench_admin_token"]
    assert len(key) >= 48
    assert credentials["hashi_remote_shared_token"] == "network-fixture"
    assert credentials["provider_key"] == "provider-fixture"
    assert manager.load()[2]["workbench_admin_token"] == key

    server = workbench_api.WorkbenchApiServer(
        config, global_config, secrets=credentials, reconcile_session_runs=False,
    )
    async with TestClient(TestServer(server.app)) as client:
        url = "/api/v1/workbench-local-paths/verify"
        assert (await client.post(url, json={"attachments": []})).status == 401
        authenticated = await client.post(
            url, json={"attachments": []}, headers={"Authorization": f"Bearer {key}"},
        )
        # The request passed authentication. Path verification itself is WSL-only.
        assert authenticated.status == 400
        assert (await authenticated.json())["error_code"] == "attachment_reselect_required"


# HASHI-Fingerprint: hfp_b4b95e064e7b463c89fdc189ba6513ff



def test_empty_first_bootstrap_defers_then_grants_only_exact_remote_consumer(tmp_path, monkeypatch):
    monkeypatch.delenv("HASHI_REMOTE_SHARED_TOKEN", raising=False)
    path = tmp_path / "secrets.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        workbench_auth, "resolve_remote_runtime_sids",
        lambda _root: pytest.fail("optional Remote principal was queried during local bootstrap"),
    )
    initial = workbench_auth.ensure_local_api_credential(
        read_config_json(path), profile="personal", defer_missing_remote=True,
    )
    local_key = initial["workbench_admin_token"]
    before = subprocess.run(["icacls.exe", str(path)], capture_output=True,
                            text=True, check=True).stdout
    assert "Everyone:(F)" not in before and "BUILTIN\\Users:(F)" not in before

    monkeypatch.setattr(
        workbench_auth, "resolve_remote_runtime_sids",
        lambda root: ("S-1-5-19",) if root == tmp_path else pytest.fail("wrong root"),
    )
    workbench_auth.ensure_remote_runtime_access(path, tmp_path)
    final = read_config_json(path)
    after = subprocess.run(["icacls.exe", str(path)], capture_output=True,
                           text=True, check=True).stdout
    assert final["workbench_admin_token"] == local_key
    assert len(final["hashi_remote_shared_token"]) >= 48
    assert "NT AUTHORITY\\LOCAL SERVICE:(F)" in after
    assert "Everyone:(F)" not in after and "BUILTIN\\Users:(F)" not in after
