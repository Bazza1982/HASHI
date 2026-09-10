import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from adapters import deepseek_api
from adapters.deepseek_api import DeepSeekAdapter
from adapters.openrouter_api import (
    _APIResult,
    _backend_failure_response,
    _tool_call_forensic_details,
)
from adapters.stream_events import (
    KIND_SHELL_EXEC,
    KIND_TEXT_DELTA,
    KIND_THINKING,
    KIND_TOOL_END,
    StreamEvent,
)
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("choice_fields", "expected_source"),
    [({}, "missing"), ({"finish_reason": None}, "provider_null")],
)
async def test_deepseek_non_stream_preserves_missing_finish_reason(
    tmp_path,
    choice_fields,
    expected_source,
):
    adapter = _adapter(tmp_path)

    class _Response:
        headers = {"x-request-id": "wire-response-1"}

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "id": "completion-1",
                "choices": [
                    {
                        "message": {"content": "possibly complete"},
                        **choice_fields,
                    }
                ],
            }

    adapter.client = SimpleNamespace(post=AsyncMock(return_value=_Response()))

    result = await adapter._call_api_once({}, {}, None)

    assert result.finish_reason is None
    assert result.raw_finish_reason is None
    assert result.finish_reason_present is ("finish_reason" in choice_fields)
    assert result.finish_reason_source == expected_source
    assert result.provider_response_id == "completion-1"
    assert result.transport_complete is True


