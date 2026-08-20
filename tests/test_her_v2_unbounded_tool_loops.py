from types import SimpleNamespace

import pytest

from adapters.deepseek_api import DeepSeekAdapter
from adapters.her_v2 import _UnboundedToolRegistry
from adapters.ollama_api import OllamaAdapter
from adapters.openrouter_api import OpenRouterAdapter, _APIResult
from adapters.xai_api import XaiApiAdapter
from tools.registry import ToolResult


class _LimitedRegistry:
    def __init__(self, max_loops: int = 2):
        self.max_loops = max_loops
        self.calls = []

    def get_tool_definitions(self, tiers=None):
        del tiers
        return [
            {
                "type": "function",
                "function": {
                    "name": "file_read",
                    "description": "Read a file.",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                },
            }
        ]

    async def execute(self, tool_name, arguments, tool_call_id=""):
        self.calls.append((tool_name, arguments, tool_call_id))
        return ToolResult(tool_call_id=tool_call_id, output="tool output")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("adapter_class", "engine", "model"),
    [
        (OpenRouterAdapter, "openrouter-api", "configured/model"),
        (DeepSeekAdapter, "deepseek-api", "deepseek-v4-pro"),
        (OllamaAdapter, "ollama-api", "qwen3:32b"),
        (XaiApiAdapter, "xai-api", "grok-4.3"),
    ],
)
async def test_gateway_adapter_continues_past_registry_limit_for_her_v2(
    adapter_class,
    engine,
    model,
    monkeypatch,
    tmp_path,
):
    config = SimpleNamespace(
        name="test-agent",
        engine=engine,
        model=model,
        workspace_dir=tmp_path,
        system_md=None,
        extra={},
    )
    global_config = SimpleNamespace(
        openrouter_url="https://example.invalid/chat/completions",
        xai_api_base_url="https://example.invalid/v1",
        xai_use_responses_api=False,
        hermes_home=None,
    )
    adapter = adapter_class(config, global_config, api_key="test-key")
    registry = _LimitedRegistry(max_loops=2)
    adapter.tool_registry = _UnboundedToolRegistry(registry)
    monkeypatch.setattr(adapter, "_ensure_client", lambda: None)

    if isinstance(adapter, XaiApiAdapter):
        async def resolve_bearer(*, force_refresh=False):
            del force_refresh

        monkeypatch.setattr(adapter, "_resolve_bearer", resolve_bearer)

    provider_payloads = []
    tool_calls = [
        {
            "id": "call-1",
            "type": "function",
            "function": {
                "name": "file_read",
                "arguments": '{"path":"notes.txt"}',
            },
        }
    ]

    async def call_api_once(payload, headers, on_stream_event):
        del headers, on_stream_event
        provider_payloads.append(payload)
        if len(provider_payloads) <= 4:
            return _APIResult(
                text="",
                tool_calls=tool_calls,
                finish_reason="tool_calls",
            )
        return _APIResult(
            text="completed after four tool rounds",
            tool_calls=None,
            finish_reason="stop",
        )

    monkeypatch.setattr(adapter, "_call_api_once", call_api_once)

    response = await adapter.generate_response("finish the task", "request-1")

    assert registry.max_loops == 2
    assert len(registry.calls) == 4
    assert len(provider_payloads) == 5
    assert response.is_success is True
    assert response.text == "completed after four tool rounds"
    assert all(
        not str(message.get("content") or "").startswith("Tool loop limit reached.")
        for payload in provider_payloads
        for message in payload["messages"]
    )
