from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import ClientSession
from aiohttp.test_utils import TestClient, TestServer

from orchestrator.api_gateway import APIGatewayServer
from orchestrator.api_gateway_config import (
    available_api_models,
    configured_gateway_model_overrides,
    config_path_for,
    legacy_state_path_for,
    load_api_gateway_config,
    save_api_gateway_config,
)
from orchestrator.service_manager import ServiceManager
from orchestrator.commands import api as api_command_module
from orchestrator.commands import api_restart
from orchestrator.command_registry import load_runtime_callbacks, load_runtime_commands


def _global_config(tmp_path: Path, **kwargs):
    values = {
        "bridge_home": tmp_path,
        "project_root": tmp_path,
        "api_host": "127.0.0.1",
        "api_gateway_port": 18801,
    }
    values.update(kwargs)
    return SimpleNamespace(**values)


def test_api_gateway_config_defaults_and_persistence(tmp_path):
    cfg = _global_config(tmp_path)

    loaded = load_api_gateway_config(cfg)

    assert loaded["enabled"] is False
    assert loaded["default_model"] == "gpt-5.4"

    saved = save_api_gateway_config(
        cfg,
        enabled=True,
        default_model="gpt-5.5",
        updated_by="telegram:123",
    )

    assert saved["enabled"] is True
    assert saved["default_model"] == "gpt-5.5"
    assert json.loads(config_path_for(cfg).read_text(encoding="utf-8"))["updated_by"] == "telegram:123"


def test_api_gateway_config_migrates_legacy_state_once(tmp_path):
    cfg = _global_config(tmp_path)
    legacy_path = legacy_state_path_for(cfg)
    legacy_path.write_text(
        json.dumps({"enabled": True, "default_model": "grok-4.5"}),
        encoding="utf-8",
    )

    loaded = load_api_gateway_config(cfg)

    assert loaded["enabled"] is True
    assert loaded["default_model"] == "grok-4.5"
    assert loaded["updated_by"] == "legacy-state-migration"
    assert config_path_for(cfg).exists()
    assert legacy_path.exists()

    legacy_path.write_text(
        json.dumps({"enabled": False, "default_model": "gpt-5.5"}),
        encoding="utf-8",
    )
    loaded_again = load_api_gateway_config(cfg)

    assert loaded_again["enabled"] is True
    assert loaded_again["default_model"] == "grok-4.5"


def test_api_gateway_default_model_list_includes_grok_models():
    models = available_api_models()
    assert "grok-4.5" in models
    assert "grok-4.3" in models
    assert "grok-build-0.1" in models
    assert "grok-imagine-image" in models
    assert "grok-imagine-video" in models


def test_api_gateway_model_list_includes_smoke_tested_gpt56_variants():
    models = available_api_models()
    assert {"gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"}.issubset(models)


def test_api_command_module_is_registered():
    commands = {command.name: command for command in load_runtime_commands()}
    callbacks = [callback.pattern for callback in load_runtime_callbacks()]

    assert "api" in commands
    assert commands["api"].callback is api_restart.api_command
    assert r"^api:" in callbacks


class _FakeRequest:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


class _FakePool:
    def __init__(self):
        self.models = []
        self.reasoning_efforts = []

    async def get(self, engine, model):
        self.models.append((engine, model))
        return SimpleNamespace(generate_response=self._generate_response)

    async def update_model(self, engine, model):
        self.models.append(("update", engine, model))

    async def shutdown(self):
        pass

    async def _generate_response(
        self,
        prompt,
        request_id,
        is_retry=False,
        silent=True,
        on_stream_event=None,
        reasoning_effort=None,
    ):
        self.reasoning_efforts.append(reasoning_effort)
        return SimpleNamespace(
            is_success=True,
            text="ok",
            error=None,
            usage=SimpleNamespace(
                input_tokens=17,
                output_tokens=3,
                thinking_tokens=2,
            ),
        )


class _FakeQuery:
    def __init__(self, data: str, user_id: int = 123):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.answers = []
        self.edits = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text, **kwargs):
        self.edits.append((text, kwargs))


class _FakeRuntime:
    def __init__(self, global_config, *, authorized: bool = True):
        self.global_config = global_config
        self._authorized = authorized

    def _is_authorized_user(self, user_id):
        return self._authorized


