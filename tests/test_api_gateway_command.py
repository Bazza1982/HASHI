from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.api_gateway import APIGatewayServer
from orchestrator.api_gateway_config import (
    config_path_for,
    load_api_gateway_config,
    save_api_gateway_config,
)
from orchestrator.commands import api as api_command_module
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


def test_api_command_module_is_registered():
    commands = {command.name for command in load_runtime_commands()}
    callbacks = [callback.pattern for callback in load_runtime_callbacks()]

    assert "api" in commands
    assert r"^api:" in callbacks


class _FakeRequest:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


class _FakePool:
    def __init__(self):
        self.models = []

    async def get(self, engine, model):
        self.models.append((engine, model))
        return SimpleNamespace(generate_response=self._generate_response)

    async def update_model(self, engine, model):
        self.models.append(("update", engine, model))

    async def _generate_response(self, prompt, request_id, is_retry=False, silent=True, on_stream_event=None):
        return SimpleNamespace(is_success=True, text="ok", error=None)


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
    assert fake_pool.models[0] == ("codex-cli", "gpt-5.5")


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
    assert body["default_model"] == "gpt-5.5"
    assert body["port"] == 18801


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
