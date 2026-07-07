from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from orchestrator.api_gateway import APIGatewayServer
from orchestrator.api_gateway_preflight import check_gateway_engine, check_gateway_engines
from adapters.xai_imagine import XaiImageResult, XaiVideoResult


def test_check_gateway_engine_xai_api_with_hermes_oauth(tmp_path: Path):
    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps(
            {
                "providers": {
                    "xai-oauth": {
                        "tokens": {"refresh_token": "refresh-abc"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    cfg = SimpleNamespace(hermes_home=str(tmp_path))
    status = check_gateway_engine(cfg, {}, "xai-api")
    assert status["available"] is True
    assert "Hermes OAuth" in status["reason"]


def test_check_gateway_engine_xai_api_relogin_required_not_available(tmp_path: Path):
    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps(
            {
                "providers": {
                    "xai-oauth": {
                        "last_auth_error": {
                            "relogin_required": True,
                            "message": "Refresh token has been revoked",
                        }
                    }
                },
                "credential_pool": {"xai-oauth": []},
            }
        ),
        encoding="utf-8",
    )
    cfg = SimpleNamespace(hermes_home=str(tmp_path))
    status = check_gateway_engine(cfg, {}, "xai-api")
    assert status["available"] is False
    assert "relogin required" in status["reason"]
    assert "revoked" in status["reason"]


def test_check_gateway_engine_xai_api_with_secrets_refresh_token():
    cfg = SimpleNamespace(hermes_home="/nonexistent/hermes")
    with patch("orchestrator.api_gateway_preflight.hermes_oauth_available", return_value=False):
        status = check_gateway_engine(
            cfg,
            {"xai_oauth_refresh_token": "refresh-abc"},
            "xai-api",
        )
    assert status["available"] is True
    assert "xai_oauth_refresh_token" in status["reason"]


def test_check_gateway_engine_xai_api_without_credentials():
    cfg = SimpleNamespace(hermes_home="/nonexistent/hermes")
    with patch("orchestrator.api_gateway_preflight.hermes_oauth_available", return_value=False):
        status = check_gateway_engine(cfg, {}, "xai-api")
    assert status["available"] is False


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
        hermes_home="/nonexistent",
        gemini_cmd="gemini",
        claude_cmd="claude",
        codex_cmd="codex",
        grok_cmd="grok",
    )
    server = APIGatewayServer(cfg, secrets={}, workspace_root=tmp_path, default_model="gpt-5.5")
    response = await server.handle_health(SimpleNamespace())

    payload = json.loads(response.text)
    assert "engine_status" in payload
    assert "xai-api" in payload["engine_status"]
    assert "available_models" in payload
    assert payload["status"] in {"ok", "degraded"}


@pytest.mark.asyncio
async def test_api_gateway_models_omit_unavailable_xai(tmp_path: Path):
    cfg = SimpleNamespace(
        api_gateway_port=18801,
        api_host="127.0.0.1",
        project_root=tmp_path,
        hermes_home="/nonexistent",
        gemini_cmd="gemini",
        claude_cmd="claude",
        codex_cmd="codex",
        grok_cmd="grok",
    )
    server = APIGatewayServer(cfg, secrets={}, workspace_root=tmp_path)
    server._engine_status = check_gateway_engines(cfg, {}, {"xai-api", "codex-cli"})
    server._engine_status["xai-api"] = {"available": False, "reason": "no oauth"}
    server._engine_status["codex-cli"] = {"available": True, "reason": "/bin/codex"}

    response = await server.handle_models(SimpleNamespace())
    payload = json.loads(response.text)
    model_ids = [item["id"] for item in payload["data"]]
    assert "grok-4.3" not in model_ids
    assert any(m.startswith("gpt-") for m in model_ids)


class _FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


@pytest.mark.asyncio
async def test_api_gateway_chat_returns_503_when_engine_unavailable(tmp_path: Path):
    cfg = SimpleNamespace(
        api_gateway_port=18801,
        api_host="127.0.0.1",
        project_root=tmp_path,
        hermes_home="/nonexistent",
    )
    server = APIGatewayServer(cfg, secrets={}, workspace_root=tmp_path)
    server._engine_status["xai-api"] = {"available": False, "reason": "no oauth"}

    response = await server.handle_chat_completions(
        _FakeRequest(
            {
                "model": "grok-4.3",
                "messages": [{"role": "user", "content": "hi"}],
            }
        )
    )
    assert response.status == 503


@pytest.mark.asyncio
async def test_api_gateway_image_generations_route(tmp_path: Path):
    cfg = SimpleNamespace(
        api_gateway_port=18801,
        api_host="127.0.0.1",
        project_root=tmp_path,
        hermes_home="/nonexistent",
        xai_api_base_url="https://api.x.ai/v1",
    )
    server = APIGatewayServer(cfg, secrets={}, workspace_root=tmp_path)
    server._engine_status["xai-api"] = {"available": True, "reason": "test"}

    with patch(
        "orchestrator.api_gateway.generate_xai_image",
        new=AsyncMock(
            return_value=XaiImageResult(
                urls=["https://example.com/image.png"],
                model="grok-imagine-image",
                raw={},
            )
        ),
    ):
        response = await server.handle_image_generations(
            _FakeRequest(
                {
                    "model": "grok-imagine-image",
                    "prompt": "a small red cube",
                    "n": 1,
                }
            )
        )

    payload = json.loads(response.text)
    assert response.status == 200
    assert payload["data"][0]["url"] == "https://example.com/image.png"


@pytest.mark.asyncio
async def test_api_gateway_video_generations_route(tmp_path: Path):
    cfg = SimpleNamespace(
        api_gateway_port=18801,
        api_host="127.0.0.1",
        project_root=tmp_path,
        hermes_home="/nonexistent",
        xai_api_base_url="https://api.x.ai/v1",
    )
    server = APIGatewayServer(cfg, secrets={}, workspace_root=tmp_path)
    server._engine_status["xai-api"] = {"available": True, "reason": "test"}

    with patch(
        "orchestrator.api_gateway.generate_xai_video",
        new=AsyncMock(
            return_value=XaiVideoResult(
                request_id="vid-123",
                model="grok-imagine-video",
                raw={"request_id": "vid-123"},
            )
        ),
    ):
        response = await server.handle_video_generations(
            _FakeRequest(
                {
                    "model": "grok-imagine-video",
                    "prompt": "a small red cube rotating",
                }
            )
        )

    payload = json.loads(response.text)
    assert response.status == 200
    assert payload["request_id"] == "vid-123"
    assert payload["object"] == "video.generation"
