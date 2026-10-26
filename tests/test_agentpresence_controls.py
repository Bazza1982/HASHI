from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from aiohttp.test_utils import TestClient, TestServer
from orchestrator.config_json import ConfigConflictError
from orchestrator import agent_presence_notify, agent_presence_settings
from orchestrator.workbench_api import WorkbenchApiServer


def server_at(tmp_path):
    path = tmp_path / "agents.json"
    path.write_text(json.dumps({"global": {"instance_id": "TEST"},
        "agents": [{"name": "agent1"}, {"name": "agent2"}]}), encoding="utf-8")
    config = SimpleNamespace(bridge_home=tmp_path, project_root=tmp_path, instance_id="TEST",
        authorized_id=7, workbench_port=0, api_gateway_port=0, deployment_profile="personal")
    runtime = SimpleNamespace(name="agent1", backend_ready=True,
        get_runtime_metadata=lambda: {"id": "agent1", "display_name": "One", "online": True})
    other = SimpleNamespace(name="agent2", backend_ready=True,
        get_runtime_metadata=lambda: {"id": "agent2", "display_name": "Two", "online": True})
    agent_presence_settings.set_default_agent(tmp_path, "agent1")
    api = WorkbenchApiServer(config_path=path, runtimes=[runtime, other],
        global_config=config, reconcile_session_runs=False)
    return api


@pytest.mark.asyncio
async def test_controls_reject_untrusted_and_wrong_instance_before_read(tmp_path, monkeypatch):
    api = server_at(tmp_path)
    monkeypatch.setattr(agent_presence_notify, "token_matches", lambda value: value == "secret")
    async with TestClient(TestServer(api.app)) as client:
        response = await client.get("/api/agentpresence/controls?instance_id=TEST")
        assert response.status == 401
        response = await client.get("/api/agentpresence/controls?instance_id=OTHER",
            headers={"X-AP-Token": "secret"})
        assert response.status == 409


def test_default_agent_stale_revision_never_overwrites(tmp_path):
    first = agent_presence_settings.set_default_agent(tmp_path, "agent1")
    agent_presence_settings.set_default_agent(tmp_path, "agent2")
    with pytest.raises(ConfigConflictError):
        agent_presence_settings.set_default_agent(tmp_path, "agent1", expected_revision=first["revision"])
    assert agent_presence_settings.get_default_agent(tmp_path) == "agent2"

@pytest.mark.asyncio
async def test_route_change_persists_once_and_rejects_stale_clients(tmp_path, monkeypatch):
    from orchestrator.agent_presence_control_api import AgentPresenceControls
    monkeypatch.setattr(agent_presence_notify, "token_matches", lambda value: value == "secret")
    # Config mutation uses the real route owner; arranging the unrelated Worker projection avoids inference.
    async def worker(self, name, body):
        return {"ok": True, "agent_id": name, "configuration": {"engine": "codex-cli"},
                "configuration_version": "version", "online": True}
    monkeypatch.setattr(AgentPresenceControls, "worker", worker)
    api = server_at(tmp_path)
    body = {"instance_id": "TEST", "agent_id": "agent1", "route_revision": 1,
        "configuration_version": "version", "setting": "agent", "value": "agent2", "request_id": "change-1"}
    async with TestClient(TestServer(api.app)) as client:
        headers = {"X-AP-Token": "secret"}
        response = await client.get("/api/agentpresence/controls?instance_id=TEST", headers=headers)
        assert (await response.json())["agent_id"] == "agent1"
        for _ in range(2):
            response = await client.post("/api/agentpresence/controls", json=body, headers=headers)
            assert response.status == 200
            assert (await response.json())["persisted"] is True
        assert agent_presence_settings.load_settings(tmp_path) == {
            "schema_version": 1, "revision": 2, "default_agent": "agent2"}
        response = await client.post("/api/agentpresence/controls",
            json={**body, "request_id": "stale-change"}, headers=headers)
        assert response.status == 409
        assert agent_presence_settings.get_default_agent(tmp_path) == "agent2"
        response = await client.post("/api/agentpresence/controls",
            json={**body, "value": "agent1"}, headers=headers)
        assert response.status == 409