@pytest.mark.asyncio
async def test_deepseek_stream_done_does_not_fabricate_missing_finish_reason(tmp_path):
    adapter = _adapter(tmp_path)

    class _StreamResponse:
        headers = {"x-request-id": "wire-stream-1"}

        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            yield 'data: {"id":"completion-stream-1","choices":[{"delta":{"content":"done"}}]}'
            yield "data: [DONE]"

    class _StreamContext:
        async def __aenter__(self):
            return _StreamResponse()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    adapter.client = SimpleNamespace(stream=lambda *args, **kwargs: _StreamContext())

    result = await adapter._stream_api_once({}, {}, None)

    assert result.text == "done"
    assert result.finish_reason is None
    assert result.finish_reason_present is False
    assert result.finish_reason_source == "missing"
    assert result.transport_complete is True
    assert result.stream_done is True
    assert result.stream_eof is False


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
    assert connection.stream_metadata["provider_http_failure"]["request"][
        "url"
    ] == str(request.url)
    assert incomplete.error_code == "PROVIDER_INCOMPLETE_STREAM"
    assert incomplete.error_retryable is True
    assert "request" not in incomplete.stream_metadata["provider_http_failure"]
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
    usage_fields = {
        "input",
        "output",
        "thinking",
        "token_source",
        "thinking_in_output",
        "cost_usd",
        "prompt_cache_hit_tokens",
        "prompt_cache_miss_tokens",
    }
    assert [
        {
            key: value
            for key, value in call.items()
            if key in usage_fields
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
    assert [call["status"] for call in provider_calls] == [
        "completed",
        "completed",
    ]
    assert len({call["provider_request_id"] for call in provider_calls}) == 2


@pytest.mark.asyncio
async def test_explicit_stop_with_tool_calls_is_audited_conflict_without_side_effect(
    monkeypatch,
    tmp_path,
):
    adapter = _adapter(tmp_path)
    observed = []
    adapter.set_provider_call_observer(observed.append)
    tool_calls = [
        {
            "id": "call_conflict",
            "type": "function",
            "function": {
                "name": "file_list",
                "arguments": '{"path": "/tmp"}',
            },
        }
    ]
    provider_calls = 0

    async def fake_call(payload, headers, on_stream_event):
        nonlocal provider_calls
        provider_calls += 1
        return _APIResult(
            text="I am stopping now.",
            tool_calls=tool_calls,
            finish_reason="stop",
        )

    monkeypatch.setattr(adapter, "_call_api_once", fake_call)

    response = await adapter.generate_response("inspect", "req-stop-conflict")

    assert response.is_success is False
    assert response.error_code == "PROVIDER_FINISH_REASON_CONFLICT"
    assert response.error_retryable is False
    assert response.text == "I am stopping now."
    assert response.tool_call_count == 0
    assert adapter.tool_registry.calls == []
    assert provider_calls == 1
    assert observed[0]["raw_finish_reason"] == "stop"
    assert observed[0]["finish_reason_source"] == "provider"
    assert observed[0]["decision"] == "protocol_conflict"
    assert observed[0]["decision_reason"] == "stop_with_tool_calls"
    assert observed[0]["tool_calls"] == [
        {
            "id": "call_conflict",
            "name": "file_list",
            "complete": True,
            "arguments_state": "valid_object",
        }
    ]


@pytest.mark.asyncio
async def test_missing_finish_reason_is_unknown_failure_not_natural_stop(
    monkeypatch,
    tmp_path,
):
    adapter = _adapter(tmp_path)
    observed = []
    adapter.set_provider_call_observer(observed.append)

    async def fake_call(payload, headers, on_stream_event):
        return _APIResult(
            text="answer without a terminal field",
            tool_calls=None,
            finish_reason=None,
            finish_reason_present=False,
            raw_finish_reason=None,
            finish_reason_source="missing",
        )

    monkeypatch.setattr(adapter, "_call_api_once", fake_call)

    response = await adapter.generate_response("answer", "req-missing-finish")

    assert response.is_success is False
    assert response.error_code == "PROVIDER_MISSING_FINISH_REASON"
    assert response.error_retryable is False
    assert response.stop_reason == "missing_finish_reason"
    assert observed[0]["raw_finish_reason_present"] is False
    assert observed[0]["raw_finish_reason"] is None
    assert observed[0]["normalized_finish_reason"] == "missing_finish_reason"
    assert observed[0]["decision"] == "reject_missing_finish_reason"


@pytest.mark.asyncio
async def test_truncation_never_executes_accumulated_tool_calls(monkeypatch, tmp_path):
    adapter = _adapter(tmp_path)
    tool_calls = [
        {
            "id": "call_truncated",
            "type": "function",
            "function": {
                "name": "file_list",
                "arguments": '{"path": "/tmp"}',
            },
        }
    ]

    async def fake_call(payload, headers, on_stream_event):
        return _APIResult("partial", tool_calls, "length")

    monkeypatch.setattr(adapter, "_call_api_once", fake_call)

    response = await adapter.generate_response("inspect", "req-length")

    assert response.is_success is False
    assert response.error_code == "PROVIDER_OUTPUT_TRUNCATED"
    assert response.stop_reason == "length"
    assert response.tool_call_count == 0
    assert adapter.tool_registry.calls == []


@pytest.mark.asyncio
async def test_incomplete_tool_arguments_repair_in_place_before_any_side_effect(
    monkeypatch,
    tmp_path,
):
    adapter = _adapter(tmp_path)
    observed = []
    adapter.set_provider_call_observer(observed.append)
    incomplete = [
        {
            "id": "call_partial",
            "type": "function",
            "function": {
                "name": "file_list",
                "arguments": '{"path":',
            },
        }
    ]

    repaired = [
        {
            "id": "call_partial",
            "type": "function",
            "function": {
                "name": "file_list",
                "arguments": '{"path":"/tmp"}',
            },
        }
    ]
    replies = iter(
        [
            _APIResult(
                "",
                incomplete,
                "tool_calls",
                provider_response_id="provider-bad-1",
            ),
            _APIResult(
                "",
                repaired,
                "tool_calls",
                provider_response_id="provider-repaired-1",
            ),
            _APIResult(
                "done",
                None,
                "stop",
                provider_response_id="provider-final-1",
            ),
        ]
    )
    payloads = []

    async def fake_call(payload, headers, on_stream_event):
        payloads.append(payload)
        return next(replies)

    monkeypatch.setattr(adapter, "_call_api_once", fake_call)

    response = await adapter.generate_response("inspect", "req-partial-tool")

    assert response.is_success is True
    assert response.text == "done"
    assert response.tool_call_count == 1
    assert adapter.tool_registry.calls == [
        ("file_list", {"path": "/tmp"}, "call_partial")
    ]
    assert len(payloads) == 3
    assert any(
        "repair request 1/3" in message.get("content", "")
        for message in payloads[1]["messages"]
        if message.get("role") == "system"
    )
    assert observed[0]["decision"] == "reject_invalid_tool_calls"
    assert observed[0]["tool_calls"][0]["complete"] is False
    assert observed[0]["tool_calls"][0]["arguments_state"] == "invalid_json"
    assert observed[0]["tool_call_repair"]["next_request"] == "1/3"
    forensic_path = response.stream_metadata["provider_protocol_forensic_path"]
    records = [
        json.loads(line)
        for line in Path(forensic_path).read_text(encoding="utf-8").splitlines()
    ]
    assert records[0]["tool_calls"][0]["parser_input"] == '{"path":'
    assert records[0]["tool_calls"][0]["parser_error"]["type"] == "JSONDecodeError"
    assert records[0]["raw_provider"]["transport"] == "test_double"


@pytest.mark.asyncio
async def test_invalid_tool_arguments_succeed_on_third_repair_without_replay(
    monkeypatch,
    tmp_path,
):
    adapter = _adapter(tmp_path)
    first_tool = [
        {
            "id": "call_first",
            "type": "function",
            "function": {
                "name": "file_list",
                "arguments": '{"path":"/first"}',
            },
        }
    ]
    bad_tool = [
        {
            "id": "call_second",
            "type": "function",
            "function": {"name": "file_list", "arguments": '{"path":'},
        }
    ]
    repaired_tool = [
        {
            "id": "call_second",
            "type": "function",
            "function": {
                "name": "file_list",
                "arguments": '{"path":"/second"}',
            },
        }
    ]
    replies = iter(
        [
            _APIResult("", first_tool, "tool_calls", provider_response_id="p-1"),
            _APIResult("", bad_tool, "tool_calls", provider_response_id="p-bad-0"),
            _APIResult("", bad_tool, "tool_calls", provider_response_id="p-bad-1"),
            _APIResult("", bad_tool, "tool_calls", provider_response_id="p-bad-2"),
            _APIResult("", repaired_tool, "tool_calls", provider_response_id="p-good-3"),
            _APIResult("complete", None, "stop", provider_response_id="p-final"),
        ]
    )
    payloads = []

    async def fake_call(payload, headers, on_stream_event):
        payloads.append(payload)
        return next(replies)

    monkeypatch.setattr(adapter, "_call_api_once", fake_call)

    response = await adapter.generate_response("inspect", "req-third-repair")

    assert response.is_success is True
    assert adapter.tool_registry.calls == [
        ("file_list", {"path": "/first"}, "call_first"),
        ("file_list", {"path": "/second"}, "call_second"),
    ]
    repair_prompts = [
        message["content"]
        for payload in payloads
        for message in payload["messages"]
        if message.get("role") == "system"
        and "tool-call repair request" in message.get("content", "")
    ]
    assert any("repair request 1/3" in prompt for prompt in repair_prompts)
    assert any("repair request 2/3" in prompt for prompt in repair_prompts)
    assert any("repair request 3/3" in prompt for prompt in repair_prompts)
    assert all("call_first" in prompt for prompt in repair_prompts)


@pytest.mark.asyncio
async def test_invalid_tool_arguments_exhaust_three_repairs_with_precise_error(
    monkeypatch,
    tmp_path,
):
    adapter = _adapter(tmp_path)
    import os
    import time
    forensic_root = tmp_path / 'logs' / 'provider_protocol_forensics'
    forensic_root.mkdir(parents=True)
    expired = forensic_root / 'invalid-tool-calls-expired.jsonl'
    expired.write_text('old private evidence\n')
    os.utime(expired, (time.time() - 8 * 86400,) * 2)
    unrelated = forensic_root / 'unrelated.jsonl'
    unrelated.write_text('keep\n')
    bad_tool = [
        {
            "id": "call_broken",
            "type": "function",
            "function": {"name": "file_list", "arguments": '{"path":'},
        }
    ]
    provider_ids = iter(["bad-initial", "bad-repair-1", "bad-repair-2", "bad-repair-3"])
    call_count = 0

    async def fake_call(payload, headers, on_stream_event):
        nonlocal call_count
        call_count += 1
        return _APIResult(
            "",
            bad_tool,
            "tool_calls",
            provider_response_id=next(provider_ids),
        )

    monkeypatch.setattr(adapter, "_call_api_once", fake_call)

    response = await adapter.generate_response("inspect", "req-exhaust-repair")

    assert call_count == 4
    assert response.is_success is False
    assert response.error_code == "PROVIDER_INVALID_TOOL_CALLS"
    assert response.error_retryable is False
    assert adapter.tool_registry.calls == []
    assert "file_list" in response.error
    assert "3 repair attempts" in response.error
    assert "bad-repair-3" in response.error
    forensic_path = Path(
        response.stream_metadata["provider_protocol_forensic_path"]
    )
    assert str(forensic_path) in response.error
    if os.name == 'nt':
        import subprocess
        acl = subprocess.run(['icacls.exe', str(forensic_path)], capture_output=True, text=True, check=True)
        assert '(I)' not in acl.stdout, 'private protocol evidence must not inherit readers'
    else:
        assert forensic_path.stat().st_mode & 0o777 == 0o600
    assert not expired.exists(), 'expired private raw evidence must be removed'
    assert unrelated.read_text() == 'keep\n'
    records = [
        json.loads(line)
        for line in forensic_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(records) == 4
    assert [row["repair"]["next_request"] for row in records] == [
        "1/3",
        "2/3",
        "3/3",
        None,
    ]
    assert records[-1]["repair"]["status"] == "exhausted"


@pytest.mark.asyncio
async def test_invalid_mixed_tool_batch_executes_only_the_repaired_batch(
    monkeypatch,
    tmp_path,
):
    adapter = _adapter(tmp_path)
    initial = [
        {
            "id": "call_valid",
            "type": "function",
            "function": {
                "name": "file_list",
                "arguments": '{"path":"/valid"}',
            },
        },
        {
            "id": "call_bad",
            "type": "function",
            "function": {"name": "file_list", "arguments": '{"path":'},
        },
    ]
    repaired = [
        initial[0],
        {
            "id": "call_bad",
            "type": "function",
            "function": {
                "name": "file_list",
                "arguments": '{"path":"/repaired"}',
            },
        },
    ]
    replies = iter(
        [
            _APIResult("", initial, "tool_calls"),
            _APIResult("", repaired, "tool_calls"),
            _APIResult("done", None, "stop"),
        ]
    )

    async def fake_call(payload, headers, on_stream_event):
        return next(replies)

    monkeypatch.setattr(adapter, "_call_api_once", fake_call)

    response = await adapter.generate_response("inspect", "req-mixed-repair")

    assert response.is_success is True
    assert adapter.tool_registry.calls == [
        ("file_list", {"path": "/valid"}, "call_valid"),
        ("file_list", {"path": "/repaired"}, "call_bad"),
    ]


def test_tool_forensic_evidence_distinguishes_provider_json_from_assembly_damage():
    tool_calls = [
        {
            "id": "call-1",
            "type": "function",
            "function": {"name": "file_list", "arguments": '{"path":}'},
        }
    ]
    raw_fragments = [
        {
            "arrival": 1,
            "index": 0,
            "arguments_fragment": '{"path":',
        },
        {
            "arrival": 2,
            "index": 0,
            "arguments_fragment": '"/tmp"}',
        },
    ]

    detail = _tool_call_forensic_details(
        tool_calls,
        argument_fragments=raw_fragments,
    )[0]

    assert detail["provider_arguments_from_fragments"] == '{"path":"/tmp"}'
    assert detail["parser_input"] == '{"path":}'
    assert detail["assembly_matches_provider_fragments"] is False
    assert detail["attribution"] == "hashi_assembly_mismatch"


@pytest.mark.asyncio
async def test_streamed_tool_repair_preserves_every_sse_fragment(tmp_path):
    adapter = _adapter(tmp_path)
    streams = iter(
        [
            [
                'data: {"id":"bad-stream","choices":[{"delta":{"tool_calls":['
                '{"index":0,"id":"call-stream","type":"function","function":'
                '{"name":"file_list","arguments":"{\\"path\\":"}}]},'
                '"finish_reason":null}]}',
                'data: {"id":"bad-stream","choices":[{"delta":{"tool_calls":['
                '{"index":0,"function":{"arguments":"}"}}]},'
                '"finish_reason":"tool_calls"}]}',
                "data: [DONE]",
            ],
            [
                'data: {"id":"good-stream","choices":[{"delta":{"tool_calls":['
                '{"index":0,"id":"call-stream","type":"function","function":'
                '{"name":"file_list","arguments":"{\\"path\\":\\"/tmp\\"}"}}]},'
                '"finish_reason":"tool_calls"}]}',
                "data: [DONE]",
            ],
            [
                'data: {"id":"final-stream","choices":[{"delta":{"content":"done"},'
                '"finish_reason":"stop"}]}',
                "data: [DONE]",
            ],
        ]
    )

    class _StreamResponse:
        headers = {"x-request-id": "wire-stream"}

        def __init__(self, lines):
            self.lines = lines

        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            for line in self.lines:
                yield line

    class _StreamContext:
        def __init__(self, lines):
            self.response = _StreamResponse(lines)

        async def __aenter__(self):
            return self.response

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    adapter.client = SimpleNamespace(
        stream=lambda *args, **kwargs: _StreamContext(next(streams))
    )

    async def capture(_event):
        return None

    response = await adapter.generate_response(
        "inspect",
        "req-stream-repair",
        on_stream_event=capture,
    )

    assert response.is_success is True
    assert adapter.tool_registry.calls == [
        ("file_list", {"path": "/tmp"}, "call-stream")
    ]
    records = [
        json.loads(line)
        for line in Path(
            response.stream_metadata["provider_protocol_forensic_path"]
        ).read_text(encoding="utf-8").splitlines()
    ]
    raw = records[0]["raw_provider"]
    assert len(raw["sse_events"]) == 3
    assert [
        fragment["arguments_fragment"]
        for fragment in raw["tool_call_fragments"]
    ] == ['{"path":', "}"]
    assert records[0]["tool_calls"][0]["parser_input"] == '{"path":}'
    assert records[0]["tool_calls"][0]["attribution"] == "provider_invalid_json"


@pytest.mark.asyncio
async def test_model_text_claiming_stop_does_not_override_valid_tool_signal(
    monkeypatch,
    tmp_path,
):
    adapter = _adapter(tmp_path)
    tool_calls = [
        {
            "id": "call_structured",
            "type": "function",
            "function": {
                "name": "file_list",
                "arguments": '{"path": "/tmp"}',
            },
        }
    ]
    replies = iter(
        [
            _APIResult("I must stop.", tool_calls, "tool_calls"),
            _APIResult("finished", None, "stop"),
        ]
    )

    async def fake_call(payload, headers, on_stream_event):
        return next(replies)

    monkeypatch.setattr(adapter, "_call_api_once", fake_call)

    response = await adapter.generate_response("inspect", "req-structured-wins")

    assert response.is_success is True
    assert response.text == "finished"
    assert adapter.tool_registry.calls == [
        ("file_list", {"path": "/tmp"}, "call_structured")
    ]


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
async def test_deepseek_retries_only_the_unfinished_call_after_completed_tool_loop(
    monkeypatch,
    tmp_path,
):
    adapter = _adapter(tmp_path)
    adapter.TRANSIENT_PROVIDER_CALL_RETRY_DELAY_S = 0
    seen_messages = []
    observed_provider_calls = []
    adapter.set_provider_call_observer(observed_provider_calls.append)
    tool_calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "file_list", "arguments": '{"path": "/tmp"}'},
        }
    ]

    async def fake_stream(payload, headers, on_stream_event):
        seen_messages.append(repr(payload["messages"]))
        if len(seen_messages) == 1:
            return _APIResult("", tool_calls, "tool_calls")
        if len(seen_messages) == 2:
            await on_stream_event(
                StreamEvent(kind=KIND_THINKING, summary="finishing safely")
            )
            raise httpx.RemoteProtocolError("peer closed the final stream")
        return _APIResult("done", None, "stop")

    events = []

    async def capture(event):
        events.append(event.kind)

    monkeypatch.setattr(adapter, "_stream_api_once", fake_stream)

    response = await adapter.generate_response(
        "check files",
        "req-transport-retry",
        on_stream_event=capture,
    )

    assert response.is_success is True
    assert response.text == "done"
    assert len(seen_messages) == 3
    assert seen_messages[1] == seen_messages[2]
    assert adapter.tool_registry.calls == [
        ("file_list", {"path": "/tmp"}, "call_1")
    ]
    assert events.count(KIND_THINKING) == 1
    assert response.stream_metadata["provider_transport_retry_count"] == 1
    provider_calls = response.stream_metadata["meter"]["provider_calls"]
    assert provider_calls == observed_provider_calls
    assert [call["status"] for call in provider_calls] == [
        "completed",
        "failed_without_receipt",
        "completed",
    ]
    assert [call["retry_count"] for call in provider_calls] == [0, 0, 1]
    assert provider_calls[-1]["recovery_kind"] == "provider_transport_retry"
    assert len({call["provider_request_id"] for call in provider_calls}) == 3


