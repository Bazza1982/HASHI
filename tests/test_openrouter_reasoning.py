from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from adapters.openrouter_api import (
    OpenRouterAdapter,
    _APIResult,
    _usage_cost_usd,
    _usage_thinking_tokens,
)


def _adapter(tmp_path, *, provider_reasoning=None):
    extra = {}
    if provider_reasoning is not None:
        extra["provider_reasoning"] = provider_reasoning
        extra["reasoning_effort"] = provider_reasoning
    config = SimpleNamespace(
        name="nana",
        model="qwen/qwen3.7-flash",
        workspace_dir=tmp_path,
        system_md=None,
        extra=extra,
    )
    adapter = OpenRouterAdapter(
        config,
        SimpleNamespace(openrouter_url="https://openrouter.invalid/v1/chat"),
        api_key="test-key",
    )
    adapter.tool_registry = None
    return adapter


def test_openrouter_preserves_model_reasoning_default_when_unconfigured(tmp_path):
    adapter = _adapter(tmp_path)

    payload = adapter._build_payload([{"role": "user", "content": "hello"}])

    assert "reasoning" not in payload


def test_openrouter_sends_explicit_reasoning_off(tmp_path):
    adapter = _adapter(tmp_path, provider_reasoning="off")
    adapter.set_reasoning_enabled(False)

    payload = adapter._build_payload([{"role": "user", "content": "hello"}])

    assert payload["reasoning"] == {"enabled": False}


def test_openrouter_forwards_supported_reasoning_effort(tmp_path):
    adapter = _adapter(tmp_path, provider_reasoning="max")
    adapter.config.model = "z-ai/glm-5.3"
    adapter.set_reasoning_enabled(True)

    payload = adapter._build_payload([{"role": "user", "content": "hello"}])

    assert payload["reasoning"] == {
        "enabled": True,
        "effort": "max",
        "exclude": False,
    }


def test_openrouter_boolean_toggle_can_disable_a_default_on_model(tmp_path):
    adapter = _adapter(tmp_path)
    adapter.set_reasoning_enabled(False)

    payload = adapter._build_payload([{"role": "user", "content": "hello"}])

    assert payload["reasoning"] == {"enabled": False}


def test_openrouter_reads_current_usage_shape():
    usage = {
        "completion_tokens": 120,
        "completion_tokens_details": {"reasoning_tokens": 80},
        "cost": 0.012345,
    }

    assert _usage_thinking_tokens(usage) == 80
    assert _usage_cost_usd(usage) == pytest.approx(0.012345)


@pytest.mark.asyncio
async def test_openrouter_exposes_provider_reported_cost(tmp_path):
    adapter = _adapter(tmp_path, provider_reasoning="off")
    adapter._call_api_once = AsyncMock(
        return_value=_APIResult(
            text="done",
            tool_calls=None,
            finish_reason="stop",
            prompt_tokens=100,
            completion_tokens=20,
            cost_usd=0.001234,
        )
    )

    response = await adapter.generate_response("hello", "request-1")
    await adapter.shutdown()

    assert response.is_success is True
    assert response.cost_usd == pytest.approx(0.001234)


@pytest.mark.asyncio
async def test_openrouter_keeps_missing_provider_cost_unknown(tmp_path):
    adapter = _adapter(tmp_path, provider_reasoning="off")
    adapter._call_api_once = AsyncMock(
        return_value=_APIResult(
            text="done",
            tool_calls=None,
            finish_reason="stop",
            prompt_tokens=100,
            completion_tokens=20,
            cost_usd=None,
        )
    )

    response = await adapter.generate_response("hello", "request-2")
    await adapter.shutdown()

    assert response.is_success is True
    assert response.cost_usd is None


@pytest.mark.asyncio
async def test_openrouter_exposes_each_tool_loop_provider_call(tmp_path):
    adapter = _adapter(tmp_path, provider_reasoning="off")
    adapter.tool_registry = SimpleNamespace(
        get_tool_definitions=lambda tiers=None: [],
    )
    adapter._run_tool_calls = AsyncMock()
    adapter._call_api_once = AsyncMock(
        side_effect=[
            _APIResult(
                text="",
                tool_calls=[
                    {
                        "id": "tool-1",
                        "type": "function",
                        "function": {"name": "file_read", "arguments": "{}"},
                    }
                ],
                finish_reason="tool_calls",
                prompt_tokens=31_000,
                completion_tokens=1_000,
                thinking_tokens=700,
                cost_usd=None,
            ),
            _APIResult(
                text="done",
                tool_calls=None,
                finish_reason="stop",
                prompt_tokens=41_000,
                completion_tokens=1_000,
                thinking_tokens=600,
                cost_usd=None,
            ),
        ]
    )

    response = await adapter.generate_response("hello", "request-tool-loop")
    await adapter.shutdown()

    calls = response.stream_metadata["meter"]["provider_calls"]
    assert len(calls) == 2
    assert [call["input"] for call in calls] == [31_000, 41_000]
    assert all(call["thinking_in_output"] is True for call in calls)
