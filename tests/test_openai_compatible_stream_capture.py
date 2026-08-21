from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from adapters.ollama_api import OllamaAdapter
from adapters.openrouter_api import OpenRouterAdapter


class _StreamResponse:
    def raise_for_status(self):
        return None

    async def aiter_lines(self):
        chunks = [
            {"choices": [{"delta": {"reasoning": "control-json"}}]},
            {
                "choices": [
                    {
                        "delta": {"content": "formal-text"},
                        "finish_reason": "stop",
                    }
                ]
            },
        ]
        for chunk in chunks:
            yield f"data: {json.dumps(chunk)}"
        yield "data: [DONE]"


class _StreamContext:
    async def __aenter__(self):
        return _StreamResponse()

    async def __aexit__(self, *_args):
        return False


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter_type", [OpenRouterAdapter, OllamaAdapter])
async def test_openai_compatible_stream_waits_for_capture_callbacks(
    tmp_path,
    adapter_type,
):
    config = SimpleNamespace(
        name="provider",
        engine="provider-api",
        model="model",
        workspace_dir=tmp_path,
        system_md=None,
        extra={},
    )
    global_config = SimpleNamespace(openrouter_url="https://example.invalid")
    adapter = adapter_type(config, global_config, api_key="test")
    adapter.client = SimpleNamespace(stream=lambda *_args, **_kwargs: _StreamContext())
    events = []

    async def capture(event):
        await asyncio.sleep(0.01)
        events.append((event.kind, event.raw_delta or event.summary))

    result = await adapter._stream_api_once({}, {}, capture)

    assert result.text == "formal-text"
    assert events == [
        ("thinking", "control-json"),
        ("text_delta", "formal-text"),
    ]
