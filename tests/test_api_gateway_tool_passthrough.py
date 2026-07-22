from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from adapters.base import BackendResponse, TokenUsage
from adapters.stream_events import KIND_TEXT_DELTA, StreamEvent
from orchestrator.api_gateway import APIGatewayServer, _AdapterPool, _uses_external_tool_protocol


TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "local_read",
        "description": "Read a local Aptenra file",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
}

TOOL_CALL = {
    "id": "call-local-1",
    "type": "function",
    "function": {"name": "local_read", "arguments": '{"path":"notes.txt"}'},
}


def test_empty_tools_do_not_force_legacy_clients_into_external_mode():
    messages = [{"role": "user", "content": "hello"}]
    assert _uses_external_tool_protocol({"tools": []}, messages) is False


class _ExternalAdapter:
    def __init__(self, *, supports: bool = True, stream_text: str = ""):
        self.supports = supports
        self.stream_text = stream_text
        self.calls = []

    def supports_external_tool_passthrough(self, model=None):
        self.capability_model = model
        return self.supports

    async def generate_external_tool_response(
        self,
        messages,
        tools,
        request_id,
        *,
        tool_choice=None,
        parallel_tool_calls=None,
        use_streaming=False,
        request_options=None,
        on_stream_event=None,
        model=None,
    ):
        self.calls.append(
            {
                "messages": messages,
                "tools": tools,
                "request_id": request_id,
                "tool_choice": tool_choice,
                "parallel_tool_calls": parallel_tool_calls,
                "use_streaming": use_streaming,
                "request_options": request_options,
                "model": model,
            }
        )
        if use_streaming and self.stream_text and on_stream_event is not None:
            await on_stream_event(StreamEvent(kind=KIND_TEXT_DELTA, summary=self.stream_text))
        return BackendResponse(
            text=self.stream_text,
            duration_ms=1,
            is_success=True,
            tool_calls=[TOOL_CALL],
            stop_reason="tool_calls",
            usage=TokenUsage(input_tokens=12, output_tokens=4),
            tool_call_count=1,
            tool_loop_count=0,
        )


class _Pool:
    def __init__(self, adapter):
        self.adapter = adapter
        self._adapters = {}
        self.calls = []

    async def get(self, engine, model):
        self.calls.append((engine, model))
        self._adapters[engine] = self.adapter
        return self.adapter

    async def update_model(self, engine, model):
        self.calls.append(("update", engine, model))

    async def shutdown(self):
        return None


class _Request:
    def __init__(self, body):
        self.body = body

    async def json(self):
        return self.body


def _server(tmp_path: Path, adapter: _ExternalAdapter) -> APIGatewayServer:
    config = SimpleNamespace(
        api_gateway_port=18803,
        api_host="127.0.0.1",
        project_root=tmp_path,
    )
    server = APIGatewayServer(config, secrets={}, workspace_root=tmp_path)
    server._engine_status["xai-api"] = {"available": True, "reason": "test"}
    server._pool = _Pool(adapter)
    return server


@pytest.mark.asyncio
async def test_gateway_pool_passes_xai_static_and_refresh_credentials(tmp_path, monkeypatch):
    captured = {}

    class _Backend:
        def __init__(self, config, global_config, api_key):
            captured["config"] = config
            captured["global_config"] = global_config
            captured["api_key"] = api_key

        async def initialize(self):
            return True

    global_config = SimpleNamespace(project_root=tmp_path)
    pool = _AdapterPool(
        global_config,
        {
            "xai_api_key": "static-secret",
            "xai_oauth_refresh_token": "refresh-secret",
        },
        tmp_path / "workspaces",
    )
    monkeypatch.setattr("orchestrator.api_gateway.get_backend_class", lambda _engine: _Backend)

    await pool.get("xai-api", "grok-4.3")

    assert captured["api_key"] == {
        "xai_api_key": "static-secret",
        "xai_oauth_refresh_token": "refresh-secret",
    }


