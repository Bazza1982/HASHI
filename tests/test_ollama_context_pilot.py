from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest

from adapters.ollama_api import OllamaAdapter
from adapters.openrouter_api import (
    ProviderContextCapacityPreflightError,
    _provider_response_decision,
)


def _adapter(tmp_path, *, enabled=True, capacity=49152, url=None):
    extra = {"ollama_native_context": enabled}
    if capacity is not None:
        extra["model_capabilities"] = {
            "local-qwen": {
                "context_window_tokens": capacity,
                "response_headroom_tokens": 8192,
            }
        }
    if url is not None:
        extra["ollama_url"] = url
    config = SimpleNamespace(
        name="pilot",
        engine="ollama-api",
        model="local-qwen",
        workspace_dir=tmp_path,
        system_md=None,
        extra=extra,
    )
    return OllamaAdapter(config, SimpleNamespace())


@pytest.mark.asyncio
@pytest.mark.parametrize("native_context", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("reasoning,think", [("enabled", True), ("off", False)])
async def test_boolean_reasoning_uses_native_chat_and_survives_tool_continuation(
    tmp_path, native_context, streaming, reasoning, think
):
    adapter = _adapter(tmp_path, enabled=native_context, capacity=49152 if native_context else None)
    adapter.config.extra["provider_reasoning"] = reasoning
    messages = [
        {"role": "user", "content": "read the file"},
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "read-1", "type": "function",
            "function": {"name": "file_read", "arguments": '{"path":"a.txt"}'},
        }]},
        {"role": "tool", "tool_call_id": "read-1", "content": "file contents"},
    ]
    original = deepcopy(messages)
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={
            "message": {"content": "answer", "thinking": "checking" if think else ""},
            "done": True, "done_reason": "stop", "prompt_eval_count": 100, "eval_count": 3,
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter.client = client
        payload = adapter._build_payload(messages, use_streaming=streaming)
        method = adapter._stream_api_once if streaming else adapter._call_api_once
        result = await method(payload, adapter._request_headers(), None)

    assert requests[0].url.path == "/api/chat"
    sent = json.loads(requests[0].content)
    assert sent["think"] is think
    assert "reasoning_effort" not in sent
    if native_context:
        assert sent["options"]["num_predict"] == 8192
    else:
        assert "num_predict" not in sent["options"] and "num_ctx" not in sent["options"]
    assert sent["messages"][-1]["tool_name"] == "file_read"
    assert sent["messages"][-2]["tool_calls"][0]["function"]["arguments"] == {"path": "a.txt"}
    assert messages == original
    assert result.text == "answer"
    assert result.reasoning_content == ("checking" if think else "")
    assert result.provider_compatibility["native_request_controls"]["think"] is think


def test_direct_boolean_effort_is_independent_of_display_and_generation_budget(tmp_path):
    adapter = _adapter(tmp_path, enabled=False, capacity=None)
    adapter.set_reasoning_enabled(True)
    adapter.effort = "off"
    disabled = adapter._build_payload([{"role": "user", "content": "task"}])
    adapter.effort = "enabled"
    enabled = adapter._build_payload([{"role": "user", "content": "task"}])
    assert disabled["think"] is False
    assert enabled["think"] is True
    assert disabled["options"] == enabled["options"] == {}
    adapter.effort = "low"
    with pytest.raises(ValueError, match="enabled/off"):
        adapter._build_payload([{"role": "user", "content": "task"}])


def test_native_context_is_an_instance_opt_in(tmp_path):
    adapter = _adapter(tmp_path, enabled=False)
    payload = adapter._build_payload([{"role": "user", "content": "task"}])

    assert adapter._chat_completions_url().endswith("/v1/chat/completions")
    assert payload["options"] == {"num_ctx": 32768}
    assert "truncate" not in payload


@pytest.mark.parametrize("capacity", [49152, 65536])
def test_native_context_uses_declared_capacity_and_no_server_truncation(tmp_path, capacity):
    adapter = _adapter(
        tmp_path, capacity=capacity, url="http://local.invalid/ollama/v1/chat/completions"
    )
    payload = adapter._build_payload(
        [{"role": "user", "content": "task"}], use_streaming=True
    )

    assert adapter._chat_completions_url() == "http://local.invalid/ollama/api/chat"
    assert payload["options"] == {"num_ctx": capacity, "num_predict": 8192}
    assert payload["truncate"] is False
    assert payload["stream"] is True


def test_native_context_unknown_capacity_uses_server_defaults_without_claim(tmp_path):
    adapter = _adapter(tmp_path, capacity=None)

    payload = adapter._build_payload([{"role": "user", "content": "task"}])
    assert payload["options"] == {}
    assert payload["truncate"] is False
    assert adapter._native_capacity() is None


def _small_model_options(adapter):
    options = adapter.config.extra["model_capabilities"]["local-qwen"]
    options.update({
        "ollama_native_context": True,
        "ollama_url": "http://localhost:11434/v1/chat/completions",
        "context_window_tokens": 8192,
        "response_headroom_tokens": 2048,
        "ollama_max_input_chars": 25000,
    })
    return options


def test_exact_model_context_survives_switch_without_changing_other_models(tmp_path):
    adapter = _adapter(tmp_path, enabled=False)
    _small_model_options(adapter)
    adapter.config.extra["provider_reasoning"] = "off"
    messages = [{"role": "user", "content": "task"}]

    small = adapter._build_payload(messages)
    assert small["options"] == {"num_ctx": 8192, "num_predict": 2048}
    assert small["truncate"] is False
    adapter.config.model = "other-model"
    other = adapter._build_payload(messages)
    assert other["options"] == {}
    assert "truncate" in other
    adapter.config.model = "local-qwen"
    assert adapter._build_payload(messages) == small


def test_exact_model_context_is_not_reused_at_different_endpoint(tmp_path):
    adapter = _adapter(tmp_path, enabled=False)
    _small_model_options(adapter)
    adapter.ollama_url = "http://different.invalid/v1/chat/completions"

    payload = adapter._build_payload([{"role": "user", "content": "task"}])
    assert payload["options"] == {}
    assert payload["truncate"] is False
    assert adapter._native_capacity() is None


@pytest.mark.asyncio
async def test_encoded_input_limit_blocks_before_any_physical_probe(tmp_path):
    adapter = _adapter(tmp_path, enabled=False)
    _small_model_options(adapter)["ollama_max_input_chars"] = 1000
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={
            "message": {"content": "answer"}, "done": True,
            "done_reason": "stop", "prompt_eval_count": 100, "eval_count": 1,
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter.client = client
        response = await adapter.generate_response("汉" * 200, "encoded-budget")

    assert response.error_code == "CONTEXT_CAPACITY_REJECTED"
    assert requests == []


@pytest.mark.asyncio
async def test_exact_model_token_reserve_blocks_full_generation(tmp_path):
    adapter = _adapter(tmp_path, enabled=False)
    _small_model_options(adapter)
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={
            "message": {"content": "a"}, "done": True,
            "done_reason": "length", "prompt_eval_count": 7000, "eval_count": 1,
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter.client = client
        response = await adapter.generate_response("task", "token-budget")

    assert response.error_code == "CONTEXT_CAPACITY_REJECTED"
    assert len(requests) == 1
    assert requests[0]["options"] == {"num_ctx": 8192, "num_predict": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("upstream_error,expected_code,attempts", [
    ("resource_profile_unverified", "PROVIDER_RESOURCE_PROFILE_UNVERIFIED", 1),
    ("temporary backend unavailable", "PROVIDER_SERVER_ERROR", 4),
])
async def test_qualification_rejection_stops_only_its_own_transport_recovery(
    tmp_path, upstream_error, expected_code, attempts
):
    adapter = _adapter(tmp_path, enabled=False)
    adapter.TRANSIENT_PROVIDER_CALL_RETRY_DELAY_S = 0
    adapter.TRANSIENT_PROVIDER_CALL_RETRY_MAX_DELAY_S = 0
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(503, json={"error": upstream_error})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter.client = client
        response = await adapter.generate_response("task", "qualified-recovery")

    assert response.error_code == expected_code
    assert len(requests) == attempts
def test_native_context_memory_controls_are_exact_model_scoped(tmp_path):
    adapter = _adapter(tmp_path)
    adapter.config.extra["model_capabilities"]["local-qwen"].update({
        "ollama_num_batch": 128, "ollama_num_gpu": 60,
    })

    options = adapter._build_payload([{"role": "user", "content": "task"}])["options"]

    assert options == {"num_ctx": 49152, "num_predict": 8192, "num_batch": 128, "num_gpu": 60}


def test_native_context_idle_residency_is_explicit_without_forcing_cpu_offload(tmp_path):
    adapter = _adapter(tmp_path)
    adapter.config.extra["ollama_keep_alive_seconds"] = 90
    payload = adapter._build_payload([{"role": "user", "content": "task"}])

    assert payload["keep_alive"] == 90
    assert "num_gpu" not in payload["options"]
    assert payload["options"]["num_ctx"] == 49152
    assert adapter._native_capacity().usable_input_tokens == 40960


@pytest.mark.parametrize("value", [-1, True, "90s"])
def test_native_context_rejects_invalid_idle_residency(tmp_path, value):
    adapter = _adapter(tmp_path)
    adapter.config.extra["ollama_keep_alive_seconds"] = value

    with pytest.raises(ValueError):
        adapter._build_payload([{"role": "user", "content": "task"}])


@pytest.mark.parametrize("value", [0, -1, True, "128"])
def test_native_context_rejects_invalid_batch_controls(tmp_path, value):
    adapter = _adapter(tmp_path)
    adapter.config.extra["model_capabilities"]["local-qwen"]["ollama_num_batch"] = value

    with pytest.raises(ValueError):
        adapter._build_payload([{"role": "user", "content": "task"}])


def test_native_context_preserves_task_and_completed_tool_results(tmp_path):
    adapter = _adapter(tmp_path)
    messages = [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "original task"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "file_read",
                        "arguments": '{"path":"sample.txt"}',
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "read receipt"},
    ]
    before = deepcopy(messages)
    payload = adapter._build_payload(messages)

    assert messages == before
    assert [item["role"] for item in payload["messages"]] == [
        "system", "user", "assistant", "tool"
    ]
    assert payload["messages"][1]["content"] == "original task"
    assert payload["messages"][2]["tool_calls"][0]["function"]["arguments"] == {
        "path": "sample.txt"
    }
    assert payload["messages"][3]["content"] == "read receipt"
    assert payload["messages"][3]["tool_name"] == "file_read"


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_native_context_tools_usage_and_wire_truth(tmp_path, streaming):
    adapter = _adapter(tmp_path)
    chunks = [
        {"message": {"role": "assistant", "thinking": "checking"}, "done": False},
        {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "file_read", "arguments": {"path": "a.txt"}}},
                    {"function": {"name": "file_read", "arguments": {"path": "b.txt"}}},
                ],
            },
            "done": True,
            "done_reason": "stop",
            "prompt_eval_count": 123,
            "eval_count": 17,
            "total_duration": 100000000,
            "load_duration": 10000000,
            "prompt_eval_duration": 40000000,
            "eval_duration": 50000000,
        },
    ]
    if not streaming:
        chunks[-1]["message"]["thinking"] = "checking"
        chunks = chunks[-1:]
    received = []

    def respond(request):
        received.append(json.loads(request.content))
        return httpx.Response(
            200,
            content="\n".join(json.dumps(chunk) for chunk in chunks),
        )

    events = []
    adapter.set_her_v2_stream_inactivity_timeout(0.005)

    async def capture(event):
        await asyncio.sleep(0.01)
        events.append((event.kind, event.raw_delta or event.summary))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter.client = client
        method = adapter._stream_api_once if streaming else adapter._call_api_once
        payload = adapter._build_payload(
            [{"role": "user", "content": "read two files"}], use_streaming=streaming
        )
        result = await method(payload, {}, capture)

    assert received == [payload]
    assert events == [("thinking", "checking")]
    assert result.prompt_tokens == 123
    assert result.completion_tokens == 17
    assert result.thinking_tokens == 0
    assert result.transport_complete is True
    assert len({call["id"] for call in result.tool_calls}) == 2
    assert json.loads(result.tool_calls[1]["function"]["arguments"]) == {"path": "b.txt"}
    assert _provider_response_decision(result, tool_registry_available=True)["execute_tools"] is True
    assert result.provider_compatibility["native_done_reason"] == "stop"
    performance = result.provider_compatibility["native_performance"]
    assert performance["prompt_tokens_per_second"] == 3075
    assert performance["output_tokens_per_second"] == 340
    assert performance["load_duration_ns"] == 10000000
    assert performance["first_response_ms"] >= 0
    assert result.provider_compatibility["native_request_controls"]["num_ctx"] == 49152
    assert result.finish_reason_source == "ollama_native_translation"
    assert [
        json.loads(event["raw_line"])
        for event in result.wire_evidence["ndjson_events"]
    ] == chunks


