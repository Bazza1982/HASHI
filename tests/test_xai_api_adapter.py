from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from adapters.xai_api import XaiApiAdapter
from adapters.xai_oauth_credentials import XaiCredentials
from orchestrator.api_gateway import _ENGINE_FOR_MODEL
from orchestrator.flexible_backend_registry import get_available_models
from orchestrator.model_catalog import AVAILABLE_XAI_API_MODELS


def test_xai_api_models_registered_in_gateway_and_registry():
    assert "grok-4.5" in AVAILABLE_XAI_API_MODELS
    assert _ENGINE_FOR_MODEL["grok-4.5"] == "xai-api"
    assert "grok-4.5" in get_available_models("xai-api")
    assert "grok-4.3" in AVAILABLE_XAI_API_MODELS
    assert _ENGINE_FOR_MODEL["grok-4.3"] == "xai-api"
    assert "grok-4.3" in get_available_models("xai-api")


@pytest.mark.asyncio
async def test_xai_api_adapter_initialize_resolves_oauth(tmp_path):
    cfg = SimpleNamespace(
        name="test-agent",
        workspace_dir=tmp_path,
        system_md=None,
        model="grok-4.3",
    )
    global_cfg = SimpleNamespace(
        hermes_home=None,
        xai_api_base_url="https://api.x.ai/v1",
    )

    with patch(
        "adapters.xai_api.resolve_xai_credentials",
        return_value=XaiCredentials(
            provider="xai-oauth",
            api_key="fresh-token",
            base_url="https://api.x.ai/v1",
            source="test",
        ),
    ):
        adapter = XaiApiAdapter(cfg, global_cfg, api_key=None)
        ok = await adapter.initialize()

    assert ok is True
    assert adapter._bearer_token == "fresh-token"


@pytest.mark.asyncio
async def test_xai_api_adapter_generate_response_success(tmp_path):
    cfg = SimpleNamespace(
        name="test-agent",
        workspace_dir=tmp_path,
        system_md=None,
        model="grok-4.3",
    )
    global_cfg = SimpleNamespace(
        hermes_home=None,
        xai_api_base_url="https://api.x.ai/v1",
    )

    adapter = XaiApiAdapter(cfg, global_cfg, api_key="static")
    adapter.sys_prompt = "system"
    adapter._bearer_token = "static"
    adapter._base_url = "https://api.x.ai/v1"
    adapter.tool_registry = None

    from adapters.openrouter_api import _APIResult

    fake_result = _APIResult(
        text="OK",
        tool_calls=None,
        finish_reason="stop",
        prompt_tokens=10,
        completion_tokens=2,
        thinking_tokens=0,
    )

    with patch.object(adapter, "_resolve_bearer", new=AsyncMock()), patch.object(
        adapter, "_call_api_once", new=AsyncMock(return_value=fake_result)
    ):
        response = await adapter.generate_response("Reply exactly: OK", "req-1")

    assert response.is_success is True
    assert response.text == "OK"


def test_xai_api_adapter_uses_responses_api_for_grok45(tmp_path):
    cfg = SimpleNamespace(
        name="test-agent",
        workspace_dir=tmp_path,
        system_md=None,
        model="grok-4.5",
    )
    global_cfg = SimpleNamespace(
        hermes_home=None,
        xai_api_base_url="https://api.x.ai/v1",
        xai_use_responses_api=False,
    )
    adapter = XaiApiAdapter(cfg, global_cfg, api_key="static")
    assert adapter._use_responses_api() is True
    payload = adapter._build_payload(
        [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "hello"},
        ]
    )
    assert payload["model"] == "grok-4.5"
    assert payload["input"] == "System: system\n\nhello"


def test_xai_api_adapter_parse_responses_body():
    adapter = XaiApiAdapter(
        SimpleNamespace(name="t", workspace_dir=Path("/tmp"), system_md=None, model="grok-build-0.1"),
        SimpleNamespace(hermes_home=None, xai_api_base_url="https://api.x.ai/v1", xai_use_responses_api=False),
        api_key="static",
    )
    result = adapter._parse_api_body(
        {
            "status": "completed",
            "output_text": "OK",
            "usage": {"input_tokens": 3, "output_tokens": 1},
        }
    )
    assert result.text == "OK"
    assert result.prompt_tokens == 3
    assert result.completion_tokens == 1


