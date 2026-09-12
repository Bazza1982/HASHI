"""
DeepSeek API adapter — OpenAI-compatible, inherits from OpenRouterAdapter.

Differences from OpenRouter:
  - Endpoint: https://api.deepseek.com/v1/chat/completions
  - No OpenRouter-specific headers (HTTP-Referer, X-Title)
  - Reasoning content field: "reasoning_content" (not "reasoning")
  - Current model IDs include deepseek-v4-flash, deepseek-v4-pro, and the
    exact vision-capable deepseek-v4-flash-vision-exp model
"""

from __future__ import annotations

import json
from collections.abc import Mapping

import httpx

from adapters.openrouter_api import (
    OpenRouterAdapter,
    _APIResult,
    _annotate_stream_exception,
    _assistant_content_text,
    _iter_provider_stream_lines,
    _message_structured_data,
    _provider_request_id,
    _read_http_error_body,
    _request_wire_evidence,
    _response_wire_evidence,
    _stream_error_exception,
    _tool_call_protocol_summary,
)
from adapters.stream_events import KIND_THINKING, StreamEvent

_DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"

HASHI_COMPACTION_CAPABILITIES = {
    "prompt_isolation": True,
    "tool_disablement": True,
    "semantic_reasoning": True,
    "local_or_slow": False,
}
HASHI_MODEL_CAPACITY_PROFILES = {
    "deepseek-v4-flash-vision-exp": {
        "context_window_tokens": 1_000_000,
        "capacity_provenance": "official_deepseek_api_docs_2026-08-21",
    },
    "deepseek-v4-flash": {
        "context_window_tokens": 1_000_000,
        "capacity_provenance": "official_deepseek_api_docs_2026-08-22",
    },
    "deepseek-v4-pro": {
        "context_window_tokens": 1_000_000,
        "capacity_provenance": "official_deepseek_api_docs_2026-08-22",
    },
}


def _with_reasoning_content(result: _APIResult, reasoning_content: str) -> _APIResult:
    # Keep provider augmentation explicit at the adapter boundary.
    result.reasoning_content = reasoning_content
    return result


def _optional_usage_int(usage: Mapping[str, object], key: str) -> int | None:
    value = usage.get(key)
    if value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _with_deepseek_cache_usage(
    result: _APIResult,
    usage: Mapping[str, object],
) -> _APIResult:
    """Attach official DeepSeek prompt-cache counters."""
    result.prompt_cache_hit_tokens = _optional_usage_int(
        usage, "prompt_cache_hit_tokens"
    )
    result.prompt_cache_miss_tokens = _optional_usage_int(
        usage, "prompt_cache_miss_tokens"
    )
    return result


