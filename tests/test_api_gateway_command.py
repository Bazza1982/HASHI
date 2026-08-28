from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from orchestrator.api_gateway import APIGatewayServer
from orchestrator.api_gateway_config import (
    available_api_models,
    config_path_for,
    legacy_state_path_for,
    load_api_gateway_config,
    save_api_gateway_config,
)
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