@pytest.mark.asyncio
async def test_xai_api_adapter_retries_403_with_force_refresh(tmp_path):
    cfg = SimpleNamespace(
        name="test-agent",
        workspace_dir=tmp_path,
        system_md=None,
        model="grok-4.3",
    )
    global_cfg = SimpleNamespace(
        hermes_home=None,
        xai_api_base_url="https://api.x.ai/v1",
        xai_use_responses_api=False,
    )
    adapter = XaiApiAdapter(cfg, global_cfg, api_key="stale")
    adapter._bearer_token = "stale"
    adapter._base_url = "https://api.x.ai/v1"

    class _FakeResponse:
        def __init__(self, status_code, body=None):
            self.status_code = status_code
            self._body = body or {}

        def raise_for_status(self):
            if self.status_code >= 400:
                raise AssertionError(f"unexpected status {self.status_code}")

        def json(self):
            return self._body

    ok_body = {
        "choices": [
            {
                "message": {"content": "OK"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    adapter.client = SimpleNamespace(
        post=AsyncMock(side_effect=[_FakeResponse(403), _FakeResponse(200, ok_body)])
    )

    async def _refresh(*, force_refresh=False):
        assert force_refresh is True
        adapter._bearer_token = "fresh"

    with patch.object(adapter, "_resolve_bearer", new=AsyncMock(side_effect=_refresh)) as refresh:
        result = await adapter._call_api_once({"model": "grok-4.3"}, adapter._xai_headers(), None)

    assert result.text == "OK"
    assert refresh.await_count == 1
    assert adapter.client.post.await_count == 2


@pytest.mark.asyncio
async def test_xai_api_adapter_generate_response_via_responses_api(tmp_path):
    cfg = SimpleNamespace(
        name="test-agent",
        workspace_dir=tmp_path,
        system_md=None,
        model="grok-build-0.1",
    )
    global_cfg = SimpleNamespace(
        hermes_home=None,
        xai_api_base_url="https://api.x.ai/v1",
        xai_use_responses_api=False,
    )

    adapter = XaiApiAdapter(cfg, global_cfg, api_key="static")
    adapter.sys_prompt = "system"
    adapter._bearer_token = "static"
    adapter._base_url = "https://api.x.ai/v1"
    adapter.tool_registry = None

    from adapters.openrouter_api import _APIResult

    fake_result = _APIResult(
        text="BUILD OK",
        tool_calls=None,
        finish_reason="completed",
        prompt_tokens=5,
        completion_tokens=2,
        thinking_tokens=0,
    )

    with patch.object(adapter, "_resolve_bearer", new=AsyncMock()), patch.object(
        adapter, "_call_api_once", new=AsyncMock(return_value=fake_result)
    ):
        response = await adapter.generate_response("build this", "req-2")

    assert response.is_success is True
    assert response.text == "BUILD OK"


@pytest.mark.asyncio
async def test_xai_api_adapter_generate_imagine_response(tmp_path):
    cfg = SimpleNamespace(
        name="test-agent",
        workspace_dir=tmp_path,
        system_md=None,
        model="grok-imagine-image-quality",
    )
    global_cfg = SimpleNamespace(
        hermes_home=None,
        xai_api_base_url="https://api.x.ai/v1",
    )

    adapter = XaiApiAdapter(cfg, global_cfg, api_key="static")
    adapter._bearer_token = "static"
    adapter._base_url = "https://api.x.ai/v1"

    from adapters.xai_imagine import XaiImageResult

    fake_image = XaiImageResult(
        urls=["https://example.com/image.png"],
        model="grok-imagine-image-quality",
        raw={},
    )

    with patch.object(adapter, "_resolve_bearer", new=AsyncMock()), patch(
        "adapters.xai_api.generate_xai_image",
        new=AsyncMock(return_value=fake_image),
    ):
        response = await adapter.generate_response("a red rose", "req-3")

    assert response.is_success is True
    assert "https://example.com/image.png" in response.text
