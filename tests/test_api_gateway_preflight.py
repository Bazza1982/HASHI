from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.api_gateway import APIGatewayServer
from orchestrator.api_gateway_preflight import check_gateway_engine, check_gateway_engines


def test_check_gateway_engine_codex_cli_missing():
    cfg = SimpleNamespace(codex_cmd="missing-codex-binary-xyz")
    status = check_gateway_engine(cfg, {}, "codex-cli")
    assert status["available"] is False


@pytest.mark.asyncio
async def test_api_gateway_health_reports_engine_status(tmp_path: Path):
    cfg = SimpleNamespace(
        api_gateway_port=18801,
        api_host="127.0.0.1",
        project_root=tmp_path,
        agy_cmd="agy",
        claude_cmd="claude",
        codex_cmd="codex",
        grok_cmd="grok",
    )
    server = APIGatewayServer(cfg, secrets={}, workspace_root=tmp_path, default_model="gpt-5.5")
    response = await server.handle_health(SimpleNamespace())

    payload = json.loads(response.text)
    assert "engine_status" in payload
    assert "codex-cli" in payload["engine_status"]
    assert "available_models" in payload
    assert payload["status"] in {"ok", "degraded"}


@pytest.mark.asyncio
async def test_api_gateway_models_omit_unavailable_engine(tmp_path: Path):
    cfg = SimpleNamespace(
        api_gateway_port=18801,
        api_host="127.0.0.1",
        project_root=tmp_path,
        agy_cmd="agy",
        claude_cmd="claude",
        codex_cmd="codex",
        grok_cmd="grok",
    )
    server = APIGatewayServer(cfg, secrets={}, workspace_root=tmp_path)
    server._engine_status = check_gateway_engines(cfg, {}, {"claude-cli", "codex-cli"})
    server._engine_status["claude-cli"] = {"available": False, "reason": "missing"}
    server._engine_status["codex-cli"] = {"available": True, "reason": "/bin/codex"}

    response = await server.handle_models(SimpleNamespace())
    payload = json.loads(response.text)
    model_ids = [item["id"] for item in payload["data"]]

    claude_models = [
        model
        for model in server._all_models
        if server._engine_for_model[model] == "claude-cli"
    ]
    assert claude_models
    assert not set(claude_models) & set(model_ids)
    assert any(m.startswith("gpt-") for m in model_ids)


class _FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


@pytest.mark.asyncio
async def test_api_gateway_chat_rejects_unknown_model(tmp_path: Path):
    cfg = SimpleNamespace(
        api_gateway_port=18801,
        api_host="127.0.0.1",
        project_root=tmp_path,
    )
    server = APIGatewayServer(cfg, secrets={}, workspace_root=tmp_path)

    response = await server.handle_chat_completions(
        _FakeRequest(
            {
                "model": "not-a-known-model",
                "messages": [{"role": "user", "content": "hi"}],
            }
        )
    )
    assert response.status in {400, 404}
# HASHI-Fingerprint: hfp_2df56bf9c5cb4863b67572d647e1a905
