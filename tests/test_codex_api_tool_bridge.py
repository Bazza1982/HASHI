from __future__ import annotations

import asyncio
import json
import re

import pytest

from adapters.codex_app_server import (
    CodexAppServerToolBridge,
    codex_dynamic_tool_name,
    openai_messages_to_codex_conversation,
    openai_tools_to_codex_dynamic_tools,
)
from adapters.stream_events import KIND_TEXT_DELTA
from tools.schemas import TOOL_SCHEMA_MAP

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
WEB_SEARCH_TOOL = TOOL_SCHEMA_MAP["web_search"]

WEATHER_CODEX_TOOL = codex_dynamic_tool_name("get_weather")
TIMEZONE_CODEX_TOOL = codex_dynamic_tool_name("get_timezone")
WEB_SEARCH_CODEX_TOOL = codex_dynamic_tool_name("web_search")


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
    def __init__(self, proc: _FakeAppServerProcess):
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
        host_method=None,
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
        self.host_method = host_method
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
                        "method": "item/completed",
                        "params": {
                            "threadId": self.thread_id,
                            "turnId": self.turn_id,
                            "item": {
                                "type": self.forbidden_item_type,
                                "id": "forbidden-local-tool",
                                "status": "failed",
                                "error": {"message": "native tool unavailable"},
                            },
                        },
                    }
                )
                self.stdout.emit(
                    {
                        "method": "item/completed",
                        "params": {
                            "threadId": self.thread_id,
                            "turnId": self.turn_id,
                            "item": {
                                "type": "agentMessage",
                                "id": "answer-after-native-tool",
                                "phase": "final_answer",
                                "text": self.final_text,
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
            elif self.host_method:
                self.stdout.emit(
                    {
                        "method": self.host_method,
                        "id": "unsupported-host-call",
                        "params": {"threadId": self.thread_id},
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
        elif request_id == "unsupported-host-call":
            self.stdout.emit(
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": self.thread_id,
                        "turnId": self.turn_id,
                        "item": {
                            "type": "agentMessage",
                            "id": "answer-after-host-error",
                            "phase": "final_answer",
                            "text": self.final_text,
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
            "name": WEATHER_CODEX_TOOL,
            "description": (
                "Caller-visible function name: get_weather. "
                "Get weather for one city"
            ),
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
            "name": WEATHER_CODEX_TOOL,
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
    assert (
        f"'get_weather' -> '{WEATHER_CODEX_TOOL}'"
        in conversation.developer_instructions
    )
    assert "Make at most one dynamic function call" in (
        conversation.developer_instructions
    )


def test_final_user_multiple_images_preserve_turn_input_order_and_detail():
    conversation = openai_messages_to_codex_conversation(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Compare both."},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64,AAAA",
                            "detail": "high",
                        },
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/jpeg;base64,BBBB"},
                    },
                ],
            }
        ],
        tools=[],
        tool_choice="none",
    )

    assert conversation.turn_input == [
        {"type": "text", "text": "Compare both."},
        {
            "type": "image",
            "url": "data:image/png;base64,AAAA",
            "detail": "high",
        },
        {"type": "image", "url": "data:image/jpeg;base64,BBBB"},
    ]


def test_historical_user_images_are_preserved_but_assistant_images_are_not_user_input():
    conversation = openai_messages_to_codex_conversation(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "First turn."},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,HISTORY"},
                    },
                ],
            },
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "I saw it."},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,ASSISTANT"},
                    },
                ],
            },
            {"role": "user", "content": "Continue."},
        ],
        tools=[],
    )

    assert conversation.history_items == [
        {
            "type": "message",
            "role": "user",
            "content": [
                {"type": "input_text", "text": "First turn."},
                {
                    "type": "input_image",
                    "image_url": "data:image/png;base64,HISTORY",
                },
            ],
        },
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "I saw it."}],
        },
    ]
    assert conversation.turn_input == [{"type": "text", "text": "Continue."}]


def test_structured_tool_image_result_preserves_function_output_shape():
    conversation = openai_messages_to_codex_conversation(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-image",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-image",
                "content": [
                    {"type": "text", "text": "Rendered chart"},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64,TOOL",
                            "detail": "original",
                        },
                    },
                ],
            },
        ],
        tools=[WEATHER_TOOL],
    )

    assert conversation.history_items[-1] == {
        "type": "function_call_output",
        "call_id": "call-image",
        "output": [
            {"type": "input_text", "text": "Rendered chart"},
            {
                "type": "input_image",
                "image_url": "data:image/png;base64,TOOL",
                "detail": "original",
            },
        ],
    }


