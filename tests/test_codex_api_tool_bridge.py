from __future__ import annotations

import asyncio
import json

import pytest

from adapters.codex_app_server import (
    CodexAppServerToolBridge,
    openai_messages_to_codex_conversation,
    openai_tools_to_codex_dynamic_tools,
)
from adapters.stream_events import KIND_TEXT_DELTA


WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get weather for one city",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
            "additionalProperties": False,
        },
    },
}

TIMEZONE_TOOL = {
    "type": "function",
    "function": {
        "name": "get_timezone",
        "description": "Get timezone for one city",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}


class _QueueStdout:
    def __init__(self):
        self.queue: asyncio.Queue[bytes] = asyncio.Queue()

    async def readline(self):
        return await self.queue.get()

    def emit(self, payload):
        self.queue.put_nowait(
            (json.dumps(payload, separators=(",", ":")) + "\n").encode()
        )


class _EmptyStderr:
    async def read(self, _size):
        return b""


class _FakeStdin:
    def __init__(self, proc: "_FakeAppServerProcess"):
        self.proc = proc
        self.buffer = b""

    def write(self, data):
        self.buffer += data
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            if line:
                self.proc.receive(json.loads(line))

    async def drain(self):
        return None

    def close(self):
        self.proc.finish()


class _FakeAppServerProcess:
    def __init__(
        self,
        *,
        tool_calls=None,
        final_text="",
        forbidden_item_type=None,
        fail_after_tools=False,
    ):
        self.pid = 4321
        self.returncode = None
        self.stdout = _QueueStdout()
        self.stderr = _EmptyStderr()
        self.stdin = _FakeStdin(self)
        self._done = asyncio.Event()
        self.tool_calls = list(tool_calls or [])
        self.final_text = final_text
        self.forbidden_item_type = forbidden_item_type
        self.fail_after_tools = fail_after_tools
        self.received = []
        self.thread_params = None
        self.injected_items = None
        self.turn_id = "turn-test"
        self.thread_id = "thread-test"
        self._tool_index = 0

    def finish(self):
        if self.returncode is None:
            self.returncode = 0
            self._done.set()

    def terminate(self):
        self.finish()

    def kill(self):
        self.returncode = -9
        self._done.set()

    async def wait(self):
        await self._done.wait()
        return self.returncode

    def _emit_next_tool_or_resume(self):
        if self._tool_index < len(self.tool_calls):
            call = self.tool_calls[self._tool_index]
            self._tool_index += 1
            self.stdout.emit(
                {
                    "method": "item/started",
                    "params": {
                        "threadId": self.thread_id,
                        "turnId": self.turn_id,
                        "item": {
                            "type": "dynamicToolCall",
                            "id": call["callId"],
                            "tool": call["tool"],
                            "arguments": call["arguments"],
                            "status": "inProgress",
                        },
                    },
                }
            )
            self.stdout.emit(
                {
                    "method": "item/tool/call",
                    "id": self._tool_index - 1,
                    "params": {
                        "threadId": self.thread_id,
                        "turnId": self.turn_id,
                        **call,
                    },
                }
            )
            return
        if self.fail_after_tools:
            self.stdout.emit(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": self.thread_id,
                        "turn": {
                            "id": self.turn_id,
                            "status": "failed",
                            "error": {"message": "provider failed after call"},
                        },
                    },
                }
            )
            return
        self.stdout.emit(
            {
                "method": "item/started",
                "params": {
                    "threadId": self.thread_id,
                    "turnId": self.turn_id,
                    "item": {"type": "reasoning", "id": "reasoning-after-tools"},
                },
            }
        )

    def receive(self, message):
        self.received.append(message)
        method = message.get("method")
        request_id = message.get("id")
        if method == "initialize":
            self.stdout.emit({"id": request_id, "result": {"userAgent": "fake"}})
        elif method == "initialized":
            return
        elif method == "thread/start":
            self.thread_params = message["params"]
            self.stdout.emit(
                {
                    "id": request_id,
                    "result": {"thread": {"id": self.thread_id}},
                }
            )
        elif method == "thread/inject_items":
            self.injected_items = message["params"]["items"]
            self.stdout.emit({"id": request_id, "result": {}})
        elif method == "turn/start":
            self.stdout.emit(
                {
                    "id": request_id,
                    "result": {"turn": {"id": self.turn_id}},
                }
            )
            if self.forbidden_item_type:
                self.stdout.emit(
                    {
                        "method": "item/started",
                        "params": {
                            "threadId": self.thread_id,
                            "turnId": self.turn_id,
                            "item": {
                                "type": self.forbidden_item_type,
                                "id": "forbidden-local-tool",
                            },
                        },
                    }
                )
            elif self.tool_calls:
                self._emit_next_tool_or_resume()
            else:
                self.stdout.emit(
                    {
                        "method": "item/completed",
                        "params": {
                            "threadId": self.thread_id,
                            "turnId": self.turn_id,
                            "item": {
                                "type": "agentMessage",
                                "id": "answer",
                                "phase": "final_answer",
                                "text": self.final_text,
                            },
                        },
                    }
                )
                self.stdout.emit(
                    {
                        "method": "thread/tokenUsage/updated",
                        "params": {
                            "threadId": self.thread_id,
                            "turnId": self.turn_id,
                            "tokenUsage": {
                                "total": {
                                    "inputTokens": 11,
                                    "outputTokens": 3,
                                    "reasoningOutputTokens": 1,
                                }
                            },
                        },
                    }
                )
                self.stdout.emit(
                    {
                        "method": "turn/completed",
                        "params": {
                            "threadId": self.thread_id,
                            "turn": {"id": self.turn_id, "status": "completed"},
                        },
                    }
                )
        elif method == "turn/interrupt":
            self.stdout.emit({"id": request_id, "result": {}})
            self.stdout.emit(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": self.thread_id,
                        "turn": {"id": self.turn_id, "status": "interrupted"},
                    },
                }
            )
        elif method is None and request_id is not None:
            call = self.tool_calls[self._tool_index - 1]
            self.stdout.emit(
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": self.thread_id,
                        "turnId": self.turn_id,
                        "item": {
                            "type": "dynamicToolCall",
                            "id": call["callId"],
                            "tool": call["tool"],
                            "arguments": call["arguments"],
                            "status": "completed",
                        },
                    },
                }
            )
            self._emit_next_tool_or_resume()


