from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from adapters.hashi_api import HashiApiAdapter
from adapters.openrouter_api import _APIResult
from adapters.registry import get_backend_class
from orchestrator.flexible_backend_registry import (
    get_available_efforts,
    get_available_models,
)


def _adapter(
    tmp_path,
    *,
    base_url="http://gateway.invalid/v1",
    model="gpt-5.6-luna",
    effort=None,
    provider_reasoning=None,
):
    extra = {}
    if effort is not None:
        extra["effort"] = effort
    if provider_reasoning is not None:
        extra["provider_reasoning"] = provider_reasoning
    config = SimpleNamespace(
        name="arale",
        model=model,
        workspace_dir=tmp_path,
        system_md=None,
        extra=extra,
    )
    global_config = SimpleNamespace(
        her_providers={
            "providers": {
                "hashi": {
                    "engine": "hashi-api",
                    "base_url": base_url,
                    "status": "provisional",
                }
            }
        }
    )
    adapter = HashiApiAdapter(config, global_config)
    adapter.tool_registry = None
    return adapter


def test_hashi_api_is_registered_with_concrete_models():
    assert get_backend_class("hashi-api") is HashiApiAdapter
    assert get_available_models("hashi-api") == [
        "gpt-5.6-luna",
        "gpt-5.6-sol",
    ]
    assert get_available_efforts("hashi-api", "gpt-5.6-luna") == [
        "none",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    ]


@pytest.mark.parametrize(
    ("model", "configured", "expected"),
    [
        ("gpt-5.6-luna", "high", "high"),
        ("gpt-5.6-sol", "max", "max"),
        ("gpt-5.6-luna", "off", "none"),
    ],
)
def test_hashi_api_sends_gateway_reasoning_effort_not_openrouter_reasoning(
    tmp_path, model, configured, expected
):
    adapter = _adapter(
        tmp_path,
        model=model,
        effort="low",
        provider_reasoning=configured,
    )

    payload = adapter._build_payload([{"role": "user", "content": "hello"}])

    assert payload["reasoning_effort"] == expected
    assert "reasoning" not in payload


def test_hashi_api_rejects_unknown_reasoning_effort_before_http(tmp_path):
    adapter = _adapter(tmp_path, provider_reasoning="ultra")

    with pytest.raises(ValueError, match="HASHI reasoning effort"):
        adapter._build_payload([{"role": "user", "content": "hello"}])


@pytest.mark.asyncio
async def test_hashi_api_initializes_without_a_provider_secret(tmp_path):
    adapter = _adapter(tmp_path, base_url="http://127.0.0.1:18801/v1/")

    assert await adapter.initialize() is True
    assert adapter.hashi_url == "http://127.0.0.1:18801/v1/chat/completions"
    assert adapter._hashi_headers() == {"Content-Type": "application/json"}

    await adapter.shutdown()


@pytest.mark.asyncio
async def test_hashi_api_reports_usage_and_never_adds_openrouter_headers(tmp_path):
    adapter = _adapter(tmp_path)
    adapter._call_api_once = AsyncMock(
        return_value=_APIResult(
            text="done",
            tool_calls=None,
            finish_reason="stop",
            prompt_tokens=120,
            completion_tokens=30,
            thinking_tokens=10,
            cost_usd=0.0025,
            structured_data={"result": "ok"},
        )
    )

    response = await adapter.generate_response("hello", "request-1")

    assert response.is_success is True
    assert response.text == "done"
    assert response.structured_data == {"result": "ok"}
    assert response.usage.input_tokens == 120
    assert response.usage.output_tokens == 30
    assert response.usage.thinking_tokens == 10
    assert response.cost_usd == pytest.approx(0.0025)
    _payload, headers, _callback = adapter._call_api_once.await_args.args
    assert headers == {"Content-Type": "application/json"}
    assert "Authorization" not in headers

    await adapter.shutdown()
