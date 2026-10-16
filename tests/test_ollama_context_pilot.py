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


def test_native_context_unknown_capacity_fails_closed(tmp_path):
    adapter = _adapter(tmp_path, capacity=None)

    with pytest.raises(ProviderContextCapacityPreflightError):
        adapter._build_payload([{"role": "user", "content": "task"}])


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
async def test_native_capacity_probe_keeps_exact_boundary_and_separate_usage(tmp_path):
    adapter = _adapter(tmp_path)
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
        actual_count = 39000 + len(result_text.encode("utf-8")) // 2
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