def test_codex_tool_conversion_preserves_schema_and_structured_history():
    tool_call = {
        "id": "call-weather",
        "type": "function",
        "function": {
            "name": "get_weather",
            "arguments": '{"city":"Sydney"}',
        },
    }
    messages = [
        {"role": "system", "content": "Be concise."},
        {"role": "user", "content": "What is the weather?"},
        {"role": "assistant", "content": None, "tool_calls": [tool_call]},
        {"role": "tool", "tool_call_id": "call-weather", "content": "Sunny"},
    ]

    dynamic = openai_tools_to_codex_dynamic_tools([WEATHER_TOOL])
    conversation = openai_messages_to_codex_conversation(
        messages,
        tools=[WEATHER_TOOL],
        tool_choice="auto",
        parallel_tool_calls=False,
    )

    assert dynamic == [
        {
            "type": "function",
            "name": "get_weather",
            "description": "Get weather for one city",
            "inputSchema": WEATHER_TOOL["function"]["parameters"],
        }
    ]
    assert conversation.history_items == [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "What is the weather?"}],
        },
        {
            "type": "function_call",
            "call_id": "call-weather",
            "name": "get_weather",
            "arguments": '{"city":"Sydney"}',
        },
        {
            "type": "function_call_output",
            "call_id": "call-weather",
            "output": "Sunny",
        },
    ]
    assert conversation.turn_input[0]["text"].startswith("Continue the conversation")
    assert "CALLER SYSTEM MESSAGE:\nBe concise." in conversation.developer_instructions
    assert "Make at most one dynamic function call" in conversation.developer_instructions


@pytest.mark.asyncio
async def test_codex_app_server_captures_all_calls_without_executing_them(monkeypatch):
    fake = _FakeAppServerProcess(
        tool_calls=[
            {
                "callId": "exec-weather",
                "tool": "get_weather",
                "arguments": {"city": "Sydney"},
            },
            {
                "callId": "exec-timezone",
                "tool": "get_timezone",
                "arguments": '{"city":"Sydney"}',
            },
        ]
    )

    async def create_subprocess(*_args, **_kwargs):
        return fake

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess)
    bridge = CodexAppServerToolBridge(
        command="codex",
        model="gpt-5.6-luna",
        effort="medium",
        idle_timeout_sec=5,
        disabled_mcp_servers=("github", "openaiDeveloperDocs"),
    )

    response = await bridge.run(
        [{"role": "user", "content": "Check Sydney"}],
        [WEATHER_TOOL, TIMEZONE_TOOL],
        "req-tools",
        parallel_tool_calls=True,
    )

    assert response.is_success is True
    assert response.stop_reason == "tool_calls"
    assert response.tool_calls == [
        {
            "id": "exec-weather",
            "type": "function",
            "function": {
                "name": "get_weather",
                "arguments": '{"city":"Sydney"}',
            },
        },
        {
            "id": "exec-timezone",
            "type": "function",
            "function": {
                "name": "get_timezone",
                "arguments": '{"city":"Sydney"}',
            },
        },
    ]
    assert [item["name"] for item in fake.thread_params["dynamicTools"]] == [
        "get_weather",
        "get_timezone",
    ]
    deferred = [
        message
        for message in fake.received
        if message.get("id") in {0, 1} and "result" in message
    ]
    assert len(deferred) == 2
    assert all("DEFERRED" in item["result"]["contentItems"][0]["text"] for item in deferred)
    command = bridge._command()
    assert (
        'mcp_servers.github={url="http://127.0.0.1/",enabled=false}' in command
    )
    assert (
        "mcp_servers.openaiDeveloperDocs="
        '{url="http://127.0.0.1/",enabled=false}' in command
    )
    assert "api.githubcopilot.com" not in " ".join(command)