def _write_instance_models(cfg, *, model="gpt-6-astra"):
    cfg.bridge_home.mkdir(parents=True, exist_ok=True)
    (cfg.bridge_home / "agents.json").write_text(json.dumps({"agents": [
        {"allowed_backends": [{"engine": "codex-cli", "models": [model],
          "model_efforts": {model: ["medium", "high", "max"]}}]},
        {"is_active": False, "allowed_backends": [
            {"engine": "codex-cli", "models": ["inactive-model"]}]},
    ]}), encoding="utf-8-sig")


@pytest.mark.asyncio
async def test_instance_models_survive_shared_service_restart_and_route_http(tmp_path):
    cfg = _global_config(tmp_path / "instance", project_root=tmp_path / "source",
                         api_gateway_port=0, workspaces_root=tmp_path / "workspaces")
    _write_instance_models(cfg)
    kernel = SimpleNamespace(paths=cfg, global_cfg=cfg, secrets={},
                             enable_api_gateway=True, api_gateway=None)
    manager = ServiceManager(kernel)
    assert "gpt-6-astra" in manager.api_gateway_state_snapshot()["available_models"]
    assert "inactive-model" not in available_api_models(cfg)
    assert manager.set_api_gateway_default_model("gpt-6-astra")[0]
    for _ in range(2):
        await manager.start_api_gateway(cfg, {})
        server = kernel.api_gateway
        assert server is not None
        try:
            assert server.default_model == "gpt-6-astra"
            server._engine_status["codex-cli"] = {"available": True}
            server._pool = pool = _FakePool()
            async with ClientSession(base_url=f"http://127.0.0.1:{server.bound_port}") as client:
                response = await client.get("/v1/models")
                assert response.status == 200
                assert "gpt-6-astra" in [m["id"] for m in (await response.json())["data"]]
                response = await client.post("/v1/chat/completions", json={
                    "messages": [{"role": "user", "content": "hello"}],
                    "reasoning_effort": "max",
                })
                assert response.status == 200, await response.text()
                assert (await response.json())["model"] == "gpt-6-astra"
                assert pool.models == [("codex-cli", "gpt-6-astra")]
                assert pool.reasoning_efforts == ["max"]
                response = await client.post("/v1/chat/completions", json={
                    "messages": [{"role": "user", "content": "hello"}],
                    "reasoning_effort": "ultra",
                })
                assert response.status == 400
                assert pool.reasoning_efforts == ["max"]
        finally:
            assert await manager.stop_api_gateway()
    assert load_api_gateway_config(cfg)["default_model"] == "gpt-6-astra"
    assert "gpt-6-astra" not in available_api_models(_global_config(tmp_path / "other"))
    assert not cfg.project_root.exists()


@pytest.mark.asyncio
async def test_canonical_api_menu_uses_instance_model_catalog(tmp_path):
    cfg = _global_config(tmp_path)
    _write_instance_models(cfg)
    manager = ServiceManager(SimpleNamespace(paths=cfg, global_cfg=cfg, api_gateway=None))
    runtime = _FakeRuntime(cfg)
    runtime.orchestrator = SimpleNamespace(service_manager=manager)
    query = _FakeQuery("apigw:menu:model")
    await api_restart.api_callback(runtime, SimpleNamespace(callback_query=query), SimpleNamespace())
    markup = query.edits[-1][1]["reply_markup"]
    assert "apigw:model:gpt-6-astra" in [
        button.callback_data for row in markup.inline_keyboard for button in row
    ]
    query = _FakeQuery("apigw:model:gpt-6-astra")
    await api_restart.api_callback(runtime, SimpleNamespace(callback_query=query), SimpleNamespace())
    assert load_api_gateway_config(cfg)["default_model"] == "gpt-6-astra"


@pytest.mark.parametrize("conflict", ["engine", "effort"])
def test_configured_gateway_models_reject_ambiguous_agents(conflict):
    rows = [{"allowed_backends": [{"engine": engine, "models": ["gpt-6-astra"],
             "model_efforts": {"gpt-6-astra": efforts}}]}
            for engine, efforts in [
                ("codex-cli", ["high", "max"]),
                ("claude-cli" if conflict == "engine" else "codex-cli", ["high"]),
            ]]
    with pytest.raises(ValueError, match="maps to both|conflicting model_efforts"):
        configured_gateway_model_overrides(rows)