@pytest.mark.asyncio
async def test_reserved_control_transport_never_runs_from_chat():
    from orchestrator.agent_presence_controls import try_dispatch, encode
    runtime = SimpleNamespace(is_function_worker_proxy=True, execute_slash_command=AsyncMock())
    response = await try_dispatch(runtime, encode({"op": "read", "agent_id": "any"}),
                                  source_channel="api_chat")
    assert response["http_status"] == 403
    runtime.execute_slash_command.assert_not_awaited()


def test_strict_settings_save_surfaces_actual_disk_failure(tmp_path, monkeypatch):
    from tests.test_flexible_backend_state import _make_manager
    manager = _make_manager(tmp_path / "agent")
    manager.persist_state(active_model="gpt-5.4")
    before = manager.state_store.read_strict()
    def fail(_state):
        raise OSError("disk unavailable")
    monkeypatch.setattr(manager.state_store, "replace", fail)
    with pytest.raises(OSError, match="disk unavailable"):
        with manager.strict_settings_save():
            manager.persist_state(active_model="gpt-5.6-sol")
    assert manager.state_store.read_strict() == before


@pytest.mark.asyncio
async def test_busy_backend_and_stale_configuration_never_execute(monkeypatch):
    from orchestrator.agent_presence_controls import apply_setting, configuration_version, ControlError
    from orchestrator import admin_local_testing
    execute = AsyncMock()
    monkeypatch.setattr(admin_local_testing, "execute_local_command", execute)
    runtime = SimpleNamespace(name="one", config=SimpleNamespace(active_backend="codex-cli"),
        backend_manager=SimpleNamespace(agent_mode="fixed"), backend_ready=True,
        get_current_model=lambda: "gpt-5.4", get_current_provider=lambda: None,
        _get_current_effort=lambda: "high", _is_command_allowed=lambda c: True, _backend_busy=lambda: True)
    body = {"setting": "backend", "value": "her-v3", "configuration_version": "stale"}
    with pytest.raises(ControlError, match="control_configuration_changed"):
        await apply_setting(runtime, body)
    from orchestrator.agent_presence_controls import configuration
    body["configuration_version"] = configuration_version(configuration(runtime))
    with pytest.raises(ControlError, match="backend_busy"):
        await apply_setting(runtime, body)
    execute.assert_not_awaited()

@pytest.mark.asyncio
async def test_effort_control_executes_real_command_and_survives_reload(tmp_path):
    from tests.test_flexible_backend_state import _make_manager
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
    from orchestrator.agent_presence_controls import apply_setting, configuration, configuration_version
    manager = _make_manager(tmp_path / "agent")
    manager.config.allowed_backends[0]["effort"] = "high"
    manager.current_backend = SimpleNamespace(config=SimpleNamespace(model="gpt-5.4"), effort="high")
    manager.persist_state(active_model="gpt-5.4")
    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime.name = manager.config.name
    runtime.workspace_dir = manager.config.workspace_dir
    runtime.global_config = manager.global_config
    runtime.config = manager.config
    runtime.backend_manager = manager
    runtime.backend_ready = True
    runtime.get_current_model = lambda: manager.current_backend.config.model
    runtime.get_current_provider = lambda: None
    runtime._is_authorized_user = lambda _: True
    runtime._is_command_allowed = lambda _: True
    runtime._backend_busy = lambda: False
    runtime._reply_text = AsyncMock()
    result = await apply_setting(runtime, {"setting": "effort", "value": "low",
        "configuration_version": configuration_version(configuration(runtime))})
    assert result["persisted"] is True
    assert manager.current_backend.effort == "low"
    reloaded = _make_manager(manager.config.workspace_dir)
    assert reloaded.config.allowed_backends[0]["effort"] == "low"
