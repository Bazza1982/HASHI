import asyncio
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from adapters import deepseek_api
from adapters.deepseek_api import DeepSeekAdapter
from adapters.openrouter_api import _APIResult, _backend_failure_response
from adapters.stream_events import KIND_SHELL_EXEC, KIND_TOOL_END
from orchestrator.enterprise import IdentityService, PolicyEvaluator
from orchestrator.multimodal_contract import canonical_request_content
from tools.registry import ToolResult


class _DummyToolRegistry:
    max_loops = 2

    def __init__(self):
        self.calls = []
        self.policy_denials = []

    def get_tool_definitions(self, tiers=None):
        if tiers == []:
            return []
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
        self.calls.append((tool_name, arguments, tool_call_id))
        return ToolResult(tool_call_id=tool_call_id, output="tool output")

    async def record_policy_denial(
        self,
        tool_name,
        arguments,
        tool_call_id,
        *,
        output,
        decision,
    ):
        self.policy_denials.append(
            (tool_name, arguments, tool_call_id, decision)
        )
        return ToolResult(
            tool_call_id=tool_call_id,
            output=output,
            is_error=True,
            details={"control_disposition": decision},
        )


def _adapter(tmp_path, *, global_config=None, model="deepseek-v4-pro"):
    cfg = SimpleNamespace(
        name="ying",
        engine="deepseek-api",
        model=model,
        workspace_dir=tmp_path,
        system_md=None,
        extra={},
    )
    adapter = DeepSeekAdapter(cfg, global_config or SimpleNamespace(), api_key="test-key")
    adapter.tool_registry = _DummyToolRegistry()
    return adapter


@pytest.mark.asyncio
async def test_deepseek_vision_model_receives_native_image_content(tmp_path):
    image = tmp_path / "vision.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nvision")
    payload = image.read_bytes()
    content = canonical_request_content(
        [
            {"type": "text", "item_index": 1, "text": "Inspect it."},
            {
                "type": "media",
                "item_index": 2,
                "attachment_id": "attachment-vision",
                "modality": "image",
                "kind": "photo",
                "mime_type": "image/png",
                "filename": image.name,
                "caption": "",
                "local_ref": str(image),
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "transport": {"message_id": 2},
            },
        ]
    )
    adapter = _adapter(tmp_path, model="deepseek-v4-flash-vision-exp")
    adapter._call_api_once = AsyncMock(
        return_value=_APIResult("seen", None, "stop", 10, 2)
    )

    response = await adapter.generate_response(
        "Inspect it.", "request-deepseek-vision", request_content=content
    )

    assert response.is_success is True
    outbound = adapter._call_api_once.call_args.args[0]
    user_content = outbound["messages"][1]["content"]
    assert [part["type"] for part in user_content] == ["text", "image_url"]
    assert user_content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    tool_names = {
        item["function"]["name"] for item in outbound.get("tools", [])
    }
    assert tool_names.isdisjoint({"media_read", "vision_inspect"})


def _init_org(tmp_path, org_id: str = "ORG-001") -> None:
    identity = IdentityService.from_path(tmp_path / "state" / "enterprise.sqlite")
    identity.create_organization(org_id=org_id, name="Acme")


def _enterprise_global_config(tmp_path):
    return SimpleNamespace(
        deployment_profile="enterprise",
        organization_id="ORG-001",
        bridge_home=tmp_path,
    )


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


def test_deepseek_cache_helper_supports_old_api_result_shape():
    class OldAPIResult:
        pass

    result = deepseek_api._with_deepseek_cache_usage(
        OldAPIResult(),
        {
            "prompt_cache_hit_tokens": 123,
            "prompt_cache_miss_tokens": "45",
        },
    )

    assert result.prompt_cache_hit_tokens == 123
    assert result.prompt_cache_miss_tokens == 45