@pytest.mark.asyncio
async def test_deepseek_does_not_retry_after_partial_answer_text(monkeypatch, tmp_path):
    adapter = _adapter(tmp_path)
    adapter.TRANSIENT_PROVIDER_CALL_RETRY_DELAY_S = 0
    calls = 0

    async def fake_stream(payload, headers, on_stream_event):
        nonlocal calls
        calls += 1
        await on_stream_event(StreamEvent(kind=KIND_TEXT_DELTA, summary="partial"))
        raise httpx.RemoteProtocolError("peer closed after answer text")

    async def capture(_event):
        return None

    monkeypatch.setattr(adapter, "_stream_api_once", fake_stream)

    response = await adapter.generate_response(
        "answer",
        "req-partial-text",
        on_stream_event=capture,
    )

    assert response.is_success is False
    assert response.error_code == "PROVIDER_INCOMPLETE_STREAM"
    assert response.stream_metadata["provider_transport_retry_count"] == 0
    assert calls == 1


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


@pytest.mark.asyncio
async def test_interrupted_stream_audits_partial_protocol_before_return(tmp_path):
    adapter = _adapter(tmp_path)
    observed = []
    adapter.set_provider_call_observer(observed.append)

    class _StreamResponse:
        headers = {"x-request-id": "wire-partial-1"}

        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            yield (
                'data: {"id":"completion-partial-1","choices":['
                '{"delta":{"content":"partial"},"finish_reason":null}]}'
            )
            raise httpx.RemoteProtocolError("wire ended")

    class _StreamContext:
        async def __aenter__(self):
            return _StreamResponse()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    adapter.client = SimpleNamespace(stream=lambda *args, **kwargs: _StreamContext())

    async def capture(_event):
        return None

    response = await adapter.generate_response(
        "answer",
        "req-partial-protocol",
        on_stream_event=capture,
    )

    assert response.is_success is False
    assert response.error_code == "PROVIDER_INCOMPLETE_STREAM"
    assert len(observed) == 1
    assert observed[0]["status"] == "failed_after_partial_response"
    assert observed[0]["provider_response_id"] == "completion-partial-1"
    assert observed[0]["transport_request_id"] == "wire-partial-1"
    assert observed[0]["raw_finish_reason_present"] is False
    assert observed[0]["finish_reason_source"] == "provider_null"
    assert observed[0]["text_length"] == 7
    assert observed[0]["transport_complete"] is False
    assert observed[0]["stream_truncated"] is True
    assert observed[0]["decision"] == "return_provider_failure"