class DeepSeekAdapter(OpenRouterAdapter):
    # DeepSeek's long-running tool conversations can occasionally lose the
    # current SSE call after earlier tool results have already been committed.
    # Retry only that unfinished HTTP call; the base adapter never replays the
    # completed tool loops.
    TRANSIENT_PROVIDER_CALL_RETRIES = 3

    def _provider_evidence_url(self) -> str:
        return _DEEPSEEK_URL

    def _request_headers(self) -> dict[str, str]:
        return self._deepseek_headers()

    def _augment_assistant_tool_message(
        self,
        assistant_msg: dict,
        result: _APIResult,
    ) -> None:
        reasoning_content = getattr(result, "reasoning_content", "")
        if reasoning_content:
            assistant_msg["reasoning_content"] = reasoning_content

    def _build_payload(
        self,
        messages: list[dict],
        use_streaming: bool = False,
        tool_tiers: list[str] | None = ...,
        *,
        excluded_tool_names: frozenset[str] = frozenset(),
        audio_output=None,
        allow_tools: bool = True,
    ) -> dict:
        # OpenRouter owns the optional native-audio request extension.  The
        # DeepSeek compatibility surface remains text/image-only, but accepts
        # the additive keyword so inherited request orchestration stays
        # substitutable when no audio output profile was selected.
        if audio_output is not None:
            raise ValueError("DeepSeek does not support native audio output")
        payload: dict = {
            "model": self.config.model,
            "messages": messages,
        }
        extra = getattr(self.config, "extra", None) or {}
        raw_reasoning = str(
            extra.get("provider_reasoning")
            or extra.get("reasoning_effort")
            or ""
        ).strip().lower()
        if raw_reasoning in {"off", "none", "false", "0", "disabled"}:
            payload["thinking"] = {"type": "disabled"}
        elif raw_reasoning:
            payload["thinking"] = {"type": "enabled"}
            payload["reasoning_effort"] = (
                "max" if raw_reasoning in {"max", "xhigh"} else "high"
            )
        if use_streaming:
            payload["stream"] = True
            payload["stream_options"] = {"include_usage": True}
        if allow_tools and self.tool_registry:
            tiers = self.DEFAULT_TOOL_TIERS if tool_tiers is ... else tool_tiers
            tool_defs = self.tool_registry.get_tool_definitions(tiers=tiers)
            if excluded_tool_names:
                tool_defs = [
                    item
                    for item in tool_defs
                    if str((item.get("function") or {}).get("name") or "")
                    not in excluded_tool_names
                ]
            if tool_defs:
                payload["tools"] = tool_defs
        return payload

    def _deepseek_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def _call_api_once(self, payload, headers, on_stream_event) -> _APIResult:
        response = await self.client.post(_DEEPSEEK_URL, json=payload, headers=headers)
        await _read_http_error_body(response)
        response.raise_for_status()
        try:
            response_request = response.request
        except (AttributeError, RuntimeError):
            response_request = None
        wire_evidence = {
            "transport": "json",
            "request": _request_wire_evidence(payload, response_request),
            "raw_response": _response_wire_evidence(response),
            "sse_events": [],
            "tool_call_fragments": [],
            "assembly_snapshots": [],
        }
        data = response.json()
        choices = data.get("choices") or []
        if not choices:
            return _APIResult(
                text="",
                tool_calls=None,
                finish_reason=None,
                raw_finish_reason=None,
                finish_reason_present=False,
                finish_reason_source="missing",
                provider_response_id=str(data.get("id") or ""),
                transport_request_id=_provider_request_id(response),
                transport_state="complete_response_no_choices",
                wire_evidence=wire_evidence,
            )

        choice = choices[0]
        message = choice.get("message") or {}
        finish_reason_present = "finish_reason" in choice
        raw_finish_reason = choice.get("finish_reason")
        finish_reason = (
            str(raw_finish_reason).strip() or None
            if raw_finish_reason is not None
            else None
        )
        ai_text = _assistant_content_text(message.get("content"))
        reasoning_content = str(message.get("reasoning_content") or "")

        # DeepSeek uses "reasoning_content" for thinking tokens
        if on_stream_event is not None:
            reasoning = reasoning_content.strip()
            if reasoning:
                await on_stream_event(
                    StreamEvent(
                        kind=KIND_THINKING,
                        summary=reasoning[:400],
                        raw_delta=reasoning,
                    )
                )

        tool_calls = message.get("tool_calls") or None

        # Extract real token usage from DeepSeek API response
        usage = data.get("usage") or {}
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        # DeepSeek reports thinking tokens in prompt_tokens_details or completion_tokens_details
        thinking_tokens = 0
        comp_details = usage.get("completion_tokens_details") or {}
        thinking_tokens = comp_details.get("reasoning_tokens", 0)

        return _with_deepseek_cache_usage(
            _with_reasoning_content(
                _APIResult(
                    text=ai_text,
                    tool_calls=tool_calls,
                    finish_reason=finish_reason,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    thinking_tokens=thinking_tokens,
                    structured_data=_message_structured_data(message),
                    raw_finish_reason=raw_finish_reason,
                    finish_reason_present=finish_reason_present,
                    finish_reason_source=(
                        "provider"
                        if finish_reason is not None
                        else (
                            "provider_null" if finish_reason_present else "missing"
                        )
                    ),
                    provider_response_id=str(data.get("id") or ""),
                    transport_request_id=_provider_request_id(response),
                    reasoning_state=(
                        "available" if reasoning_content else "unavailable"
                    ),
                    wire_evidence=wire_evidence,
                ),
                reasoning_content,
            ),
            usage,
        )

    async def _stream_api_once(self, payload, headers, on_stream_event) -> _APIResult:
        text_chunks: list[str] = []
        reasoning_chunks: list[str] = []
        tool_calls_acc: dict[int, dict] = {}
        finish_reason = ""
        finish_reason_present = False
        finish_reason_field_seen = False
        raw_finish_reason = None
        stream_usage: dict = {}
        saw_done = False
        provider_response_id = ""
        transport_request_id = ""
        wire_evidence = {
            "transport": "sse",
            "request": _request_wire_evidence(payload),
            "sse_events": [],
            "tool_call_fragments": [],
            "assembly_snapshots": [],
        }
        protocol_state = {
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
        }
        protocol_state["wire_evidence"] = wire_evidence

        async with self.client.stream("POST", _DEEPSEEK_URL, json=payload, headers=headers) as response:
            await _read_http_error_body(response)
            response.raise_for_status()
            transport_request_id = _provider_request_id(response)
            protocol_state["transport_request_id"] = transport_request_id
            try:
                stream_request = response.request
            except (AttributeError, RuntimeError):
                stream_request = None
            wire_evidence["request"] = _request_wire_evidence(
                payload, stream_request
            )

            async for line in _iter_provider_stream_lines(response, protocol_state):
                self._touch_activity()
                event_arrival = len(wire_evidence["sse_events"]) + 1
                wire_evidence["sse_events"].append(
                    {"arrival": event_arrival, "raw_line": line}
                )
                if not line.startswith("data: "):
                    continue
                data_str = line[6:].strip()
                if data_str == "[DONE]":
                    saw_done = True
                    break

                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError as exc:
                    error = httpx.RemoteProtocolError(
                        "provider stream contained invalid JSON data"
                    )
                    raise _annotate_stream_exception(error, protocol_state) from exc
                if not isinstance(data, Mapping):
                    continue
                if data.get("id"):
                    provider_response_id = str(data.get("id"))
                    protocol_state["provider_response_id"] = provider_response_id

                stream_error = _stream_error_exception(
                    data,
                    request=stream_request,
                    provider_activity_observed=bool(
                        reasoning_chunks or text_chunks or tool_calls_acc
                    ),
                )
                if stream_error is not None:
                    raise _annotate_stream_exception(
                        stream_error, protocol_state
                    )

                if data.get("usage"):
                    stream_usage = data["usage"]

                choices = data.get("choices", [])
                if not choices:
                    continue

                choice = choices[0]
                delta = choice.get("delta", {})
                if "finish_reason" in choice:
                    finish_reason_field_seen = True
                    raw_finish_reason = choice.get("finish_reason")
                    if raw_finish_reason is not None and str(
                        raw_finish_reason
                    ).strip():
                        finish_reason = str(raw_finish_reason).strip()
                        finish_reason_present = True
                    protocol_state.update(
                        {
                            "raw_finish_reason_present": bool(
                                finish_reason_present
                            ),
                            "raw_finish_reason": (
                                finish_reason
                                if finish_reason_present
                                else raw_finish_reason
                            ),
                            "finish_reason_source": (
                                "provider"
                                if finish_reason_present
                                else "provider_null"
                            ),
                            "normalized_finish_reason": (
                                finish_reason.casefold()
                                if finish_reason_present
                                else "missing_finish_reason"
                            ),
                        }
                    )

                # DeepSeek streams thinking in "reasoning_content"
                reasoning_delta = str(delta.get("reasoning_content") or "")
                if reasoning_delta:
                    reasoning_chunks.append(reasoning_delta)
                    protocol_state["reasoning_availability"] = "available"
                    protocol_state["reasoning_length"] += len(reasoning_delta)
                if reasoning_delta and on_stream_event:
                    await on_stream_event(
                        StreamEvent(
                            kind=KIND_THINKING,
                            summary=reasoning_delta[:400],
                            raw_delta=reasoning_delta,
                        )
                    )

                content = delta.get("content", "")
                if content:
                    text_chunks.append(content)
                    protocol_state["text_provided"] = True
                    protocol_state["text_length"] += len(str(content))
                    if on_stream_event:
                        from adapters.stream_events import KIND_TEXT_DELTA
                        await on_stream_event(
                            StreamEvent(kind=KIND_TEXT_DELTA, summary=content)
                        )

                for tc_delta in (delta.get("tool_calls") or []):
                    idx = tc_delta.get("index", 0)
                    if idx not in tool_calls_acc:
                        tool_calls_acc[idx] = {
                            "id": tc_delta.get("id", ""),
                            "type": tc_delta.get("type", "function"),
                            "function": {"name": "", "arguments": ""},
                        }
                    acc = tool_calls_acc[idx]
                    if tc_delta.get("id"):
                        acc["id"] = tc_delta["id"]
                    fn_delta = tc_delta.get("function", {})
                    wire_evidence["tool_call_fragments"].append(
                        {
                            "arrival": len(wire_evidence["tool_call_fragments"]) + 1,
                            "sse_event_arrival": event_arrival,
                            "index": idx,
                            "id_fragment": tc_delta.get("id", ""),
                            "type_fragment": tc_delta.get("type", ""),
                            "name_fragment": fn_delta.get("name", ""),
                            "arguments_fragment": fn_delta.get("arguments", ""),
                        }
                    )
                    if fn_delta.get("name"):
                        acc["function"]["name"] += fn_delta["name"]
                    if fn_delta.get("arguments"):
                        acc["function"]["arguments"] += fn_delta["arguments"]
                    wire_evidence["assembly_snapshots"].append(
                        {
                            "arrival": len(wire_evidence["assembly_snapshots"]) + 1,
                            "after_fragment": len(wire_evidence["tool_call_fragments"]),
                            "index": idx,
                            "assembled": json.loads(
                                json.dumps(acc, ensure_ascii=False)
                            ),
                        }
                    )
                if tool_calls_acc:
                    protocol_state["tool_calls"] = _tool_call_protocol_summary(
                        list(tool_calls_acc.values())
                    )[0]

        if not saw_done and not finish_reason:
            error = httpx.RemoteProtocolError(
                "provider stream ended without a completion marker"
            )
            raise _annotate_stream_exception(error, protocol_state)
        full_text = "".join(text_chunks)
        reasoning_content = "".join(reasoning_chunks)
        tool_calls = list(tool_calls_acc.values()) if tool_calls_acc else None
        comp_details = stream_usage.get("completion_tokens_details") or {}
        return _with_deepseek_cache_usage(
            _with_reasoning_content(
                _APIResult(
                    text=full_text,
                    tool_calls=tool_calls,
                    finish_reason=finish_reason or None,
                    prompt_tokens=stream_usage.get("prompt_tokens", 0),
                    completion_tokens=stream_usage.get("completion_tokens", 0),
                    thinking_tokens=comp_details.get("reasoning_tokens", 0),
                    raw_finish_reason=(
                        finish_reason if finish_reason_present else raw_finish_reason
                    ),
                    finish_reason_present=finish_reason_present,
                    finish_reason_source=(
                        "provider"
                        if finish_reason_present
                        else (
                            "provider_null"
                            if finish_reason_field_seen
                            else "missing"
                        )
                    ),
                    provider_response_id=provider_response_id,
                    transport_request_id=transport_request_id,
                    transport_complete=True,
                    transport_state=(
                        "done_marker" if saw_done else "eof_after_finish_reason"
                    ),
                    stream_done=saw_done,
                    stream_eof=not saw_done,
                    reasoning_state=(
                        "available" if reasoning_content else "unavailable"
                    ),
                    wire_evidence=wire_evidence,
                ),
                reasoning_content,
            ),
            stream_usage,
        )

    async def generate_response(
        self,
        prompt,
        request_id,
        is_retry=False,
        silent=False,
        on_stream_event=None,
        request_content=None,
    ):
        return await super().generate_response(
            prompt,
            request_id,
            is_retry=is_retry,
            silent=silent,
            on_stream_event=on_stream_event,
            request_content=request_content,
        )