@pytest.mark.asyncio
async def test_native_context_missing_timings_are_unknown_not_zero(tmp_path):
    adapter = _adapter(tmp_path)
    chunk = {"message": {"content": "done"}, "done": True, "done_reason": "stop"}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=chunk))
    ) as client:
        adapter.client = client
        result = await adapter._call_api_once(
            adapter._build_payload([{"role": "user", "content": "task"}]), {}, None
        )

    performance = result.provider_compatibility["native_performance"]
    assert "prompt_tokens_per_second" not in performance
    assert "output_tokens_per_second" not in performance
    assert "load_duration_ns" not in performance


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["", "not-json"])
async def test_native_context_incomplete_stream_is_not_success(tmp_path, ending):
    adapter = _adapter(tmp_path)
    content = json.dumps({"message": {"content": "partial"}, "done": False})
    events = []

    async def capture(event):
        events.append(event.summary)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=content + "\n" + ending)
        )
    ) as client:
        adapter.client = client
        with pytest.raises(httpx.RemoteProtocolError) as raised:
            await adapter._stream_api_once(
                adapter._build_payload([{"role": "user", "content": "task"}], True),
                {}, capture,
            )

    assert events == ["partial"]
    assert raised.value.hashi_provider_protocol["transport_complete"] is False
    assert raised.value.hashi_provider_protocol["text_length"] == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    "the input length exceeds the context length",
    json.dumps({"error": {
        "code": 400,
        "message": "request (8215 tokens) exceeds the available context size (1024 tokens), try increasing it",
        "type": "exceed_context_size_error",
        "n_prompt_tokens": 8215,
        "n_ctx": 1024,
    }}),
])
async def test_native_context_server_overflow_is_typed_and_never_retried(tmp_path, error):
    adapter = _adapter(tmp_path)
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(400, json={"error": error})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter.client = client
        with pytest.raises(ProviderContextCapacityPreflightError) as raised:
            await adapter._stream_api_once(
                adapter._build_payload([{"role": "user", "content": "task"}], True),
                {}, None,
            )

    assert len(requests) == 1
    assert raised.value.response.status_code == 400
    assert raised.value.response.json() == {"error": error}