@pytest.mark.asyncio
async def test_cancelled_stream_audits_observed_tool_fragment_without_execution(
    tmp_path,
):
    adapter = _adapter(tmp_path)
    observed = []
    adapter.set_provider_call_observer(observed.append)

    class _StreamResponse:
        headers = {"x-request-id": "wire-cancelled-1"}

        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            yield (
                'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
                '"id":"call-cancelled","type":"function","function":'
                '{"name":"file_list","arguments":"{\\"path\\":"}}]}}]}'
            )
            raise asyncio.CancelledError

    class _StreamContext:
        async def __aenter__(self):
            return _StreamResponse()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    adapter.client = SimpleNamespace(stream=lambda *args, **kwargs: _StreamContext())

    async def capture(_event):
        return None

    with pytest.raises(asyncio.CancelledError):
        await adapter.generate_response(
            "inspect",
            "req-cancelled-protocol",
            on_stream_event=capture,
        )

    assert adapter.tool_registry.calls == []
    assert len(observed) == 1
    assert observed[0]["status"] == "cancelled"
    assert observed[0]["decision"] == "cancel"
    assert observed[0]["transport_state"] == "cancelled"
    assert observed[0]["tool_calls"][0]["id"] == "call-cancelled"
    assert observed[0]["tool_calls"][0]["complete"] is False