def test_newly_configured_model_cannot_partially_change_running_gateway_default(tmp_path):
    cfg = _global_config(tmp_path)
    server = APIGatewayServer(cfg, {}, tmp_path / "workspaces")
    before = load_api_gateway_config(cfg)
    _write_instance_models(cfg)
    manager = ServiceManager(SimpleNamespace(paths=cfg, global_cfg=cfg, api_gateway=server))
    success, message = manager.set_api_gateway_default_model("gpt-6-astra")
    assert success is False
    assert "restart" in message.lower()
    assert load_api_gateway_config(cfg) == before
    assert server.default_model == before["default_model"]


@pytest.mark.asyncio
async def test_api_gateway_uses_default_model_when_request_omits_model(tmp_path):
    global_config = _global_config(tmp_path)
    save_api_gateway_config(global_config, enabled=True, default_model="gpt-5.5", updated_by="test")
    server = APIGatewayServer(global_config, secrets={}, workspace_root=tmp_path / "workspaces")
    fake_pool = _FakePool()
    server._pool = fake_pool

    response = await server.handle_chat_completions(
        _FakeRequest({"messages": [{"role": "user", "content": "hello"}]})
    )

    assert response.status == 200
    body = json.loads(response.text)
    assert body["model"] == "gpt-5.5"
    assert body["usage"] == {
        "prompt_tokens": 17,
        "completion_tokens": 3,
        "total_tokens": 20,
        "completion_tokens_details": {"reasoning_tokens": 2},
    }
    assert fake_pool.models[0] == ("codex-cli", "gpt-5.5")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "reasoning_effort"),
    [
        ("gpt-5.6-luna", "high"),
        ("gpt-5.6-sol", "max"),
        ("gpt-5.6-luna", "none"),
    ],
)
async def test_api_gateway_passes_valid_reasoning_effort_to_codex_request(
    tmp_path, model, reasoning_effort
):
    server = APIGatewayServer(
        _global_config(tmp_path),
        secrets={},
        workspace_root=tmp_path / "workspaces",
    )
    fake_pool = _FakePool()
    server._pool = fake_pool

    response = await server.handle_chat_completions(
        _FakeRequest(
            {
                "model": model,
                "messages": [{"role": "user", "content": "hello"}],
                "reasoning_effort": reasoning_effort,
            }
        )
    )

    assert response.status == 200
    assert fake_pool.reasoning_efforts == [reasoning_effort]


@pytest.mark.asyncio
async def test_api_gateway_routes_instance_configured_model_with_configured_effort(
    tmp_path,
):
    server = APIGatewayServer(
        _global_config(tmp_path),
        secrets={},
        workspace_root=tmp_path / "workspaces",
        configured_model_engines={"gpt-6-astra": "codex-cli"},
        configured_model_efforts={
            "gpt-6-astra": ["low", "medium", "high", "xhigh", "max"]
        },
    )
    server._engine_status["codex-cli"] = {"available": True, "reason": "test"}
    fake_pool = _FakePool()
    server._pool = fake_pool

    models_response = await server.handle_models(_FakeRequest({}))
    model_rows = json.loads(models_response.text)["data"]
    astra = next(row for row in model_rows if row["id"] == "gpt-6-astra")
    assert astra["owned_by"] == "codex"

    response = await server.handle_chat_completions(
        _FakeRequest(
            {
                "model": "gpt-6-astra",
                "messages": [{"role": "user", "content": "hello"}],
                "reasoning_effort": "max",
            }
        )
    )

    assert response.status == 200
    assert fake_pool.models[0] == ("codex-cli", "gpt-6-astra")
    assert fake_pool.reasoning_efforts == ["max"]


def test_instance_configured_model_does_not_mutate_process_catalogue(tmp_path):
    configured = APIGatewayServer(
        _global_config(tmp_path),
        secrets={},
        workspace_root=tmp_path / "configured",
        configured_model_engines={"gpt-6-astra": "codex-cli"},
    )
    ordinary = APIGatewayServer(
        _global_config(tmp_path),
        secrets={},
        workspace_root=tmp_path / "ordinary",
    )

    assert "gpt-6-astra" in configured.configured_models()
    assert "gpt-6-astra" not in ordinary.configured_models()