@pytest.mark.asyncio
async def test_native_context_other_http_failures_are_not_capacity_errors(tmp_path):
    adapter = _adapter(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(400, json={"error": "invalid tools schema"})
        )
    ) as client:
        adapter.client = client
        with pytest.raises(httpx.HTTPStatusError):
            await adapter._stream_api_once(
                adapter._build_payload([{"role": "user", "content": "task"}], True),
                {}, None,
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "error_code"),
    [(None, "PROVIDER_MISSING_FINISH_REASON"), ("length", "PROVIDER_OUTPUT_TRUNCATED")],
)
async def test_native_context_does_not_accept_missing_or_truncated_finish(
    tmp_path, reason, error_code
):
    adapter = _adapter(tmp_path)
    chunk = {"message": {"content": "unfinished"}, "done": True}
    if reason is not None:
        chunk["done_reason"] = reason
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=chunk))
    ) as client:
        adapter.client = client
        result = await adapter._call_api_once(
            adapter._build_payload([{"role": "user", "content": "task"}]), {}, None
        )

    decision = _provider_response_decision(result, tool_registry_available=True)
    assert decision["success"] is False
    assert decision["execute_tools"] is False
    assert decision["error_code"] == error_code


@pytest.mark.asyncio
async def test_native_context_records_transport_failure_after_tool_fragment(tmp_path):
    adapter = _adapter(tmp_path)

    class InterruptedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield json.dumps({
                "message": {"tool_calls": [{
                    "function": {"name": "file_read", "arguments": {"path": "a.txt"}}
                }]}, "done": False,
            }).encode() + b"\n"
            raise httpx.ReadError("connection lost")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=InterruptedStream())
        )
    ) as client:
        adapter.client = client
        with pytest.raises(httpx.ReadError) as raised:
            await adapter._stream_api_once(
                adapter._build_payload([{"role": "user", "content": "task"}], True),
                {}, None,
            )

    protocol = raised.value.hashi_provider_protocol
    assert protocol["transport_complete"] is False
    assert protocol["tool_calls"][0]["name"] == "file_read"
    assert protocol["wire_evidence"]["ndjson_events"]

