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

from adapters.openrouter_api import (
    OpenRouterAdapter,
    _APIResult,
    _assistant_content_text,
    _message_structured_data,
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
    # /reboot min can reload this adapter while retaining the already-imported
    # OpenRouter _APIResult class. Attach dynamically so the DeepSeek fix works
    # with both the old and new dataclass shapes.
    result.reasoning_content = reasoning_content
    return result


class DeepSeekAdapter(OpenRouterAdapter):

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
        response.raise_for_status()
        data = response.json()
        choices = data.get("choices") or []
        if not choices:
            return _APIResult(text="", tool_calls=None, finish_reason="error")

        choice = choices[0]
        message = choice.get("message") or {}
        finish_reason = choice.get("finish_reason") or "stop"
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

        return _with_reasoning_content(
            _APIResult(
                text=ai_text, tool_calls=tool_calls, finish_reason=finish_reason,
                prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                thinking_tokens=thinking_tokens,
                structured_data=_message_structured_data(message),
            ),
            reasoning_content,
        )

    async def _stream_api_once(self, payload, headers, on_stream_event) -> _APIResult:
        text_chunks: list[str] = []
        reasoning_chunks: list[str] = []
        tool_calls_acc: dict[int, dict] = {}
        finish_reason = ""
        stream_usage: dict = {}
        saw_done = False

        async with self.client.stream("POST", _DEEPSEEK_URL, json=payload, headers=headers) as response:
            response.raise_for_status()

            async for line in response.aiter_lines():
                self._touch_activity()
                if not line.startswith("data: "):
                    continue
                data_str = line[6:].strip()
                if data_str == "[DONE]":
                    saw_done = True
                    break

                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                if data.get("usage"):
                    stream_usage = data["usage"]

                choices = data.get("choices", [])
                if not choices:
                    continue

                choice = choices[0]
                delta = choice.get("delta", {})
                finish_reason = choice.get("finish_reason") or finish_reason

                # DeepSeek streams thinking in "reasoning_content"
                reasoning_delta = str(delta.get("reasoning_content") or "")
                if reasoning_delta:
                    reasoning_chunks.append(reasoning_delta)
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
                    if fn_delta.get("name"):
                        acc["function"]["name"] += fn_delta["name"]
                    if fn_delta.get("arguments"):
                        acc["function"]["arguments"] += fn_delta["arguments"]

        if not saw_done and not finish_reason:
            import httpx

            raise httpx.RemoteProtocolError(
                "provider stream ended without a completion marker"
            )
        full_text = "".join(text_chunks)
        reasoning_content = "".join(reasoning_chunks)
        tool_calls = list(tool_calls_acc.values()) if tool_calls_acc else None
        comp_details = stream_usage.get("completion_tokens_details") or {}
        return _with_reasoning_content(
            _APIResult(
                text=full_text,
                tool_calls=tool_calls,
                finish_reason=finish_reason or "stop",
                prompt_tokens=stream_usage.get("prompt_tokens", 0),
                completion_tokens=stream_usage.get("completion_tokens", 0),
                thinking_tokens=comp_details.get("reasoning_tokens", 0),
            ),
            reasoning_content,
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