@pytest.mark.asyncio
async def test_api_gateway_streams_backend_usage_with_request_effort(tmp_path):
    server = APIGatewayServer(
        _global_config(tmp_path),
        secrets={},
        workspace_root=tmp_path / "workspaces",
    )
    fake_pool = _FakePool()
    server._pool = fake_pool

    async with TestClient(TestServer(server.app)) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-5.6-sol",
                "messages": [{"role": "user", "content": "hello"}],
                "reasoning_effort": "max",
                "stream": True,
            },
        )
        raw = await response.text()

    events = [
        json.loads(line.removeprefix("data: "))
        for line in raw.splitlines()
        if line.startswith("data: {")
    ]
    assert response.status == 200
    assert events[-1]["usage"] == {
        "prompt_tokens": 17,
        "completion_tokens": 3,
        "total_tokens": 20,
        "completion_tokens_details": {"reasoning_tokens": 2},
    }
    assert raw.rstrip().endswith("data: [DONE]")
    assert fake_pool.reasoning_efforts == ["max"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "reasoning_effort"),
    [
        ("gpt-5.6-luna", "minimal"),
        ("gpt-5.6-luna", "ultra"),
        ("gpt-5.6-luna", 5),
        ("gpt-5.6-luna", ""),
        ("gpt-5.6-terra", "max"),
    ],
)
async def test_api_gateway_rejects_invalid_reasoning_effort_before_adapter_init(
    tmp_path, model, reasoning_effort
):
    server = APIGatewayServer(
        _global_config(tmp_path),
        secrets={},
        workspace_root=tmp_path / "workspaces",
    )
    fake_pool = _FakePool()
    server._pool = fake_pool

    response = await server.handle_chat_completions(
        _FakeRequest(
            {
                "model": model,
                "messages": [{"role": "user", "content": "hello"}],
                "reasoning_effort": reasoning_effort,
            }
        )
    )
    payload = json.loads(response.text)

    assert response.status == 400
    assert payload["error"]["code"] == "invalid_reasoning_effort"
    assert payload["error"]["param"] == "reasoning_effort"
    assert fake_pool.models == []


@pytest.mark.asyncio
async def test_api_gateway_health_reports_default_model(tmp_path):
    global_config = _global_config(tmp_path)
    save_api_gateway_config(global_config, enabled=True, default_model="gpt-5.5", updated_by="test")
    server = APIGatewayServer(global_config, secrets={}, workspace_root=tmp_path / "workspaces")
    server.bind_host = "127.0.0.1"

    response = await server.handle_health(_FakeRequest({}))

    assert response.status == 200
    body = json.loads(response.text)
    assert body["enabled"] is True
    assert body["running"] is False
    assert body["configured_enabled"] is True
    assert body["default_model"] == "gpt-5.5"
    assert body["port"] == 18801


@pytest.mark.asyncio
async def test_api_gateway_health_reports_live_runtime_separately_from_config(tmp_path):
    global_config = _global_config(tmp_path, api_gateway_port=0)
    server = APIGatewayServer(global_config, secrets={}, workspace_root=tmp_path / "workspaces")

    await server.start()
    try:
        response = await server.handle_health(_FakeRequest({}))
        body = json.loads(response.text)
        assert body["enabled"] is True
        assert body["running"] is True
        assert body["configured_enabled"] is False
    finally:
        await server.stop()

    assert server.enabled is False


@pytest.mark.asyncio
async def test_api_callback_answers_unauthorized_queries(tmp_path):
    query = _FakeQuery("api:status")
    update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=123))
    runtime = _FakeRuntime(_global_config(tmp_path), authorized=False)

    await api_command_module.api_callback(runtime, update, SimpleNamespace())

    assert query.answers == [(None, False)]
    assert query.edits == []


@pytest.mark.asyncio
async def test_api_callback_rejects_crafted_unknown_model(tmp_path):
    query = _FakeQuery("api:model:not-a-model")
    update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=123))
    runtime = _FakeRuntime(_global_config(tmp_path), authorized=True)

    await api_command_module.api_callback(runtime, update, SimpleNamespace())

    assert query.answers == [("Unknown API model: not-a-model", True)]
    assert query.edits == []
