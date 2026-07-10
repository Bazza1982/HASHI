from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from adapters.openrouter_api import OpenRouterAdapter, _APIResult
from adapters.stream_events import KIND_TEXT_DELTA, KIND_THINKING, StreamEvent
from adapters.xai_imagine import generate_xai_image, is_imagine_image_model
from adapters.xai_oauth_credentials import (
    DEFAULT_XAI_BASE_URL,
    XaiOAuthCredentialError,
    resolve_xai_credentials,
)

_RESPONSES_MODEL_PREFIXES = ("grok-build", "grok-4.5")
_AUTH_RETRY_STATUSES = {401, 403}


class XaiApiAdapter(OpenRouterAdapter):
    """xAI HTTP backend with Hermes OAuth auto-refresh or static API key."""

    def __init__(self, agent_config, global_config, api_key: str = None):
        super().__init__(agent_config, global_config, api_key)
        self.logger = self.logger.getChild("XaiApi") if hasattr(self.logger, "getChild") else self.logger
        self._bearer_token = ""
        self._oauth_refresh_token: str | None = None
        self._base_url = DEFAULT_XAI_BASE_URL
        self._credential_source = "init"
        self._apply_api_key_input(api_key)

    def _apply_api_key_input(self, api_key: Any) -> None:
        if isinstance(api_key, dict):
            self._bearer_token = str(
                api_key.get("api_key") or api_key.get("xai_api_key") or ""
            ).strip()
            refresh = str(
                api_key.get("oauth_refresh_token") or api_key.get("xai_oauth_refresh_token") or ""
            ).strip()
            self._oauth_refresh_token = refresh or None
        else:
            self._bearer_token = str(api_key or "").strip()
            self._oauth_refresh_token = None

    def _hermes_home(self) -> str | None:
        return str(getattr(self.global_config, "hermes_home", "") or "").strip() or None

    def _configured_base_url(self) -> str:
        configured = str(getattr(self.global_config, "xai_api_base_url", "") or "").strip()
        return configured.rstrip("/") if configured else DEFAULT_XAI_BASE_URL

    def _use_responses_api(self) -> bool:
        if getattr(self.global_config, "xai_use_responses_api", False):
            return True
        model = str(self.config.model or "")
        return any(model.startswith(prefix) for prefix in _RESPONSES_MODEL_PREFIXES)

    async def _resolve_bearer(self, *, force_refresh: bool = False) -> None:
        creds = await asyncio.to_thread(
            resolve_xai_credentials,
            static_api_key=self._bearer_token or None,
            oauth_refresh_token=self._oauth_refresh_token,
            hermes_home=self._hermes_home(),
            base_url=self._configured_base_url(),
            force_refresh=force_refresh,
        )
        self._bearer_token = creds.api_key
        self._base_url = creds.base_url
        self._credential_source = creds.source
        self.api_key = creds.api_key

    async def initialize(self) -> bool:
        self.config.workspace_dir.mkdir(parents=True, exist_ok=True)
        try:
            await self._resolve_bearer()
        except XaiOAuthCredentialError as exc:
            self.logger.error("xAI credential resolution failed: %s", exc)
            return False
        self._ensure_client()
        try:
            if self.config.system_md:
                from pathlib import Path

                if Path(self.config.system_md).exists():
                    self.sys_prompt = Path(self.config.system_md).read_text(encoding="utf-8")
        except Exception as exc:
            self.logger.warning("Could not read system_md: %s", exc)
        self.logger.info("xAI API adapter initialized (source=%s)", self._credential_source)
        return True

    def _xai_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._bearer_token}",
            "Content-Type": "application/json",
        }

    def _chat_url(self) -> str:
        return f"{self._base_url.rstrip('/')}/chat/completions"

    def _responses_url(self) -> str:
        return f"{self._base_url.rstrip('/')}/responses"

    def _api_url(self) -> str:
        return self._responses_url() if self._use_responses_api() else self._chat_url()

    def _build_payload(self, messages: list[dict], use_streaming: bool = False,
                       tool_tiers: list[str] | None = ...) -> dict:
        if self._use_responses_api():
            return self._build_responses_payload(messages, use_streaming=use_streaming)
        payload: dict = {
            "model": self.config.model,
            "messages": messages,
        }
        if use_streaming:
            payload["stream"] = True
            payload["stream_options"] = {"include_usage": True}
        if self.tool_registry:
            tiers = self.DEFAULT_TOOL_TIERS if tool_tiers is ... else tool_tiers
            tool_defs = self.tool_registry.get_tool_definitions(tiers=tiers)
            if tool_defs:
                payload["tools"] = tool_defs
        return payload

    def _build_responses_payload(self, messages: list[dict], *, use_streaming: bool = False) -> dict:
        parts: list[str] = []
        for message in messages:
            role = str(message.get("role") or "user")
            content = str(message.get("content") or "").strip()
            if not content:
                continue
            if role == "system":
                parts.append(f"System: {content}")
            elif role == "assistant":
                parts.append(f"Assistant: {content}")
            else:
                parts.append(content)
        payload: dict[str, Any] = {
            "model": self.config.model,
            "input": "\n\n".join(parts),
        }
        if use_streaming:
            payload["stream"] = True
        return payload

    @staticmethod
    def _extract_responses_text(data: dict[str, Any]) -> str:
        text = str(data.get("output_text") or "").strip()
        if text:
            return text

        chunks: list[str] = []
        for item in data.get("output") or []:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "")
            if item_type == "message":
                for part in item.get("content") or []:
                    if not isinstance(part, dict):
                        continue
                    part_type = str(part.get("type") or "")
                    if part_type in {"output_text", "text"}:
                        chunks.append(str(part.get("text") or ""))
            elif item_type in {"output_text", "text"}:
                chunks.append(str(item.get("text") or ""))
        return "".join(chunks).strip()

    def _parse_api_body(self, data: dict[str, Any]) -> _APIResult:
        if self._use_responses_api():
            ai_text = self._extract_responses_text(data)
            usage = data.get("usage") or {}
            return _APIResult(
                text=ai_text,
                tool_calls=None,
                finish_reason=str(data.get("status") or "stop"),
                prompt_tokens=int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("output_tokens") or usage.get("completion_tokens") or 0),
                thinking_tokens=0,
            )

        choices = data.get("choices") or []
        if not choices:
            return _APIResult(text="", tool_calls=None, finish_reason="error")

        choice = choices[0]
        message = choice.get("message") or {}
        finish_reason = choice.get("finish_reason") or "stop"
        ai_text = message.get("content") or ""
        reasoning_content = str(message.get("reasoning_content") or message.get("reasoning") or "")
        tool_calls = message.get("tool_calls") or None
        usage = data.get("usage") or {}
        comp_details = usage.get("completion_tokens_details") or {}
        result = _APIResult(
            text=ai_text,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            thinking_tokens=comp_details.get("reasoning_tokens", 0),
        )
        result.reasoning_content = reasoning_content
        return result

    async def _call_api_once(self, payload, headers, on_stream_event) -> _APIResult:
        response = await self.client.post(self._api_url(), json=payload, headers=headers)
        if response.status_code in _AUTH_RETRY_STATUSES:
            await self._resolve_bearer(force_refresh=True)
            headers = self._xai_headers()
            response = await self.client.post(self._api_url(), json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()
        result = self._parse_api_body(data)

        if on_stream_event is not None:
            reasoning = str(getattr(result, "reasoning_content", "") or "").strip()
            if reasoning:
                await on_stream_event(StreamEvent(kind=KIND_THINKING, summary=reasoning[:400]))
            if result.text:
                await on_stream_event(StreamEvent(kind=KIND_TEXT_DELTA, summary=result.text))

        return result

    async def _stream_api_once(self, payload, headers, on_stream_event) -> _APIResult:
        import json

        if self._use_responses_api():
            return await self._call_api_once(payload, headers, on_stream_event)

        text_chunks: list[str] = []
        reasoning_chunks: list[str] = []
        tool_calls_acc: dict[int, dict] = {}
        finish_reason = "stop"
        stream_usage: dict = {}

        async def _read_stream(response) -> None:
            nonlocal finish_reason, stream_usage
            async for line in response.aiter_lines():
                self._touch_activity()
                if not line.startswith("data: "):
                    continue
                data_str = line[6:].strip()
                if data_str == "[DONE]":
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

                reasoning_delta = str(
                    delta.get("reasoning_content") or delta.get("reasoning") or ""
                )
                if reasoning_delta:
                    reasoning_chunks.append(reasoning_delta)
                    if on_stream_event:
                        asyncio.create_task(
                            on_stream_event(
                                StreamEvent(kind=KIND_THINKING, summary=reasoning_delta[:400])
                            )
                        )

                content = delta.get("content", "")
                if content:
                    text_chunks.append(content)
                    if on_stream_event:
                        asyncio.create_task(
                            on_stream_event(StreamEvent(kind=KIND_TEXT_DELTA, summary=content))
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

        async with self.client.stream(
            "POST", self._api_url(), json=payload, headers=headers
        ) as response:
            if response.status_code in _AUTH_RETRY_STATUSES:
                await self._resolve_bearer(force_refresh=True)
                async with self.client.stream(
                    "POST",
                    self._api_url(),
                    json=payload,
                    headers=self._xai_headers(),
                ) as retry_response:
                    retry_response.raise_for_status()
                    await _read_stream(retry_response)
            else:
                response.raise_for_status()
                await _read_stream(response)

        full_text = "".join(text_chunks)
        reasoning_content = "".join(reasoning_chunks)
        tool_calls = list(tool_calls_acc.values()) if tool_calls_acc else None
        comp_details = stream_usage.get("completion_tokens_details") or {}
        result = _APIResult(
            text=full_text,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            prompt_tokens=stream_usage.get("prompt_tokens", 0),
            completion_tokens=stream_usage.get("completion_tokens", 0),
            thinking_tokens=comp_details.get("reasoning_tokens", 0),
        )
        result.reasoning_content = reasoning_content
        return result

    async def _generate_imagine_response(self, prompt: str, started: float, on_stream_event=None):
        from adapters.base import BackendResponse

        try:
            result = await generate_xai_image(
                prompt=prompt,
                model=str(self.config.model or ""),
                bearer_token=self._bearer_token or None,
                oauth_refresh_token=self._oauth_refresh_token,
                hermes_home=self._hermes_home(),
                base_url=self._base_url,
            )
            lines = [f"Generated {len(result.urls)} image(s) with {result.model}:"]
            lines.extend(result.urls)
            text = "\n".join(lines)
            if on_stream_event is not None and text:
                await on_stream_event(StreamEvent(kind=KIND_TEXT_DELTA, summary=text))
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            return BackendResponse(
                text=text,
                duration_ms=duration_ms,
                is_success=True,
                stop_reason="stop",
            )
        except Exception as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            return BackendResponse(
                text="",
                duration_ms=duration_ms,
                error=str(exc),
                is_success=False,
            )

    async def generate_response(self, prompt, request_id, is_retry=False, silent=False, on_stream_event=None):
        started = time.perf_counter()
        self._ensure_client()
        await self._resolve_bearer()

        if is_imagine_image_model(self.config.model):
            return await self._generate_imagine_response(prompt, started, on_stream_event)

        use_streaming = on_stream_event is not None and not self._use_responses_api()
        max_loops = self.tool_registry.max_loops if self.tool_registry else 1

        messages = [
            {"role": "system", "content": self.sys_prompt},
            {"role": "user", "content": prompt},
        ]
        headers = self._xai_headers()
        last_text = ""
        result = None
        total_prompt = 0
        total_completion = 0
        total_thinking = 0
        total_tool_calls = 0
        tool_loop_count = 0

        try:
            self._touch_activity()
            for loop_idx in range(max_loops):
                payload = self._build_payload(messages, use_streaming=use_streaming)
                if use_streaming:
                    result = await self._stream_api_once(payload, headers, on_stream_event)
                else:
                    result = await self._call_api_once(payload, headers, on_stream_event)

                headers = self._xai_headers()
                total_prompt += result.prompt_tokens
                total_completion += result.completion_tokens
                total_thinking += result.thinking_tokens

                last_text = result.text
                if not result.tool_calls or not self.tool_registry:
                    break

                tool_loop_count += 1
                total_tool_calls += len(result.tool_calls)
                assistant_msg: dict = {"role": "assistant"}
                if result.text:
                    assistant_msg["content"] = result.text
                reasoning_content = getattr(result, "reasoning_content", "")
                if reasoning_content:
                    assistant_msg["reasoning_content"] = reasoning_content
                assistant_msg["tool_calls"] = result.tool_calls
                messages.append(assistant_msg)
                await self._run_tool_calls(result.tool_calls, messages, on_stream_event)

                if loop_idx == max_loops - 1:
                    result = await self._call_final_after_tool_loop_limit(
                        messages, headers, use_streaming, on_stream_event,
                        request_id, max_loops
                    )
                    total_prompt += result.prompt_tokens
                    total_completion += result.completion_tokens
                    total_thinking += result.thinking_tokens
                    last_text = result.text
                    break

            from adapters.base import BackendResponse, TokenUsage

            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            usage = TokenUsage(
                input_tokens=total_prompt,
                output_tokens=total_completion,
                thinking_tokens=total_thinking,
            ) if (total_prompt or total_completion) else None
            return BackendResponse(
                text=last_text,
                duration_ms=duration_ms,
                is_success=True,
                stop_reason=result.finish_reason if result else "stop",
                usage=usage,
                tool_call_count=total_tool_calls,
                tool_loop_count=tool_loop_count,
            )

        except Exception as e:
            from adapters.base import BackendResponse

            if isinstance(e, asyncio.CancelledError):
                raise
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            return BackendResponse(text="", duration_ms=duration_ms, error=str(e), is_success=False)
