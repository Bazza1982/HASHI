from __future__ import annotations

import json
import time
from collections.abc import Mapping
from uuid import uuid4

import httpx

from adapters.openrouter_api import (
    ProviderContextCapacityPreflightError,
    _APIResult,
    _annotate_stream_exception,
    _iter_provider_stream_lines,
    _provider_request_id,
    _read_http_error_body,
    _request_wire_evidence,
    _tool_call_protocol_summary,
)
from adapters.stream_events import KIND_TEXT_DELTA, KIND_THINKING, StreamEvent


def _native_performance(final_chunk, first_response_ms, first_answer_ms):
    result = {}
    for name in ("total_duration", "load_duration", "prompt_eval_duration", "eval_duration"):
        duration = final_chunk.get(name)
        if type(duration) is int and duration >= 0:
            result[name + "_ns"] = duration
    for count_key, duration_key, metric in (
        ("prompt_eval_count", "prompt_eval_duration_ns", "prompt_tokens_per_second"),
        ("eval_count", "eval_duration_ns", "output_tokens_per_second"),
    ):
        count = final_chunk.get(count_key)
        duration = result.get(duration_key)
        if type(count) is int and count >= 0 and duration is not None and duration > 0:
            result[metric] = count * 1_000_000_000 / duration
    for name, observed in (("first_response_ms", first_response_ms), ("first_answer_ms", first_answer_ms)):
        if observed is not None:
            result[name] = observed
    return result


def _native_capacity_error(response):
    if response.status_code != 400:
        return None
    try:
        data = response.json()
    except ValueError:
        return None
    error = data.get("error") if isinstance(data, Mapping) else None
    details = error
    if isinstance(error, str):
        try:
            decoded = json.loads(error)
        except ValueError:
            decoded = None
        if isinstance(decoded, Mapping):
            details = decoded.get("error")
    structured_overflow = (
        isinstance(details, Mapping)
        and details.get("type") == "exceed_context_size_error"
    )
    legacy_overflow = (
        isinstance(error, str)
        and "input length exceeds the context length" in error.casefold()
    )
    if not structured_overflow and not legacy_overflow:
        return None
    failure = ProviderContextCapacityPreflightError(
        "Ollama rejected the complete input as too large; "
        "server truncation is disabled and no tools were replayed."
    )
    failure.response = response
    failure.request = response.request
    return failure