@pytest.mark.asyncio
async def test_deepseek_non_stream_captures_prompt_cache_usage(tmp_path):
    adapter = _adapter(tmp_path)

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "message": {"content": "done"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 7,
                    "prompt_cache_hit_tokens": 80,
                    "prompt_cache_miss_tokens": 20,
                },
            }

    adapter.client = SimpleNamespace(post=AsyncMock(return_value=_Response()))

    result = await adapter._call_api_once({}, {}, None)

    assert result.prompt_tokens == 100
    assert result.prompt_cache_hit_tokens == 80
    assert result.prompt_cache_miss_tokens == 20


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (400, "PROVIDER_BAD_REQUEST", False),
        (401, "PROVIDER_AUTHENTICATION_FAILED", False),
        (403, "PROVIDER_PERMISSION_DENIED", False),
        (408, "PROVIDER_REQUEST_TIMEOUT", True),
        (429, "PROVIDER_RATE_LIMITED", True),
        (500, "PROVIDER_SERVER_ERROR", True),
        (503, "PROVIDER_SERVER_ERROR", True),
    ],
)
def test_openai_compatible_http_failures_are_typed(status, code, retryable):
    request = httpx.Request("POST", "https://provider.invalid/v1/chat")
    response = httpx.Response(
        status,
        request=request,
        headers={"x-request-id": "provider-123", "retry-after": "2"},
    )
    error = httpx.HTTPStatusError(
        f"HTTP {status}", request=request, response=response
    )

    result = _backend_failure_response(error, duration_ms=12.5)

    assert result.is_success is False
    assert result.error_code == code
    assert result.error_retryable is retryable
    assert result.http_status == status
    assert result.provider_request_id == "provider-123"
    assert result.retry_after_s == 2.0


def test_openai_compatible_connection_and_stream_failures_are_typed():
    request = httpx.Request("POST", "https://provider.invalid/v1/chat")

    connection = _backend_failure_response(
        httpx.ConnectError("connection reset", request=request),
        duration_ms=1,
    )
    incomplete = _backend_failure_response(
        httpx.RemoteProtocolError("peer closed stream"),
        duration_ms=2,
    )
    timeout = _backend_failure_response(
        httpx.ReadTimeout("provider silent", request=request),
        duration_ms=3,
    )
    invalid_url = _backend_failure_response(
        httpx.InvalidURL("invalid provider URL"),
        duration_ms=4,
    )
    tls = _backend_failure_response(
        httpx.ConnectError("TLS certificate verification failed", request=request),
        duration_ms=5,
    )

    assert connection.error_code == "PROVIDER_CONNECTION_FAILED"
    assert connection.error_retryable is True
    assert incomplete.error_code == "PROVIDER_INCOMPLETE_STREAM"
    assert incomplete.error_retryable is True
    assert timeout.error_code == "PROVIDER_REQUEST_TIMEOUT"
    assert timeout.error_retryable is True
    assert invalid_url.error_code == "PROVIDER_CONFIGURATION_ERROR"
    assert invalid_url.error_retryable is False
    assert tls.error_code == "PROVIDER_TLS_ERROR"
    assert tls.error_retryable is False


def test_stable_provider_capacity_code_is_typed_but_generic_400_is_not():
    request = httpx.Request("POST", "https://provider.invalid/v1/chat")
    capacity_response = httpx.Response(
        400,
        request=request,
        json={"error": {"code": "context_length_exceeded", "message": "large"}},
    )
    capacity_error = httpx.HTTPStatusError(
        "HTTP 400",
        request=request,
        response=capacity_response,
    )
    generic_response = httpx.Response(
        400,
        request=request,
        json={"error": {"message": "maximum context length was exceeded"}},
    )
    generic_error = httpx.HTTPStatusError(
        "HTTP 400",
        request=request,
        response=generic_response,
    )

    assert _backend_failure_response(
        capacity_error,
        duration_ms=1,
    ).error_code == "CONTEXT_CAPACITY_REJECTED"
    assert _backend_failure_response(
        generic_error,
        duration_ms=1,
    ).error_code == "PROVIDER_BAD_REQUEST"


@pytest.mark.parametrize(
    ("configured", "thinking", "effort"),
    [
        ("high", "enabled", "high"),
        ("medium", "enabled", "high"),
        ("xhigh", "enabled", "max"),
        ("max", "enabled", "max"),
        ("off", "disabled", None),
    ],
)
def test_deepseek_v4_payload_maps_provider_reasoning(configured, thinking, effort, tmp_path):
    adapter = _adapter(tmp_path)
    adapter.tool_registry = None
    adapter.config.extra = {"provider_reasoning": configured}

    payload = adapter._build_payload([{"role": "user", "content": "hello"}])

    assert payload["thinking"] == {"type": thinking}
    assert payload.get("reasoning_effort") == effort