@pytest.mark.asyncio
async def test_codex_app_server_enforces_parallel_calls_false(monkeypatch):
    fake = _FakeAppServerProcess(
        tool_calls=[
            {
                "callId": "exec-weather",
                "tool": "get_weather",
                "arguments": {"city": "Sydney"},
            },
            {
                "callId": "exec-timezone",
                "tool": "get_timezone",
                "arguments": {"city": "Sydney"},
            },
        ]
    )

    async def create_subprocess(*_args, **_kwargs):
        return fake

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess)
    bridge = CodexAppServerToolBridge(
        command="codex",
        model="gpt-5.6-luna",
        effort="medium",
        idle_timeout_sec=5,
    )

    response = await bridge.run(
        [{"role": "user", "content": "Check Sydney"}],
        [WEATHER_TOOL, TIMEZONE_TOOL],
        "req-no-parallel",
        parallel_tool_calls=False,
    )

    assert response.is_success is False
    assert "parallel_tool_calls=false" in response.error


@pytest.mark.asyncio
async def test_codex_app_server_does_not_authorize_calls_from_failed_turn(monkeypatch):
    fake = _FakeAppServerProcess(
        tool_calls=[
            {
                "callId": "exec-weather",
                "tool": "get_weather",
                "arguments": {"city": "Sydney"},
            }
        ],
        fail_after_tools=True,
    )

    async def create_subprocess(*_args, **_kwargs):
        return fake

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess)
    bridge = CodexAppServerToolBridge(
        command="codex",
        model="gpt-5.6-luna",
        effort="medium",
        idle_timeout_sec=5,
    )

    response = await bridge.run(
        [{"role": "user", "content": "Check Sydney"}],
        [WEATHER_TOOL],
        "req-failed-turn",
    )

    assert response.is_success is False
    assert response.tool_calls is None
    assert "provider failed after call" in response.error


@pytest.mark.asyncio
async def test_codex_app_server_returns_final_text_and_usage_in_stream_mode(monkeypatch):
    fake = _FakeAppServerProcess(final_text="Final answer")

    async def create_subprocess(*_args, **_kwargs):
        return fake

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess)
    events = []

    async def collect(event):
        events.append(event)

    bridge = CodexAppServerToolBridge(
        command="codex",
        model="gpt-5.6-luna",
        effort="medium",
        idle_timeout_sec=5,
    )
    response = await bridge.run(
        [{"role": "user", "content": "Answer directly"}],
        [WEATHER_TOOL],
        "req-answer",
        tool_choice="none",
        use_streaming=True,
        on_stream_event=collect,
    )

    assert response.is_success is True
    assert response.text == "Final answer"
    assert response.tool_calls is None
    assert response.stop_reason == "stop"
    assert response.usage.input_tokens == 11
    assert response.usage.output_tokens == 3
    assert response.usage.thinking_tokens == 1
    assert [(event.kind, event.summary) for event in events] == [
        (KIND_TEXT_DELTA, "Final answer")
    ]
    assert fake.thread_params["dynamicTools"] == []


@pytest.mark.asyncio
async def test_named_tool_choice_exposes_only_the_selected_function(monkeypatch):
    fake = _FakeAppServerProcess(
        tool_calls=[
            {
                "callId": "exec-timezone",
                "tool": "get_timezone",
                "arguments": {"city": "Sydney"},
            }
        ]
    )

    async def create_subprocess(*_args, **_kwargs):
        return fake

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess)
    bridge = CodexAppServerToolBridge(
        command="codex",
        model="gpt-5.6-luna",
        effort="medium",
        idle_timeout_sec=5,
    )
    response = await bridge.run(
        [{"role": "user", "content": "Get the timezone"}],
        [WEATHER_TOOL, TIMEZONE_TOOL],
        "req-named",
        tool_choice={
            "type": "function",
            "function": {"name": "get_timezone"},
        },
    )

    assert response.is_success is True
    assert [item["name"] for item in fake.thread_params["dynamicTools"]] == [
        "get_timezone"
    ]
    assert response.tool_calls[0]["function"]["arguments"] == '{"city":"Sydney"}'


@pytest.mark.asyncio
async def test_codex_app_server_fails_closed_on_local_tool_item(monkeypatch):
    fake = _FakeAppServerProcess(forbidden_item_type="commandExecution")

    async def create_subprocess(*_args, **_kwargs):
        return fake

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess)
    bridge = CodexAppServerToolBridge(
        command="codex",
        model="gpt-5.6-luna",
        effort="medium",
        idle_timeout_sec=5,
    )

    response = await bridge.run(
        [{"role": "user", "content": "Read a local file"}],
        [WEATHER_TOOL],
        "req-local-tool",
    )

    assert response.is_success is False
    assert "disabled local tool item 'commandExecution'" in response.error
    assert any(item.get("method") == "turn/interrupt" for item in fake.received)