async def call_native_chat(adapter, payload, headers, on_stream_event) -> _APIResult:
    started = time.perf_counter()
    first_response_ms = None
    first_answer_ms = None
    text_chunks = []
    thinking_chunks = []
    tool_calls = []
    final_chunk = None
    wire_evidence = {
        "transport": "ollama_ndjson" if payload.get("stream") else "ollama_json",
        "request": _request_wire_evidence(payload),
        "ndjson_events": [],
        "tool_call_fragments": [],
        "assembly_snapshots": [],
    }
    state = {
        "raw_finish_reason_present": False,
        "raw_finish_reason": None,
        "finish_reason_source": "missing",
        "normalized_finish_reason": "incomplete",
        "provider_response_id": "",
        "transport_request_id": "",
        "text_provided": False,
        "text_length": 0,
        "reasoning_availability": "unavailable",
        "reasoning_length": 0,
        "tool_calls": [],
        "wire_evidence": wire_evidence,
    }
    guard = adapter._new_her_v2_stream_inactivity_guard()
    async with adapter.client.stream(
        "POST", adapter._chat_completions_url(), json=payload, headers=headers
    ) as response:
        await _read_http_error_body(response)
        capacity_error = _native_capacity_error(response)
        if capacity_error is not None:
            raise capacity_error
        response.raise_for_status()
        wire_evidence["request"] = _request_wire_evidence(payload, response.request)
        state["transport_request_id"] = _provider_request_id(response)
        try:
            async for line in _iter_provider_stream_lines(
                response, state, inactivity_guard=guard
            ):
                adapter._touch_activity()
                if not line.strip():
                    continue
                arrival = len(wire_evidence["ndjson_events"]) + 1
                wire_evidence["ndjson_events"].append(
                    {"arrival": arrival, "raw_line": line}
                )
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise httpx.RemoteProtocolError("Ollama returned invalid JSON") from exc
                if not isinstance(chunk, Mapping):
                    raise httpx.RemoteProtocolError("Ollama returned a non-object response")
                if chunk.get("error"):
                    raise httpx.RemoteProtocolError(str(chunk["error"]))
                message = chunk.get("message") or {}
                if not isinstance(message, Mapping):
                    raise httpx.RemoteProtocolError("Ollama returned an invalid message")
                content = message.get("content") or ""
                thinking = message.get("thinking") or ""
                incoming_calls = message.get("tool_calls") or []
                if not isinstance(content, str) or not isinstance(thinking, str):
                    raise httpx.RemoteProtocolError("Ollama returned non-text content")
                if not isinstance(incoming_calls, list):
                    raise httpx.RemoteProtocolError("Ollama returned invalid tool calls")
                if (content or thinking or incoming_calls) and first_response_ms is None:
                    first_response_ms = (time.perf_counter() - started) * 1000
                if content and first_answer_ms is None:
                    first_answer_ms = (time.perf_counter() - started) * 1000
                if content or thinking or incoming_calls or chunk.get("done") is True:
                    if guard is not None:
                        guard.mark_meaningful()
                if thinking:
                    thinking_chunks.append(thinking)
                    state["reasoning_availability"] = "available"
                    state["reasoning_length"] += len(thinking)
                    if on_stream_event is not None:
                        await on_stream_event(
                            StreamEvent(
                                kind=KIND_THINKING, summary=thinking[:400], raw_delta=thinking
                            )
                        )
                if content:
                    text_chunks.append(content)
                    state["text_provided"] = True
                    state["text_length"] += len(content)
                    if on_stream_event is not None:
                        await on_stream_event(
                            StreamEvent(kind=KIND_TEXT_DELTA, summary=content)
                        )
                for incoming in incoming_calls:
                    if not isinstance(incoming, Mapping):
                        raise httpx.RemoteProtocolError("Ollama returned an invalid tool call")
                    function = incoming.get("function")
                    if not isinstance(function, Mapping):
                        raise httpx.RemoteProtocolError("Ollama tool call has no function")
                    arguments = function.get("arguments")
                    call = {
                        "id": str(incoming.get("id") or "ollama_" + uuid4().hex),
                        "type": "function",
                        "function": {
                            "name": function.get("name"),
                            "arguments": (
                                arguments if isinstance(arguments, str)
                                else json.dumps(arguments, ensure_ascii=False)
                            ),
                        },
                    }
                    tool_calls.append(call)
                    wire_evidence["tool_call_fragments"].append(
                        {"ndjson_event_arrival": arrival, "native_tool_call": dict(incoming)}
                    )
                    state["tool_calls"] = _tool_call_protocol_summary(tool_calls)[0]
                    wire_evidence["assembly_snapshots"].append(
                        {"ndjson_event_arrival": arrival, "tool_calls": list(state["tool_calls"])}
                    )
                if chunk.get("done") is True:
                    final_chunk = chunk
                    break
            if final_chunk is None:
                raise httpx.RemoteProtocolError("Ollama response ended without done=true")
        except BaseException as exc:
            _annotate_stream_exception(exc, state)
            raise

    native_reason = final_chunk.get("done_reason")
    finish_reason = "tool_calls" if tool_calls and native_reason == "stop" else native_reason
    translated = finish_reason != native_reason
    compatibility = {"native_done_reason": native_reason, "native_done": True}
    compatibility["native_performance"] = _native_performance(
        final_chunk, first_response_ms, first_answer_ms
    )
    compatibility["native_request_controls"] = {
        name: value for name, value in (payload.get("options") or {}).items()
        if name in {"num_ctx", "num_predict", "num_batch", "num_gpu"}
    }
    if "keep_alive" in payload:
        compatibility["native_request_controls"]["keep_alive_seconds"] = payload["keep_alive"]
    if translated:
        compatibility["finish_reason_translation"] = "native_stop_with_structured_tool_calls"
    return _APIResult(
        text="".join(text_chunks),
        reasoning_content="".join(thinking_chunks),
        tool_calls=tool_calls or None,
        finish_reason=finish_reason,
        raw_finish_reason=finish_reason,
        finish_reason_present=bool(finish_reason),
        finish_reason_source=(
            "ollama_native_translation" if translated
            else ("provider" if native_reason else "missing")
        ),
        prompt_tokens=int(final_chunk.get("prompt_eval_count") or 0),
        completion_tokens=int(final_chunk.get("eval_count") or 0),
        transport_request_id=state["transport_request_id"],
        transport_complete=True,
        transport_state="native_done_marker",
        stream_done=bool(payload.get("stream")),
        reasoning_state="available" if thinking_chunks else "unavailable",
        provider_compatibility=compatibility,
        wire_evidence=wire_evidence,
    )