@pytest.mark.asyncio
async def test_tool_cleanup_details_are_forwarded_in_the_tool_end_event(tmp_path):
    adapter = _adapter(tmp_path)

    async def execute_with_cleanup(tool_name, arguments, tool_call_id=""):
        del tool_name, arguments
        return ToolResult(
            tool_call_id=tool_call_id,
            output="tool output",
            details={
                "foreground_cleanup": {
                    "status": "normal_completion",
                    "process_reaped": True,
                }
            },
        )

    adapter.tool_registry.execute = execute_with_cleanup
    events = []

    async def capture(event):
        events.append(event)

    await adapter._run_tool_calls(
        [
            {
                "id": "call_cleanup",
                "type": "function",
                "function": {
                    "name": "file_list",
                    "arguments": '{"path":"/tmp"}',
                },
            }
        ],
        [],
        capture,
    )

    completed = next(event for event in events if event.kind == KIND_TOOL_END)
    assert completed.metadata["tool_result_details"]["foreground_cleanup"] == {
        "status": "normal_completion",
        "process_reaped": True,
    }


@pytest.mark.asyncio
async def test_tool_activity_emits_one_typed_start_with_structured_command(tmp_path):
    adapter = _adapter(tmp_path)
    events = []

    async def capture(event):
        events.append(event)

    await adapter._run_tool_calls(
        [
            {
                "id": "call_check",
                "type": "function",
                "function": {
                    "name": "bash",
                    "arguments": '{"command":"pytest -q tests/test_example.py"}',
                },
            }
        ],
        [],
        capture,
    )

    assert [event.kind for event in events] == [KIND_SHELL_EXEC, KIND_TOOL_END]
    assert events[0].metadata["command"] == "pytest -q tests/test_example.py"
    assert events[1].metadata["is_error"] is False


@pytest.mark.asyncio
async def test_cancelled_tool_forwards_cleanup_before_propagating_cancellation(
    tmp_path,
):
    adapter = _adapter(tmp_path)

    async def cancel_after_cleanup(tool_name, arguments, tool_call_id=""):
        del tool_name, arguments, tool_call_id
        cancellation = asyncio.CancelledError()
        cancellation.hashi_tool_details = {
            "foreground_cleanup": {
                "status": "terminated",
                "process_reaped": True,
            }
        }
        raise cancellation

    adapter.tool_registry.execute = cancel_after_cleanup
    events = []

    async def capture(event):
        events.append(event)

    with pytest.raises(asyncio.CancelledError):
        await adapter._run_tool_calls(
            [
                {
                    "id": "call_cancelled_cleanup",
                    "type": "function",
                    "function": {
                        "name": "bash",
                        "arguments": '{"command":"sleep 30"}',
                    },
                }
            ],
            [],
            capture,
        )

    completed = next(event for event in events if event.kind == KIND_TOOL_END)
    assert completed.metadata["tool_result_details"]["foreground_cleanup"] == {
        "status": "terminated",
        "process_reaped": True,
    }


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
                prompt_tokens=10,
                completion_tokens=4,
                thinking_tokens=3,
                prompt_cache_hit_tokens=6,
                prompt_cache_miss_tokens=4,
            )
        assistant_msg = payload["messages"][2]
        assert assistant_msg["reasoning_content"] == "Need to inspect the directory."
        return _APIResult(
            text="done",
            tool_calls=None,
            finish_reason="stop",
            prompt_tokens=20,
            completion_tokens=5,
            thinking_tokens=2,
            prompt_cache_hit_tokens=15,
            prompt_cache_miss_tokens=5,
        )

    monkeypatch.setattr(adapter, "_call_api_once", fake_call)

    response = await adapter.generate_response("check files", "req-test")

    assert response.is_success is True
    assert response.text == "done"
    assert response.tool_call_count == 1
    assert response.tool_loop_count == 1
    provider_calls = response.stream_metadata["meter"]["provider_calls"]
    assert [
        {
            key: value
            for key, value in call.items()
            if key != "provider_call_latency_ms"
        }
        for call in provider_calls
    ] == [
        {
            "input": 10,
            "output": 4,
            "thinking": 3,
            "token_source": "provider",
            "thinking_in_output": True,
            "cost_usd": None,
            "prompt_cache_hit_tokens": 6,
            "prompt_cache_miss_tokens": 4,
        },
        {
            "input": 20,
            "output": 5,
            "thinking": 2,
            "token_source": "provider",
            "thinking_in_output": True,
            "cost_usd": None,
            "prompt_cache_hit_tokens": 15,
            "prompt_cache_miss_tokens": 5,
        },
    ]
    assert all(
        isinstance(call["provider_call_latency_ms"], float)
        and call["provider_call_latency_ms"] >= 0
        for call in provider_calls
    )


