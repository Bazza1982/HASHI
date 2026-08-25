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
    _MEDIA_FALLBACK_TOOL_NAMES,
    OpenRouterAdapter,
    _APIResult,
    _assistant_content_text,
    _backend_failure_response,
    _message_structured_data,
    _stream_error_exception,
    _usage_cost_usd,
    _usage_thinking_tokens,
)
from adapters.stream_events import (
    KIND_TEXT_DELTA,
    KIND_THINKING,
    StreamCallback,
    StreamEvent,
)
from orchestrator.multimodal_contract import (
    attachment_manifest,
    native_attachment_reference_aliases,
    normalize_request_content,
)

_DEFAULT_HASHI_API_BASE_URL = "http://10.255.255.254:18801/v1"
_HASHI_REASONING_EFFORTS = frozenset(
    {"none", "low", "medium", "high", "xhigh", "max"}
)
_HASHI_REASONING_DISABLED_VALUES = frozenset(
    {"off", "false", "0", "disabled"}
)


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

    def _trace(self, message: str, *args: Any) -> None:
        """Emit best-effort observability without entering the request boundary."""

        logger = getattr(self, "logger", None)
        log = getattr(logger, "info", None) or getattr(logger, "debug", None)
        if not callable(log):
            return
        try:
            log(message, *args)
        except Exception:  # noqa: BLE001 - diagnostics must never alter execution
            return

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
        self.effort = str(extra.get("effort") or "medium").strip().casefold()
        configured = str(
            extra.get("hashi_api_url")
            or extra.get("base_url")
            or _provider_base_url(global_config)
        ).strip().rstrip("/")
        self.hashi_url = f"{configured}/chat/completions"

    def _hashi_reasoning_effort(self) -> str:
        """Resolve provider reasoning without losing HER's request-time override."""

        extra = dict(getattr(self.config, "extra", None) or {})
        configured = extra.get("provider_reasoning")
        if configured is None:
            configured = extra.get("reasoning_effort")
        if configured is None and self.reasoning_enabled is False:
            configured = "none"
        if configured is None:
            configured = self.effort or extra.get("effort") or "medium"

        normalized = str(configured).strip().casefold()
        if normalized in _HASHI_REASONING_DISABLED_VALUES:
            normalized = "none"
        if normalized not in _HASHI_REASONING_EFFORTS:
            raise ValueError(
                "HASHI reasoning effort must be one of: "
                + ", ".join(sorted(_HASHI_REASONING_EFFORTS))
            )
        return normalized

    def _build_payload(
        self,
        messages: list[dict],
        use_streaming: bool = False,
        tool_tiers: list[str] | None = ...,
        *,
        excluded_tool_names: frozenset[str] = frozenset(),
    ) -> dict:
        payload = super()._build_payload(
            messages,
            use_streaming=use_streaming,
            tool_tiers=tool_tiers,
            excluded_tool_names=excluded_tool_names,
        )
        # OpenRouter's nested ``reasoning`` object is not part of HASHI's
        # Gateway contract. Send one request-scoped Codex effort instead.
        payload.pop("reasoning", None)
        payload["reasoning_effort"] = self._hashi_reasoning_effort()
        return payload

    def _hashi_headers(
        self,
        *,
        request_id: str | None = None,
        provider_call: int | None = None,
        after_tool_end: bool = False,
    ) -> dict:
        headers = {"Content-Type": "application/json"}
        if request_id:
            headers["X-Hashi-Correlation-ID"] = str(request_id)[:200]
        if provider_call is not None:
            headers["X-Hashi-Provider-Call"] = str(provider_call)
        headers["X-Hashi-After-Tool-End"] = "true" if after_tool_end else "false"
        return headers

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
        provider_activity_observed = False

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
                if not isinstance(data, Mapping):
                    continue

                stream_error = _stream_error_exception(
                    data,
                    request=response.request,
                    provider_activity_observed=provider_activity_observed,
                )
                if stream_error is not None:
                    raise stream_error

                if data.get("usage"):
                    stream_usage = data["usage"]
                    provider_activity_observed = True

                choices = data.get("choices", [])
                if not choices:
                    continue

                choice = choices[0]
                delta = choice.get("delta", {})
                finish_reason = choice.get("finish_reason") or finish_reason

                content = delta.get("content", "")
                reasoning_text = str(delta.get("reasoning") or "")
                tool_call_deltas = delta.get("tool_calls") or []
                if content or reasoning_text or tool_call_deltas or finish_reason:
                    provider_activity_observed = True

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

                for tc_delta in tool_call_deltas:
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
        request_content: Mapping[str, Any] | None = None,
    ) -> BackendResponse:
        started = time.perf_counter()
        self._ensure_client()

        use_streaming = on_stream_event is not None
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
        media_routing: tuple[dict[str, Any], ...] = ()
        media_fallback_attempted = False
        provider_attempt_count = 0

        try:
            self._touch_activity()
            messages, media_routing = self._initial_messages(prompt, request_content)
            self._last_media_routing = media_routing
            normalized_request_content = normalize_request_content(request_content)
            manifest = attachment_manifest(normalized_request_content)
            native_attachment_ids = {
                str(item.get("attachment_id") or "")
                for item in media_routing
                if str(item.get("route") or "") == "native"
            }
            native_local_refs = native_attachment_reference_aliases(
                manifest,
                native_attachment_ids,
            )
            all_media_native = bool(media_routing) and all(
                str(item.get("route") or "") == "native" for item in media_routing
            )

            for loop_idx in count():
                while True:
                    provider_attempt_count += 1
                    headers = self._hashi_headers(
                        request_id=request_id,
                        provider_call=provider_attempt_count,
                        after_tool_end=tool_loop_count > 0,
                    )
                    payload = self._build_payload(
                        messages,
                        use_streaming=use_streaming,
                        excluded_tool_names=(
                            _MEDIA_FALLBACK_TOOL_NAMES
                            if all_media_native
                            else frozenset()
                        ),
                    )

                    try:
                        self._trace(
                            "HASHI_API_TRACE provider_call_started "
                            "request_id=%s provider_call=%s after_tool_end=%s "
                            "streaming=%s model=%s",
                            request_id,
                            provider_attempt_count,
                            tool_loop_count > 0,
                            use_streaming,
                            self.config.model,
                        )
                        if use_streaming:
                            result = await self._stream_api_once(
                                payload,
                                headers,
                                on_stream_event,
                            )
                        else:
                            result = await self._call_api_once(
                                payload,
                                headers,
                                on_stream_event,
                            )
                    except Exception as exc:
                        if not self._can_replay_typed_media_fallback(
                            exc,
                            media_routing=media_routing,
                            fallback_attempted=media_fallback_attempted,
                            provider_call_count=provider_call_count,
                            tool_call_count=total_tool_calls,
                        ):
                            raise
                        media_fallback_attempted = True
                        self._enable_request_local_media_fallback(
                            native_attachment_ids
                        )
                        messages = self._typed_media_fallback_messages(
                            prompt,
                            request_content,
                        )
                        media_routing = self._typed_media_fallback_routing(
                            media_routing
                        )
                        self._last_media_routing = media_routing
                        native_attachment_ids = set()
                        native_local_refs = set()
                        all_media_native = False
                        continue
                    break

                self._trace(
                    "HASHI_API_TRACE provider_call_completed "
                    "request_id=%s provider_call=%s after_tool_end=%s "
                    "finish_reason=%s tool_calls=%s",
                    request_id,
                    provider_attempt_count,
                    tool_loop_count > 0,
                    result.finish_reason,
                    len(result.tool_calls or ()),
                )

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

                await self._run_tool_calls(
                    result.tool_calls,
                    messages,
                    on_stream_event,
                    native_attachment_ids=native_attachment_ids,
                    native_local_refs=native_local_refs,
                    all_media_native=all_media_native,
                )
                self._trace(
                    "HASHI_API_TRACE tool_round_completed "
                    "request_id=%s tool_round=%s tool_calls=%s",
                    request_id,
                    tool_loop_count,
                    len(result.tool_calls),
                )

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
                    "multimodal_routing": list(media_routing),
                    "multimodal_fallback_attempted": media_fallback_attempted,
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
            failure = _backend_failure_response(
                e,
                duration_ms=duration_ms,
                tool_call_count=total_tool_calls,
                tool_loop_count=tool_loop_count,
            )
            metadata = dict(failure.stream_metadata or {})
            metadata["multimodal_routing"] = list(media_routing)
            metadata["multimodal_fallback_attempted"] = media_fallback_attempted
            attachment_id = str(getattr(e, "attachment_id", "") or "")
            if attachment_id:
                metadata["attachment_id"] = attachment_id
            failure.stream_metadata = metadata
            return failure