@pytest.mark.asyncio
async def test_sync_external_tools_preserve_full_protocol_and_return_tool_calls(tmp_path):
    adapter = _ExternalAdapter()
    server = _server(tmp_path, adapter)
    messages = [
        {"role": "user", "content": "Read the note"},
        {"role": "assistant", "content": None, "tool_calls": [TOOL_CALL]},
        {"role": "tool", "tool_call_id": "call-local-1", "content": "local result"},
    ]
    body = {
        "model": "grok-4.3",
        "messages": messages,
        "tools": [TOOL_SCHEMA],
        "tool_choice": "required",
        "parallel_tool_calls": False,
    }

    response = await server.handle_chat_completions(_Request(body))
    payload = json.loads(response.text)

    assert response.status == 200
    assert payload["choices"][0]["message"] == {
        "role": "assistant",
        "content": None,
        "tool_calls": [TOOL_CALL],
    }
    assert payload["choices"][0]["finish_reason"] == "tool_calls"
    assert payload["usage"] == {
        "prompt_tokens": 12,
        "completion_tokens": 4,
        "total_tokens": 16,
    }
    assert adapter.calls[0]["messages"] == messages
    assert adapter.calls[0]["tools"] == [TOOL_SCHEMA]
    assert adapter.calls[0]["tool_choice"] == "required"
    assert adapter.calls[0]["parallel_tool_calls"] is False
    assert adapter.calls[0]["use_streaming"] is False
    assert adapter.calls[0]["model"] == "grok-4.3"


@pytest.mark.asyncio
async def test_external_tools_reject_cli_models_without_starting_adapter(tmp_path):
    adapter = _ExternalAdapter()
    server = _server(tmp_path, adapter)
    response = await server.handle_chat_completions(
        _Request(
            {
                "model": "gpt-5.5",
                "messages": [{"role": "user", "content": "Use the tool"}],
                "tools": [TOOL_SCHEMA],
            }
        )
    )
    payload = json.loads(response.text)

    assert response.status == 400
    assert payload["error"]["code"] == "external_tool_passthrough_unsupported"
    assert server._pool.calls == []


@pytest.mark.asyncio
async def test_external_tools_reject_gateway_session_cache(tmp_path):
    adapter = _ExternalAdapter()
    server = _server(tmp_path, adapter)
    response = await server.handle_chat_completions(
        _Request(
            {
                "model": "grok-4.3",
                "messages": [{"role": "user", "content": "Use the tool"}],
                "tools": [TOOL_SCHEMA],
                "session_id": "unsafe-shared-session",
            }
        )
    )
    payload = json.loads(response.text)

    assert response.status == 400
    assert payload["error"]["code"] == "external_tools_session_unsupported"
    assert server._pool.calls == []


@pytest.mark.asyncio
async def test_responses_api_model_is_rejected_by_adapter_capability(tmp_path):
    adapter = _ExternalAdapter(supports=False)
    server = _server(tmp_path, adapter)
    response = await server.handle_chat_completions(
        _Request(
            {
                "model": "grok-4.5",
                "messages": [{"role": "user", "content": "Use the tool"}],
                "tools": [TOOL_SCHEMA],
            }
        )
    )
    payload = json.loads(response.text)

    assert response.status == 400
    assert payload["error"]["code"] == "external_tool_passthrough_unsupported"
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_streaming_external_tools_emit_openai_tool_delta_and_finish_reason(tmp_path):
    adapter = _ExternalAdapter(stream_text="Checking locally. ")
    server = _server(tmp_path, adapter)

    async with TestClient(TestServer(server.app)) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "grok-4.3",
                "messages": [{"role": "user", "content": "Read the note"}],
                "tools": [TOOL_SCHEMA],
                "stream": True,
            },
        )
        raw = await response.text()

    events = [
        json.loads(line.removeprefix("data: "))
        for line in raw.splitlines()
        if line.startswith("data: {")
    ]
    assert response.status == 200
    assert events[0]["choices"][0]["delta"] == {"role": "assistant"}
    assert any(
        event["choices"][0]["delta"].get("content") == "Checking locally. "
        for event in events
    )
    tool_event = next(
        event for event in events if event["choices"][0]["delta"].get("tool_calls")
    )
    assert tool_event["choices"][0]["delta"]["tool_calls"] == [
        {"index": 0, **TOOL_CALL}
    ]
    assert events[-1]["choices"][0]["finish_reason"] == "tool_calls"
    assert raw.rstrip().endswith("data: [DONE]")
    assert adapter.calls[0]["use_streaming"] is True