def test_all_hashi_tools_receive_unique_codex_safe_aliases():
    tools = list(TOOL_SCHEMA_MAP.values())
    dynamic = openai_tools_to_codex_dynamic_tools(tools)
    aliases = [item["name"] for item in dynamic]

    assert len(aliases) == len(set(aliases)) == len(tools)
    assert all(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) for name in aliases)
    assert all(name.startswith("hashi_ext_") for name in aliases)
    public_to_alias = {
        tool["function"]["name"]: alias
        for tool, alias in zip(tools, aliases, strict=True)
    }
    for reserved_name in ("web_search", "bash", "apply_patch", "browser_screenshot"):
        assert public_to_alias[reserved_name] != reserved_name
        assert reserved_name in public_to_alias[reserved_name]
        assert public_to_alias[reserved_name] == codex_dynamic_tool_name(reserved_name)


def test_codex_alias_is_deterministic_and_bounded_for_long_names():
    first_name = "a" * 64
    second_name = "a" * 63 + "b"

    first_alias = codex_dynamic_tool_name(first_name)
    second_alias = codex_dynamic_tool_name(second_name)

    assert first_alias == codex_dynamic_tool_name(first_name)
    assert first_alias != second_alias
    assert len(first_alias) == len(second_alias) == 64


@pytest.mark.asyncio
async def test_reserved_codex_tool_name_round_trips_as_public_name(monkeypatch):
    fake = _FakeAppServerProcess(
        tool_calls=[
            {
                "callId": "exec-search",
                "tool": WEB_SEARCH_CODEX_TOOL,
                "arguments": {"query": "HASHI alias regression"},
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
        [{"role": "user", "content": "Search for HASHI alias regression"}],
        [WEB_SEARCH_TOOL],
        "req-reserved-name",
        tool_choice="auto",
    )

    assert response.is_success is True
    assert fake.thread_params["dynamicTools"][0]["name"] == WEB_SEARCH_CODEX_TOOL
    assert (
        "'web_search' -> 'hashi_ext_web_search'"
        in fake.thread_params["developerInstructions"]
    )
    assert response.tool_calls[0]["function"]["name"] == "web_search"


@pytest.mark.asyncio
async def test_codex_app_server_captures_all_calls_without_executing_them(monkeypatch):
    fake = _FakeAppServerProcess(
        tool_calls=[
            {
                "callId": "exec-weather",
                "tool": WEATHER_CODEX_TOOL,
                "arguments": {"city": "Sydney"},
            },
            {
                "callId": "exec-timezone",
                "tool": TIMEZONE_CODEX_TOOL,
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
        WEATHER_CODEX_TOOL,
        TIMEZONE_CODEX_TOOL,
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
                "tool": WEATHER_CODEX_TOOL,
                "arguments": {"city": "Sydney"},
            },
            {
                "callId": "exec-timezone",
                "tool": TIMEZONE_CODEX_TOOL,
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
                "tool": WEATHER_CODEX_TOOL,
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
                "tool": TIMEZONE_CODEX_TOOL,
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
        TIMEZONE_CODEX_TOOL
    ]
    assert repr(TIMEZONE_CODEX_TOOL) in fake.thread_params["developerInstructions"]
    assert response.tool_calls[0]["function"]["name"] == "get_timezone"
    assert response.tool_calls[0]["function"]["arguments"] == '{"city":"Sydney"}'


@pytest.mark.asyncio
async def test_codex_app_server_rejects_public_name_instead_of_internal_alias(
    monkeypatch,
):
    fake = _FakeAppServerProcess(
        tool_calls=[
            {
                "callId": "exec-weather",
                "tool": "get_weather",
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
        [{"role": "user", "content": "Check Sydney"}],
        [WEATHER_TOOL],
        "req-public-name",
    )

    assert response.is_success is False
    assert response.tool_calls is None
    assert "undeclared caller-owned dynamic tool" in response.error


@pytest.mark.asyncio
async def test_codex_app_server_allows_recovery_from_native_tool_item(monkeypatch):
    fake = _FakeAppServerProcess(
        forbidden_item_type="collabAgentToolCall",
        final_text="Continued without native collaboration.",
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
        [{"role": "user", "content": "Read a local file"}],
        [WEATHER_TOOL],
        "req-local-tool",
    )

    assert response.is_success is True
    assert response.text == "Continued without native collaboration."
    assert not any(item.get("method") == "turn/interrupt" for item in fake.received)


@pytest.mark.asyncio
async def test_codex_app_server_returns_recoverable_error_for_unknown_host_method(
    monkeypatch,
):
    fake = _FakeAppServerProcess(
        host_method="collaboration/spawn",
        final_text="Continued after the host method was unavailable.",
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
        [{"role": "user", "content": "Delegate this if possible"}],
        [WEATHER_TOOL],
        "req-unknown-host-method",
    )

    assert response.is_success is True
    assert response.text == "Continued after the host method was unavailable."
    host_response = next(
        item for item in fake.received if item.get("id") == "unsupported-host-call"
    )
    assert host_response["error"]["code"] == -32601
    assert "Continue without it" in host_response["error"]["message"]
    assert not any(item.get("method") == "turn/interrupt" for item in fake.received)