@pytest.mark.asyncio
async def test_openrouter_tool_execution_blocks_shell_policy(tmp_path):
    _init_org(tmp_path)
    policy = PolicyEvaluator.from_path(tmp_path / "state" / "enterprise.sqlite", org_id="ORG-001")
    policy.add_rule(action="shell.execute", resource="shell:bash", effect="deny")
    adapter = _adapter(tmp_path, global_config=_enterprise_global_config(tmp_path))
    messages = []
    tool_calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "bash", "arguments": '{"command": "rm -rf /tmp/example"}'},
        }
    ]

    await adapter._run_tool_calls(tool_calls, messages, on_stream_event=None)

    assert adapter.tool_registry.calls == []
    assert adapter.tool_registry.policy_denials == [
        ("bash", {"command": "rm -rf /tmp/example"}, "call_1", "deny")
    ]
    assert messages == [
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": "Error: tool call blocked by enterprise policy: bash",
        }
    ]


@pytest.mark.asyncio
async def test_openrouter_tool_execution_blocks_file_write_approval_required(tmp_path):
    _init_org(tmp_path)
    policy = PolicyEvaluator.from_path(tmp_path / "state" / "enterprise.sqlite", org_id="ORG-001")
    policy.add_rule(action="file.write", resource="file:/tmp/report.md", effect="approval_required")
    adapter = _adapter(tmp_path, global_config=_enterprise_global_config(tmp_path))
    messages = []
    tool_calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "file_write", "arguments": '{"path": "/tmp/report.md", "content": "x"}'},
        }
    ]

    await adapter._run_tool_calls(tool_calls, messages, on_stream_event=None)

    assert adapter.tool_registry.calls == []
    assert adapter.tool_registry.policy_denials == [
        (
            "file_write",
            {"path": "/tmp/report.md", "content": "x"},
            "call_1",
            "approval_required",
        )
    ]
    assert messages[0]["content"] == "Error: tool call requires approval by enterprise policy: file_write"


@pytest.mark.asyncio
async def test_deepseek_ignores_retired_tool_loop_ceiling(monkeypatch, tmp_path):
    adapter = _adapter(tmp_path)
    adapter.tool_registry.max_loops = 1
    seen_payloads = []
    def tool_calls(index):
        return [
            {
                "id": f"call_{index}",
                "type": "function",
                "function": {
                    "name": "file_list",
                    "arguments": '{"path": "/tmp"}',
                },
            }
        ]

    async def fake_call(payload, headers, on_stream_event):
        seen_payloads.append(payload)
        assert "tools" in payload
        if len(seen_payloads) <= 2:
            return _APIResult(
                text="",
                tool_calls=tool_calls(len(seen_payloads)),
                finish_reason="tool_calls",
            )
        return _APIResult(text="final answer", tool_calls=None, finish_reason="stop")

    monkeypatch.setattr(adapter, "_call_api_once", fake_call)

    response = await adapter.generate_response("check files", "req-test")

    assert response.is_success is True
    assert response.text == "final answer"
    assert response.tool_call_count == 2
    assert response.tool_loop_count == 2
    assert len(seen_payloads) == 3


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


@pytest.mark.asyncio
async def test_deepseek_stream_waits_for_reasoning_capture_before_returning(tmp_path):
    adapter = _adapter(tmp_path)

    class _StreamResponse:
        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            yield 'data: {"choices":[{"delta":{"reasoning_content":"reason first"}}]}'
            yield (
                'data: {"choices":[{"delta":{"content":"result text"},'
                '"finish_reason":"stop"}],"usage":{"prompt_tokens":2,'
                '"completion_tokens":3,"prompt_cache_hit_tokens":1,'
                '"prompt_cache_miss_tokens":1}}'
            )
            yield "data: [DONE]"

    class _StreamContext:
        async def __aenter__(self):
            return _StreamResponse()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    adapter.client = SimpleNamespace(stream=lambda *args, **kwargs: _StreamContext())
    events = []

    async def capture(event):
        await asyncio.sleep(0.01)
        events.append((event.kind, event.raw_delta or event.summary))

    result = await adapter._stream_api_once({}, {}, capture)

    assert result.text == "result text"
    assert result.reasoning_content == "reason first"
    assert result.prompt_cache_hit_tokens == 1
    assert result.prompt_cache_miss_tokens == 1
    assert events == [
        ("thinking", "reason first"),
        ("text_delta", "result text"),
    ]
