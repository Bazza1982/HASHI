from types import SimpleNamespace

import pytest

from adapters import deepseek_api
from adapters.deepseek_api import DeepSeekAdapter
from adapters.openrouter_api import _APIResult
from tools.registry import ToolResult


class _DummyToolRegistry:
    max_loops = 2

    def get_tool_definitions(self, tiers=None):
        return [
            {
                "type": "function",
                "function": {
                    "name": "file_list",
                    "description": "List files.",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                },
            }
        ]

    async def execute(self, tool_name, arguments, tool_call_id=""):
        return ToolResult(tool_call_id=tool_call_id, output="tool output")


def _adapter(tmp_path):
    cfg = SimpleNamespace(
        name="ying",
        model="deepseek-v4-pro",
        workspace_dir=tmp_path,
        system_md=None,
        extra={},
    )
    adapter = DeepSeekAdapter(cfg, SimpleNamespace(), api_key="test-key")
    adapter.tool_registry = _DummyToolRegistry()
    return adapter


def test_deepseek_reasoning_helper_supports_old_api_result_shape():
    class OldAPIResult:
        def __init__(self, text, tool_calls, finish_reason):
            self.text = text
            self.tool_calls = tool_calls
            self.finish_reason = finish_reason

    result = deepseek_api._with_reasoning_content(
        OldAPIResult(text="", tool_calls=[], finish_reason="tool_calls"),
        "legacy-safe reasoning",
    )

    assert result.reasoning_content == "legacy-safe reasoning"


@pytest.mark.asyncio
async def test_deepseek_tool_loop_preserves_reasoning_content_non_stream(monkeypatch, tmp_path):
    adapter = _adapter(tmp_path)
    seen_messages = []
    tool_calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "file_list", "arguments": '{"path": "/tmp"}'},
        }
    ]

    async def fake_call(payload, headers, on_stream_event):
        seen_messages.append(payload["messages"])
        if len(seen_messages) == 1:
            return _APIResult(
                text="",
                tool_calls=tool_calls,
                finish_reason="tool_calls",
                reasoning_content="Need to inspect the directory.",
            )
        assistant_msg = payload["messages"][2]
        assert assistant_msg["reasoning_content"] == "Need to inspect the directory."
        return _APIResult(text="done", tool_calls=None, finish_reason="stop")

    monkeypatch.setattr(adapter, "_call_api_once", fake_call)

    response = await adapter.generate_response("check files", "req-test")

    assert response.is_success is True
    assert response.text == "done"
    assert response.tool_call_count == 1
    assert response.tool_loop_count == 1


@pytest.mark.asyncio
async def test_deepseek_tool_loop_preserves_reasoning_content_stream(monkeypatch, tmp_path):
    adapter = _adapter(tmp_path)
    seen_messages = []
    tool_calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "file_list", "arguments": '{"path": "/tmp"}'},
        }
    ]

    async def fake_stream(payload, headers, on_stream_event):
        seen_messages.append(payload["messages"])
        if len(seen_messages) == 1:
            return _APIResult(
                text="",
                tool_calls=tool_calls,
                finish_reason="tool_calls",
                reasoning_content="Streaming reasoning chunk.",
            )
        assistant_msg = payload["messages"][2]
        assert assistant_msg["reasoning_content"] == "Streaming reasoning chunk."
        return _APIResult(text="done", tool_calls=None, finish_reason="stop")

    async def on_stream_event(_event):
        return None

    monkeypatch.setattr(adapter, "_stream_api_once", fake_stream)

    response = await adapter.generate_response(
        "check files",
        "req-test",
        on_stream_event=on_stream_event,
    )

    assert response.is_success is True
    assert response.text == "done"
    assert response.tool_call_count == 1
    assert response.tool_loop_count == 1
