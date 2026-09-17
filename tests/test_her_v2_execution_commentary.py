from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from adapters.base import BackendResponse, TokenUsage
from adapters.hashi_api import HashiApiAdapter
from adapters.openrouter_api import OpenRouterAdapter, _APIResult
from adapters.stream_events import (
    DELIVERY_USER_COMMENTARY,
    KIND_COMMENTARY,
    StreamEvent,
)


class _DummyToolRegistry:
    def is_allowed(self, name):
        return True

    def get_tool_definitions(self, tiers=None):
        return [{"type": "function", "function": {"name": "test_tool", "parameters": {"type": "object"}}}]

    async def execute(self, name, args):
        return {"result": "ok"}


def _make_hashi_adapter(tmp_path):
    config = SimpleNamespace(
        name="test_agent",
        model="gpt-5.6-sol",
        workspace_dir=tmp_path,
        system_md=None,
        extra={},
    )
    global_config = SimpleNamespace(
        her_providers={
            "providers": {
                "hashi": {
                    "engine": "hashi-api",
                    "base_url": "http://test.invalid/v1",
                    "status": "provisional",
                }
            }
        }
    )
    adapter = HashiApiAdapter(config, global_config)
    adapter.tool_registry = _DummyToolRegistry()
    return adapter


def _make_openrouter_adapter(tmp_path):
    config = SimpleNamespace(
        name="test_agent_or",
        model="deepseek/deepseek-v4-pro",
        workspace_dir=tmp_path,
        system_md=None,
        extra={},
    )
    global_config = SimpleNamespace(
        openrouter_api_key="sk-test",
        openrouter_url="https://openrouter.invalid/v1/chat",
    )
    adapter = OpenRouterAdapter(config, global_config, api_key="sk-test")
    adapter.tool_registry = _DummyToolRegistry()
    return adapter


@pytest.mark.asyncio
async def test_hashi_api_emits_interim_commentary_before_tool_calls(tmp_path):
    adapter = _make_hashi_adapter(tmp_path)
    events: list[StreamEvent] = []

    async def capture(event: StreamEvent):
        events.append(event)

    tool_call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "test_tool", "arguments": "{}"},
    }
    mock_api = AsyncMock(
        side_effect=[
            _APIResult("现状已核实：开始按边界落代码。", [tool_call], "tool_calls", 100, 10),
            _APIResult("任务全部完成，验证通过。", None, "stop", 20, 5),
        ]
    )
    adapter._call_api_once = mock_api
    adapter._stream_api_once = mock_api

    async def run_tool_calls(_calls, messages, _callback, **_kwargs):
        messages.append(
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": '{"result": "ok"}',
            }
        )

    adapter._run_tool_calls = run_tool_calls

    resp = await adapter.generate_response(
        prompt="落代码并测试",
        request_id="req-123",
        on_stream_event=capture,
    )

    assert resp.is_success
    assert resp.text == "任务全部完成，验证通过。"

    commentary_events = [e for e in events if e.kind == KIND_COMMENTARY]
    assert len(commentary_events) == 1
    assert commentary_events[0].summary == "现状已核实：开始按边界落代码。"
    assert commentary_events[0].delivery_class == DELIVERY_USER_COMMENTARY
    assert commentary_events[0].event_id == "req-123:commentary:1"


@pytest.mark.asyncio
async def test_hashi_api_skips_json_dump_as_commentary(tmp_path):
    adapter = _make_hashi_adapter(tmp_path)
    events: list[StreamEvent] = []

    async def capture(event: StreamEvent):
        events.append(event)

    tool_call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "test_tool", "arguments": "{}"},
    }
    mock_api = AsyncMock(
        side_effect=[
            # Model hallucinated JSON as text
            _APIResult('{"tool_name": "test_tool", "args": {}}', [tool_call], "tool_calls", 100, 10),
            _APIResult("最终结果", None, "stop", 20, 5),
        ]
    )
    adapter._call_api_once = mock_api
    adapter._stream_api_once = mock_api

    async def run_tool_calls(_calls, messages, _callback, **_kwargs):
        messages.append(
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": '{"result": "ok"}',
            }
        )

    adapter._run_tool_calls = run_tool_calls

    resp = await adapter.generate_response(
        prompt="do task",
        request_id="req-456",
        on_stream_event=capture,
    )

    assert resp.is_success
    commentary_events = [e for e in events if e.kind == KIND_COMMENTARY]
    assert len(commentary_events) == 0


@pytest.mark.asyncio
async def test_openrouter_emits_interim_commentary_before_tool_calls(tmp_path):
    adapter = _make_openrouter_adapter(tmp_path)
    events: list[StreamEvent] = []

    async def capture(event: StreamEvent):
        events.append(event)

    tool_call = {
        "id": "call_or_1",
        "type": "function",
        "function": {"name": "test_tool", "arguments": "{}"},
    }
    mock_api = AsyncMock(
        side_effect=[
            _APIResult("正在复核状态并执行修复。", [tool_call], "tool_calls", 100, 10),
            _APIResult("全部修复完成。", None, "stop", 20, 5),
        ]
    )
    adapter._call_api_once = mock_api
    adapter._stream_api_once = mock_api

    async def run_tool_calls(_calls, messages, _callback, **_kwargs):
        messages.append(
            {
                "role": "tool",
                "tool_call_id": "call_or_1",
                "content": '{"result": "ok"}',
            }
        )

    adapter._run_tool_calls = run_tool_calls

    resp = await adapter.generate_response(
        prompt="执行修复",
        request_id="req-789",
        on_stream_event=capture,
    )

    assert resp.is_success
    assert resp.text == "全部修复完成。"

    commentary_events = [e for e in events if e.kind == KIND_COMMENTARY]
    assert len(commentary_events) == 1
    assert commentary_events[0].summary == "正在复核状态并执行修复。"
    assert commentary_events[0].delivery_class == DELIVERY_USER_COMMENTARY
