from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.api_gateway import APIGatewayServer, _AdapterPool


class _FakeAdapter:
    async def generate_response(self, prompt, request_id, is_retry=False, silent=True, on_stream_event=None):
        return SimpleNamespace(is_success=True, text=f"reply:{prompt}", error=None)


class _FakePool:
    def __init__(self):
        self._adapters = {}
        self.calls = []

    async def get(self, engine, model):
        self.calls.append((engine, model))
        self._adapters[engine] = True
        return _FakeAdapter()

    async def update_model(self, engine, model):
        self.calls.append(("update", engine, model))

    async def shutdown(self):
        return None


class _FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


@pytest.mark.asyncio
async def test_api_gateway_uses_default_model_when_request_omits_model(tmp_path: Path):
    cfg = SimpleNamespace(api_gateway_port=18801, api_host="127.0.0.1", project_root=tmp_path)
    server = APIGatewayServer(cfg, secrets={}, workspace_root=tmp_path, default_model="gpt-5.5")
    server._pool = _FakePool()

    response = await server.handle_chat_completions(
        _FakeRequest({"messages": [{"role": "user", "content": "hello"}]})
    )

    payload = json.loads(response.text)
    assert payload["model"] == "gpt-5.5"
    assert server._pool.calls[0] == ("codex-cli", "gpt-5.5")


@pytest.mark.asyncio
async def test_api_gateway_health_reports_default_model(tmp_path: Path):
    cfg = SimpleNamespace(api_gateway_port=18801, api_host="127.0.0.1", project_root=tmp_path)
    server = APIGatewayServer(cfg, secrets={}, workspace_root=tmp_path, default_model="claude-sonnet-4-6")
    response = await server.handle_health(_FakeRequest({}))

    payload = json.loads(response.text)
    assert payload["default_model"] == "claude-sonnet-4-6"
    assert payload["port"] == 18801


@pytest.mark.asyncio
async def test_api_gateway_pool_isolates_models_and_request_locks(tmp_path: Path):
    cfg = SimpleNamespace(project_root=tmp_path)
    pool = _AdapterPool(cfg, secrets={}, workspace_root=tmp_path)
    created = []

    class _Adapter:
        def __init__(self, model):
            self.config = SimpleNamespace(model=model)
            self.shutdown_called = False

        async def shutdown(self):
            self.shutdown_called = True

    async def create(engine, model):
        adapter = _Adapter(model)
        created.append((engine, model, adapter))
        return adapter

    pool._create = create

    first = await pool.get("codex-cli", "gpt-one")
    same = await pool.get("codex-cli", "gpt-one")
    second = await pool.get("codex-cli", "gpt-two")

    assert first is same
    assert first is not second
    assert first.config.model == "gpt-one"
    assert second.config.model == "gpt-two"
    assert len(created) == 2
    assert pool.request_lock("codex-cli", "gpt-one") is pool.request_lock(
        "codex-cli", "gpt-one"
    )
    assert pool.request_lock("codex-cli", "gpt-one") is not pool.request_lock(
        "codex-cli", "gpt-two"
    )

    await pool.shutdown()
    assert first.shutdown_called is True
    assert second.shutdown_called is True