@pytest.mark.asyncio
@pytest.mark.parametrize("actual_tokens", [40961, 48875, 50900, 0])
async def test_native_capacity_uses_provider_count_before_full_generation(tmp_path, actual_tokens):
    adapter = _adapter(tmp_path)
    requests = []

    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        probe = payload["options"].get("num_predict") == 1
        return httpx.Response(200, json={
            "message": {"content": "" if probe else "would have generated"},
            "done": True, "done_reason": "length" if probe else "stop",
            "prompt_eval_count": actual_tokens, "eval_count": 1,
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter.client = client
        response = await adapter.generate_response("small character estimate", "capacity-regression")

    assert response.is_success is False
    assert response.error_code == "CONTEXT_CAPACITY_REJECTED"
    assert len(requests) == 1
    assert requests[0]["options"]["num_ctx"] == 49152
    assert requests[0]["options"]["num_predict"] == 1
    assert requests[0]["truncate"] is False
    records = response.stream_metadata["meter"]["provider_calls"]
    assert any(record.get("purpose") == "context_capacity_probe" for record in records)



@pytest.mark.asyncio
@pytest.mark.parametrize("overrides", [
    {"eval_count": 8192},
    {"eval_count": None},
    {"prompt_eval_count": True},
    {"prompt_eval_count": "40000"},
    {"done_reason": None},
])
async def test_native_probe_without_complete_bounded_counts_cannot_admit_generation(tmp_path, overrides):
    adapter = _adapter(tmp_path)
    requests = []
    def respond(request):
        requests.append(json.loads(request.content))
        chunk = {
            "message": {"content": ""}, "done": True, "done_reason": "length",
            "prompt_eval_count": 40000, "eval_count": 1,
        }
        chunk.update(overrides)
        return httpx.Response(200, json=chunk)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter.client = client
        response = await adapter.generate_response("task", "invalid-counter-regression")
    assert not response.is_success
    assert response.error_code == "CONTEXT_CAPACITY_REJECTED"
    assert len(requests) == 1

@pytest.mark.asyncio
@pytest.mark.parametrize("reasoning,think", [("enabled", True), ("off", False)])
async def test_native_capacity_probe_keeps_exact_boundary_and_separate_usage(tmp_path, reasoning, think):
    adapter = _adapter(tmp_path)
    adapter.config.extra["provider_reasoning"] = reasoning
    requests, visible = [], []

    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        probe = payload["options"].get("num_predict") == 1
        return httpx.Response(200, json={
            "message": {"content": "hidden" if probe else "answer"},
            "done": True, "done_reason": "length" if probe else "stop",
            "prompt_eval_count": 40960, "eval_count": 1 if probe else 3,
        })

    async def capture(event):
        visible.append(event.summary)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter.client = client
        response = await adapter.generate_response(
            " " * 200000 + "task", "boundary-regression", on_stream_event=capture
        )

    assert response.is_success, response.error
    assert response.text == "answer"
    assert "hidden" not in visible
    assert [payload["options"]["num_predict"] for payload in requests] == [1, 8192]
    assert requests[0]["messages"] == requests[1]["messages"]
    assert all(payload["think"] is think for payload in requests)
    records = response.stream_metadata["meter"]["provider_calls"]
    assert len(records) == 2
    assert records[0]["purpose"] == "context_capacity_probe"
    assert records[0]["input"] == 40960
    assert records[0]["output"] == 1



@pytest.mark.asyncio
async def test_capacity_probe_tool_calls_never_execute(tmp_path):
    from tools.registry import ToolRegistry

    sentinel = tmp_path / "must-not-exist.txt"
    adapter = _adapter(tmp_path)
    adapter.tool_registry = ToolRegistry(
        allowed_tools=["file_write"], access_root=tmp_path,
        workspace_dir=tmp_path, secrets={},
    )
    requests = []
    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        probe = payload["options"].get("num_predict") == 1
        message = {"content": "answer"}
        if probe:
            message = {"tool_calls": [{
                "function": {"name": "file_write", "arguments": {
                    "path": str(sentinel), "content": "unwanted probe side effect",
                }}
            }]}
        return httpx.Response(200, json={
            "message": message, "done": True, "done_reason": "stop",
            "prompt_eval_count": 100, "eval_count": 1,
        })
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter.client = client
        response = await adapter.generate_response("only answer", "probe-no-tools")
    assert response.is_success, response.error
    assert response.tool_call_count == 0
    assert len(requests) == 2
    assert not sentinel.exists()

@pytest.mark.asyncio
async def test_native_tool_read_is_paged_with_original_result_saved_and_no_replay(tmp_path):
    from tools.registry import ToolRegistry

    source = tmp_path / "source.txt"
    original = "".join(f"第{index}行：容量测试资料，需要保留。\n" for index in range(180))
    source.write_text(original, encoding="utf-8")
    adapter = _adapter(tmp_path)
    adapter.tool_registry = ToolRegistry(
        allowed_tools=["file_read"], access_root=tmp_path,
        workspace_dir=tmp_path, secrets={},
    )
    requests = []
    main_calls = 0

    def respond(request):
        nonlocal main_calls
        payload = json.loads(request.content)
        requests.append(payload)
        probe = payload["options"].get("num_predict") == 1
        result_text = "".join(
            row.get("content", "") for row in payload["messages"] if row["role"] == "tool"
        )
        # Leave room for the first read batch; a separate regression verifies
        # that tool use closes when the initial payload is already near full.
        actual_count = 37536 + len(result_text.encode("utf-8")) // 2
        message = {"content": ""}
        if not probe:
            main_calls += 1
            if main_calls == 1:
                message["tool_calls"] = [{
                    "function": {"name": "file_read", "arguments": {"path": str(source)}}
                }]
            else:
                message["content"] = "已根据可见资料说明结果及分页限制。"
        return httpx.Response(200, json={
            "message": message, "done": True,
            "done_reason": "length" if probe else "stop",
            "prompt_eval_count": actual_count, "eval_count": 1 if probe else 20,
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter.client = client
        response = await adapter.generate_response("read source once", "paging-regression")

    assert response.is_success, response.error
    assert response.tool_call_count == 1
    assert main_calls == 2
    tool_messages = [
        row for row in requests[-1]["messages"] if row["role"] == "tool"
    ]
    assert len(tool_messages) == 1
    assert "HASHI_OLLAMA_RESULT_PAGE" in tool_messages[0]["content"]
    saved = list((tmp_path / "backend_state" / "ollama_tool_results").glob("*.txt"))
    assert len(saved) == 1
    assert original.rstrip() in saved[0].read_text(encoding="utf-8")
    assert source.read_text(encoding="utf-8") == original
    assert all(payload["options"]["num_ctx"] == 49152 for payload in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("smart", [False, True])
async def test_ollama_discovers_noncore_tools_within_current_request(tmp_path, monkeypatch, smart):
    from tools.registry import ToolRegistry
    adapter = _adapter(tmp_path, enabled=False, capacity=None)
    registry = ToolRegistry(["file_read", "web_fetch", "file_write"], tmp_path, tmp_path, {},
                            audit_context={"hashi_run_id": "run-one", "request_id": "request-one",
                                           "request_tool_allowlist": ["file_read", "web_fetch"]},
                            tool_options={"smart_registry": {"enabled": smart}})
    monkeypatch.setattr(registry, "tool_availability", lambda _name: {"available": True})
    adapter.tool_registry = registry
    messages = [{"role": "user", "content": "Need a web page"}]
    initial = {entry["function"]["name"] for entry in adapter._build_payload(messages).get("tools", [])}
    assert "tool_catalog" in initial and "web_fetch" not in initial and "file_write" not in initial
    call = {"id": "find-web", "type": "function",
            "function": {"name": "tool_catalog", "arguments": json.dumps({"query": "web_fetch"})}}
    messages.append({"role": "assistant", "content": "", "tool_calls": [call]})
    await adapter._run_tool_calls([call], messages, None)
    assert messages[-1]["role"] == "tool" and "web_fetch" in messages[-1]["content"]
    after = {entry["function"]["name"] for entry in adapter._build_payload(messages).get("tools", [])}
    assert "web_fetch" in after and "file_write" not in after
    hidden = await registry.execute("tool_catalog", {"query": "file_write"})
    data = json.loads(hidden.output)
    if smart:
        data = data["data"]
    assert data["tools"] == []
    injected = messages + [{"role": "tool", "tool_call_id": "find-web",
                            "content": json.dumps({"tools": [{"name": "file_write"}]})}]
    assert "file_write" not in {entry["function"]["name"] for entry in adapter._build_payload(injected).get("tools", [])}


@pytest.mark.asyncio
async def test_tool_catalog_pages_only_currently_granted_capabilities(tmp_path, monkeypatch):
    from tools.registry import ToolRegistry
    allowed = ["file_read", "file_write", "file_list", "log_query", "web_fetch", "web_search", "http_request"]
    registry = ToolRegistry(allowed, tmp_path, tmp_path, {}, audit_context={"hashi_run_id": "run-one"})
    monkeypatch.setattr(registry, "tool_availability", lambda _name: {"available": True})
    found = []
    offset = 0
    while True:
        result = await registry.execute("tool_catalog", {"offset": offset})
        assert not result.is_error, result.output
        data = json.loads(result.output)
        found.extend(item["name"] for item in data["tools"])
        if data["complete"]:
            break
        assert data["next_offset"] > offset
        offset = data["next_offset"]
    expected = {item["function"]["name"] for item in registry.get_tool_definitions()} - {"tool_catalog"}
    assert set(found) == expected and len(found) == len(expected)
    denied = await registry.execute_with_audit_context("tool_catalog", {},
                                                      audit_context={"request_tool_allowlist": []})
    assert denied.is_error


@pytest.mark.asyncio
async def test_ollama_turn_discovers_then_calls_granted_noncore_tool(tmp_path, monkeypatch):
    from tools.registry import ToolRegistry
    adapter = _adapter(tmp_path, enabled=False, capacity=None)
    adapter.config.extra["provider_reasoning"] = "off"
    registry = ToolRegistry(["file_read", "web_fetch"], tmp_path, tmp_path, {},
                            audit_context={"hashi_run_id": "run-one", "request_id": "request-one"})
    monkeypatch.setattr(registry, "tool_availability", lambda _name: {"available": True})
    original_dispatch = registry._dispatch
    fetched = []
    async def fixture_dispatch(name, arguments, *, tool_call_id=""):
        if name == "web_fetch":
            fetched.append(arguments["url"])
            return "fixture web page"
        return await original_dispatch(name, arguments, tool_call_id=tool_call_id)
    monkeypatch.setattr(registry, "_dispatch", fixture_dispatch)
    adapter.tool_registry = registry
    requests = []
    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        names = {item["function"]["name"] for item in payload.get("tools", [])}
        if len(requests) == 1:
            assert "tool_catalog" in names and "web_fetch" not in names
            message = {"tool_calls": [{"function": {"name": "tool_catalog", "arguments": {"query": "web_fetch"}}}]}
        elif len(requests) == 2:
            assert "web_fetch" in names
            message = {"tool_calls": [{"function": {"name": "web_fetch", "arguments": {"url": "https://example.test"}}}]}
        else:
            assert any("fixture web page" in str(row.get("content")) for row in payload["messages"] if row.get("role") == "tool")
            message = {"content": "fixture answer"}
        return httpx.Response(200, json={"message": message, "done": True, "done_reason": "stop",
                                         "prompt_eval_count": 100, "eval_count": 3})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter.client = client
        response = await adapter.generate_response("Find a web page", "catalog-turn")
    assert response.is_success, response.error
    assert response.text == "fixture answer" and fetched == ["https://example.test"]
    assert len(requests) == 3


def test_native_ollama_uses_selected_model_settings_without_context_pilot(tmp_path):
    adapter = _adapter(tmp_path, enabled=False, capacity=None)
    adapter.config.extra["provider_reasoning"] = "off"
    adapter.config.extra["model_capabilities"] = {
        "local-qwen": {"context_window_tokens": 65536, "response_headroom_tokens": 12000,
                       "ollama_url": "http://localhost:11434/v1/chat/completions"},
        "small-model": {"ollama_num_ctx": 8192, "ollama_num_predict": 1024},
    }
    messages = [{"role": "user", "content": "task"}]
    first = adapter._build_payload(messages)
    assert first["options"] == {"num_ctx": 65536, "num_predict": 12000}
    assert adapter._chat_completions_url().endswith("/api/chat")
    adapter.config.model = "small-model"
    second = adapter._build_payload(messages)
    assert second["options"] == {"num_ctx": 8192, "num_predict": 1024}
    adapter.config.model = "local-qwen"
    adapter.ollama_url = "http://different.invalid/v1/chat/completions"
    assert adapter._build_payload(messages)["options"] == {}


def test_ollama_compatible_endpoint_keeps_its_existing_protocol_options(tmp_path):
    adapter = _adapter(tmp_path, enabled=False, capacity=None)
    adapter.config.extra["model_capabilities"] = {"local-qwen": {"context_window_tokens": 65536,
                                                                    "response_headroom_tokens": 12000}}
    payload = adapter._build_payload([{"role": "user", "content": "task"}])
    assert adapter._chat_completions_url().endswith("/v1/chat/completions")
    assert payload["options"] == {"num_ctx": 32768} and "truncate" not in payload


@pytest.mark.parametrize("settings", [
    {"ollama_num_ctx": 0}, {"ollama_num_predict": True},
    {"ollama_num_ctx": 8192, "ollama_num_predict": 8192},
])
def test_native_ollama_rejects_invalid_explicit_numeric_settings(tmp_path, settings):
    adapter = _adapter(tmp_path, enabled=False, capacity=None)
    adapter.config.extra["provider_reasoning"] = "off"
    adapter.config.extra["model_capabilities"] = {"local-qwen": settings}
    with pytest.raises(ValueError):
        adapter._build_payload([{"role": "user", "content": "task"}])


@pytest.mark.asyncio
async def test_native_ollama_sends_selected_settings_on_the_wire(tmp_path):
    adapter = _adapter(tmp_path, enabled=False, capacity=None)
    adapter.config.extra["provider_reasoning"] = "off"
    adapter.config.extra["model_capabilities"] = {
        "local-qwen": {"context_window_tokens": 65536, "response_headroom_tokens": 12000},
        "small-model": {"ollama_num_ctx": 8192, "ollama_num_predict": 1024},
    }
    requests = []
    def respond(request):
        requests.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"message": {"content": "ok"}, "done": True,
                                         "done_reason": "stop", "prompt_eval_count": 100, "eval_count": 1})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter.client = client
        for model in ("local-qwen", "small-model"):
            adapter.config.model = model
            payload = adapter._build_payload([{"role": "user", "content": "task"}])
            result = await adapter._call_api_once(payload, adapter._request_headers(), None)
            assert result.text == "ok"
    assert [path for path, _ in requests] == ["/api/chat", "/api/chat"]
    assert [payload["options"] for _, payload in requests] == [
        {"num_ctx": 65536, "num_predict": 12000}, {"num_ctx": 8192, "num_predict": 1024}]


@pytest.mark.asyncio
async def test_native_capacity_refreshes_for_endpoint_and_model_revision(tmp_path):
    adapter = _adapter(tmp_path, capacity=None, url="http://one.invalid/ollama/v1/chat/completions")
    requests = []
    versions = {
        "one.invalid": {"digest": "a" * 64, "capacity": 8192},
        "two.invalid": {"digest": "b" * 64, "capacity": 16384},
    }

    def respond(request):
        body = json.loads(request.content) if request.content else {}
        requests.append((request.url.host, request.url.path, body))
        data = versions[request.url.host]
        if request.url.path.endswith("/api/tags"):
            return httpx.Response(200, json={"models": [
                {"name": "local-qwen:latest", "digest": data["digest"]},
            ]})
        if request.url.path.endswith("/api/show"):
            assert body == {"model": "local-qwen"}
            return httpx.Response(200, json={"model_info": {
                "qwen.context_length": data["capacity"],
            }})
        if request.url.path.endswith("/api/chat"):
            return httpx.Response(200, json={
                "message": {"content": "done"}, "done": True, "done_reason": "stop",
                "prompt_eval_count": 100, "eval_count": 1,
            })
        raise AssertionError(request.url)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter.client = client
        first = await adapter.generate_response("hello", "capacity-one")
        assert first.is_success
        assert first.stream_metadata["ollama_capacity"]["verified"] is True
        first_calls = [row for row in requests if row[0] == "one.invalid" and row[1].endswith("/api/chat")]
        assert first_calls[-1][2]["options"] == {"num_ctx": 8192, "num_predict": 2048}
        assert first_calls[-1][2]["truncate"] is False

        adapter.ollama_url = "http://two.invalid/ollama/v1/chat/completions"
        second = await adapter.generate_response("hello", "capacity-two")
        assert second.is_success
        second_calls = [row for row in requests if row[0] == "two.invalid" and row[1].endswith("/api/chat")]
        assert second_calls[-1][2]["options"] == {"num_ctx": 16384, "num_predict": 4096}
        assert sum(1 for host, path, _ in requests if host == "two.invalid" and path.endswith("/api/show")) == 1

        versions["two.invalid"] = {"digest": "c" * 64, "capacity": 32768}
        third = await adapter.generate_response("hello", "capacity-revised")
        assert third.is_success
        third_calls = [row for row in requests if row[0] == "two.invalid" and row[1].endswith("/api/chat")]
        assert third_calls[-1][2]["options"] == {"num_ctx": 32768, "num_predict": 8192}
        assert sum(1 for host, path, _ in requests if host == "two.invalid" and path.endswith("/api/show")) == 2


@pytest.mark.asyncio
async def test_unknown_native_capacity_defers_to_server_without_false_claim(tmp_path):
    adapter = _adapter(tmp_path, capacity=None, url="http://unknown.invalid/v1/chat/completions")
    requests = []

    def respond(request):
        body = json.loads(request.content) if request.content else {}
        requests.append((request.url.path, body))
        if request.url.path.endswith("/api/tags"):
            return httpx.Response(200, json={"models": [
                {"name": "local-qwen:latest", "digest": "missing"},
            ]})
        if request.url.path.endswith("/api/chat"):
            return httpx.Response(200, json={
                "message": {"content": "server accepted"}, "done": True, "done_reason": "stop",
                "prompt_eval_count": 100, "eval_count": 1,
            })
        raise AssertionError(request.url)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter.client = client
        response = await adapter.generate_response("hello", "unknown-capacity")
    assert response.is_success
    assert response.stream_metadata["ollama_capacity"] == {"verified": False, "provenance": None}
    chats = [body for path, body in requests if path.endswith("/api/chat")]
    assert len(chats) == 1
    assert chats[0]["options"] == {} and chats[0]["truncate"] is False


@pytest.mark.asyncio
async def test_discovered_capacity_preserves_explicit_model_budgets(tmp_path):
    adapter = _adapter(tmp_path, capacity=None)
    adapter.config.extra["model_capabilities"] = {"local-qwen": {
        "ollama_native_context": True, "ollama_num_ctx": 4096, "ollama_num_predict": 1000,
    }}
    chats = []

    def respond(request):
        body = json.loads(request.content) if request.content else {}
        if request.url.path.endswith("/api/tags"):
            return httpx.Response(200, json={"models": [
                {"name": "local-qwen:latest", "digest": "d" * 64},
            ]})
        if request.url.path.endswith("/api/show"):
            return httpx.Response(200, json={"model_info": {"qwen.context_length": 8192}})
        if request.url.path.endswith("/api/chat"):
            chats.append(body)
            return httpx.Response(200, json={
                "message": {"content": "done"}, "done": True, "done_reason": "stop",
                "prompt_eval_count": 100, "eval_count": 1,
            })
        raise AssertionError(request.url)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter.client = client
        result = await adapter.generate_response("hello", "selected-budgets")
    assert result.is_success
    assert chats[-1]["options"] == {"num_ctx": 4096, "num_predict": 1000}
    assert result.stream_metadata["ollama_capacity"]["verified"] is True


@pytest.mark.asyncio
async def test_stale_endpoint_limits_are_not_reused_when_metadata_refreshes(tmp_path):
    adapter = _adapter(tmp_path, enabled=False, capacity=49152,
                       url="http://one.invalid/v1/chat/completions")
    row = adapter.config.extra["model_capabilities"]["local-qwen"]
    row.update({"ollama_native_context": True,
                "ollama_url": "http://one.invalid/v1/chat/completions",
                "ollama_max_input_chars": 3})
    adapter.ollama_url = "http://two.invalid/v1/chat/completions"
    chats = []

    def respond(request):
        assert request.url.host == "two.invalid"
        body = json.loads(request.content) if request.content else {}
        if request.url.path.endswith("/api/tags"):
            return httpx.Response(200, json={"models": [
                {"name": "local-qwen:latest", "digest": "e" * 64},
            ]})
        if request.url.path.endswith("/api/show"):
            return httpx.Response(200, json={"model_info": {"qwen.context_length": 8192}})
        if request.url.path.endswith("/api/chat"):
            chats.append(body)
            return httpx.Response(200, json={
                "message": {"content": "done"}, "done": True, "done_reason": "stop",
                "prompt_eval_count": 100, "eval_count": 1,
            })
        raise AssertionError(request.url)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter.client = client
        result = await adapter.generate_response("hello", "new-endpoint")
    assert result.is_success
    assert chats[-1]["options"] == {"num_ctx": 8192, "num_predict": 2048}


@pytest.mark.asyncio
async def test_unknown_capacity_preserves_actual_server_overflow_error(tmp_path):
    adapter = _adapter(tmp_path, capacity=None)
    requests = []

    def respond(request):
        requests.append(request.url.path)
        if request.url.path.endswith("/api/tags"):
            return httpx.Response(200, json={"models": []})
        if request.url.path.endswith("/api/chat"):
            body = json.loads(request.content)
            assert body["truncate"] is False and body["options"] == {}
            return httpx.Response(400, json={"error": "input length exceeds the context length"})
        raise AssertionError(request.url)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter.client = client
        result = await adapter.generate_response("hello", "actual-overflow")
    assert result.is_success is False
    assert result.error_code == "CONTEXT_CAPACITY_REJECTED"
    assert result.stream_metadata["ollama_capacity"]["verified"] is False
    assert sum(path.endswith("/api/chat") for path in requests) == 1



def test_context_capacities_row_is_not_shadowed_by_model_options_without_capacity(tmp_path):
    adapter = _adapter(tmp_path, capacity=None)
    adapter.config.extra["model_capabilities"] = {"local-qwen": {
        "ollama_native_context": True, "ollama_num_batch": 64,
    }}
    adapter.config.extra["context_capacities"] = {"local-qwen": {
        "context_window_tokens": 8192, "response_headroom_tokens": 1024,
    }}
    payload = adapter._build_payload([{"role": "user", "content": "task"}])
    assert payload["options"] == {"num_ctx": 8192, "num_predict": 1024, "num_batch": 64}


def test_context_capacities_row_is_bound_to_its_own_endpoint(tmp_path):
    adapter = _adapter(tmp_path, capacity=None, url="http://one.invalid/v1/chat/completions")
    adapter.config.extra["context_capacities"] = {"local-qwen": {
        "ollama_url": "http://two.invalid/v1/chat/completions",
        "context_window_tokens": 8192, "response_headroom_tokens": 1024,
    }}
    assert adapter._native_capacity() is None
    adapter.ollama_url = "http://two.invalid/v1/chat/completions"
    capacity = adapter._native_capacity()
    assert capacity.context_window_tokens == 8192
    assert capacity.response_headroom_tokens == 1024


def test_invalid_selected_capacity_cannot_fall_back_to_unrelated_runtime_profile(tmp_path):
    adapter = _adapter(tmp_path, capacity=None)
    adapter.config.extra["context_capacities"] = {"local-qwen": {
        "context_window_tokens": 8192, "response_headroom_tokens": 9000,
    }}
    adapter.config._hashi_runtime = SimpleNamespace(backend_manager=SimpleNamespace(
        config=SimpleNamespace(allowed_backends=[{
            "engine": "ollama-api", "model_capabilities": {
                "local-qwen": {"context_window_tokens": 65536, "response_headroom_tokens": 2048},
            },
        }]),
    ))
    with pytest.raises(ProviderContextCapacityPreflightError):
        adapter._native_capacity()



@pytest.mark.asyncio
async def test_configured_capacity_is_not_reported_as_live_server_verification(tmp_path):
    adapter = _adapter(tmp_path, capacity=49152)
    adapter.config.extra["model_capabilities"]["local-qwen"]["capacity_provenance"] = (
        "ollama_model_info:" + "f" * 64
    )

    def respond(request):
        return httpx.Response(200, json={
            "message": {"content": "done"}, "done": True, "done_reason": "stop",
            "prompt_eval_count": 100, "eval_count": 1,
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter.client = client
        result = await adapter.generate_response("hello", "declared-capacity")
    assert result.is_success
    assert result.stream_metadata["ollama_capacity"]["verified"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", ["ambiguous", "changed"])
async def test_ambiguous_or_changing_model_metadata_does_not_authorize_capacity(tmp_path, condition):
    adapter = _adapter(tmp_path, capacity=None)
    tag_count = 0

    def respond(request):
        nonlocal tag_count
        if request.url.path.endswith("/api/tags"):
            tag_count += 1
            digest = "a" * 64 if tag_count == 1 else "b" * 64
            return httpx.Response(200, json={"models": [
                {"name": "local-qwen:latest", "digest": digest},
            ]})
        if request.url.path.endswith("/api/show"):
            info = {"qwen.context_length": 8192}
            if condition == "ambiguous":
                info["other.context_length"] = 16384
            return httpx.Response(200, json={"model_info": info})
        raise AssertionError(request.url)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter.client = client
        await adapter._refresh_discovered_capacity()
    assert adapter._native_capacity() is None
