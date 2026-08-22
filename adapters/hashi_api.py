"""HASHI API adapter — HASHI's own OpenAI-compatible gateway backend.

Endpoint: ``http://10.255.255.254:18801/v1/chat/completions`` (configurable via
``global.her_providers.providers.hashi.base_url``).

Differences from OpenRouter:
  - No API key required (the gateway authenticates at the bind/transport layer).
  - No OpenRouter-specific headers (HTTP-Referer / X-Title / Authorization).
  - Models are the gateway-exposed models (e.g. gpt-5.6-luna, gpt-5.6-sol).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Mapping
from itertools import count
from pathlib import Path
from typing import Any

import httpx

from adapters.base import BackendCapabilities, BackendResponse, TokenUsage
from adapters.openrouter_api import (
    OpenRouterAdapter,
    _APIResult,
    _assistant_content_text,
    _backend_failure_response,
    _message_structured_data,
    _usage_cost_usd,
    _usage_thinking_tokens,
)
from adapters.stream_events import (
    KIND_TEXT_DELTA,
    KIND_THINKING,
    StreamCallback,
    StreamEvent,
)

_DEFAULT_HASHI_API_BASE_URL = "http://10.255.255.254:18801/v1"


def _provider_base_url(global_config: Any) -> str:
    """Resolve the HASHI gateway base URL from the global provider profile."""
    her = getattr(global_config, "her_providers", None) or {}
    providers = her.get("providers") if isinstance(her, Mapping) else {}
    if isinstance(providers, Mapping):
        profile = providers.get("hashi")
        if isinstance(profile, Mapping):
            base = str(profile.get("base_url") or "").strip().rstrip("/")
            if base:
                return base
    return _DEFAULT_HASHI_API_BASE_URL


class HashiApiAdapter(OpenRouterAdapter):
    """Stateless OpenAI-compatible adapter for HASHI's own API gateway."""

    def _define_capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            supports_sessions=False,
            supports_files=False,
            supports_tool_use=True,
            supports_thinking_stream=True,
            supports_headless_mode=True,
            supports_progress_stream=True,
            supports_tool_stream=True,
            supports_answer_stream=True,
        )

    def __init__(self, agent_config, global_config, api_key: str | None = None):
        super().__init__(agent_config, global_config, api_key)
        self.logger = logging.getLogger(f"Backend.HashiApi.{self.config.name}")
        extra = getattr(self.config, "extra", None) or {}
        configured = str(
            extra.get("hashi_api_url")
            or extra.get("base_url")
            or _provider_base_url(global_config)
        ).strip().rstrip("/")
        self.hashi_url = f"{configured}/chat/completions"

    def _hashi_headers(self) -> dict:
        return {"Content-Type": "application/json"}

    async def initialize(self) -> bool:
        self.config.workspace_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_client()
        try:
            if self.config.system_md and Path(self.config.system_md).exists():
                self.sys_prompt = Path(self.config.system_md).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as e:
            self.logger.warning(f"Could not read system_md: {e}")
        self.logger.info("HASHI API adapter initialized in stateless mode.")
        return True

    async def _call_api_once(
        self,
        payload: dict,
        headers: dict,
        on_stream_event: StreamCallback,
    ) -> _APIResult:
        response = await self.client.post(self.hashi_url, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()
        choices = data.get("choices") or []
        if not choices:
            return _APIResult(text="", tool_calls=None, finish_reason="error")

        choice = choices[0]
        message = choice.get("message") or {}
        finish_reason = choice.get("finish_reason") or "stop"
        ai_text = _assistant_content_text(message.get("content"))

        if on_stream_event is not None:
            reasoning_text = str(message.get("reasoning") or "").strip()
            if reasoning_text:
                await on_stream_event(
                    StreamEvent(
                        kind=KIND_THINKING,
                        summary=reasoning_text[:400],
                        raw_delta=reasoning_text,
                    )
                )

        tool_calls = message.get("tool_calls") or None
        usage = data.get("usage") or {}
        return _APIResult(
            text=ai_text,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            thinking_tokens=_usage_thinking_tokens(usage),
            cost_usd=_usage_cost_usd(usage),
            structured_data=_message_structured_data(message),
        )

    async def _stream_api_once(
        self,
        payload: dict,
        headers: dict,
        on_stream_event: StreamCallback,
    ) -> _APIResult:
        text_chunks: list[str] = []
        tool_calls_acc: dict[int, dict] = {}
        finish_reason = ""
        stream_usage: dict = {}
        saw_done = False

        async with self.client.stream(
            "POST", self.hashi_url, json=payload, headers=headers
        ) as response:
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

                content = delta.get("content", "")
                reasoning_text = str(delta.get("reasoning") or "")

                if reasoning_text and on_stream_event:
                    await on_stream_event(
                        StreamEvent(
                            kind=KIND_THINKING,
                            summary=reasoning_text[:400],
                            raw_delta=reasoning_text,
                        )
                    )

                if content:
                    text_chunks.append(content)
                    if on_stream_event:
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
            raise httpx.RemoteProtocolError(
                "provider stream ended without a completion marker"
            )
        full_text = "".join(text_chunks)
        tool_calls = list(tool_calls_acc.values()) if tool_calls_acc else None
        return _APIResult(
            text=full_text,
            tool_calls=tool_calls,
            finish_reason=finish_reason or "stop",
            prompt_tokens=stream_usage.get("prompt_tokens", 0),
            completion_tokens=stream_usage.get("completion_tokens", 0),
            thinking_tokens=_usage_thinking_tokens(stream_usage),
            cost_usd=_usage_cost_usd(stream_usage),
        )

    async def generate_response(
        self,
        prompt: str,
        request_id: str,
        is_retry: bool = False,
        silent: bool = False,
        on_stream_event: StreamCallback = None,
    ) -> BackendResponse:
        started = time.perf_counter()
        self._ensure_client()

        use_streaming = on_stream_event is not None
        messages = [
            {"role": "system", "content": self.sys_prompt},
            {"role": "user", "content": prompt},
        ]

        headers = self._hashi_headers()

        last_text = ""
        last_structured_data = None
        total_prompt = 0
        total_completion = 0
        total_thinking = 0
        total_cost_usd = 0.0
        provider_call_count = 0
        provider_cost_complete = True
        provider_calls: list[dict[str, Any]] = []
        total_tool_calls = 0
        tool_loop_count = 0

        try:
            self._touch_activity()

            for loop_idx in count():
                payload = self._build_payload(messages, use_streaming=use_streaming)

                if use_streaming:
                    result = await self._stream_api_once(payload, headers, on_stream_event)
                else:
                    result = await self._call_api_once(payload, headers, on_stream_event)

                total_prompt += result.prompt_tokens
                total_completion += result.completion_tokens
                total_thinking += result.thinking_tokens
                provider_call_count += 1
                provider_calls.append(
                    {
                        "input": int(result.prompt_tokens or 0),
                        "output": int(result.completion_tokens or 0),
                        "thinking": int(result.thinking_tokens or 0),
                        "token_source": "provider",
                        "thinking_in_output": True,
                        "cost_usd": result.cost_usd,
                    }
                )
                if result.cost_usd is None:
                    provider_cost_complete = False
                else:
                    total_cost_usd += result.cost_usd

                last_text = result.text
                last_structured_data = result.structured_data

                if not result.tool_calls or not self.tool_registry:
                    break

                tool_loop_count += 1
                total_tool_calls += len(result.tool_calls)

                assistant_msg: dict = {"role": "assistant"}
                if result.text:
                    assistant_msg["content"] = result.text
                assistant_msg["tool_calls"] = result.tool_calls
                messages.append(assistant_msg)

                await self._run_tool_calls(result.tool_calls, messages, on_stream_event)

            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            usage = TokenUsage(
                input_tokens=total_prompt,
                output_tokens=total_completion,
                thinking_tokens=total_thinking,
            ) if (total_prompt or total_completion) else None
            return BackendResponse(
                text=last_text,
                duration_ms=duration_ms,
                structured_data=last_structured_data,
                is_success=True,
                stop_reason=result.finish_reason if "result" in dir() else "stop",
                usage=usage,
                cost_usd=(
                    round(total_cost_usd, 12)
                    if provider_call_count and provider_cost_complete
                    else None
                ),
                stream_metadata={
                    "meter": {"provider_calls": provider_calls},
                },
                tool_call_count=total_tool_calls,
                tool_loop_count=tool_loop_count,
            )

        except asyncio.CancelledError:
            self.logger.warning(f"Request cancelled for {request_id}")
            raise
        # Provider SDK/HTTP boundaries can raise backend-specific exception types;
        # convert all of them into the adapter's stable failure response.
        except Exception as e:  # noqa: BLE001
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            return _backend_failure_response(
                e,
                duration_ms=duration_ms,
                tool_call_count=total_tool_calls,
                tool_loop_count=tool_loop_count,
            )
