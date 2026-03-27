from __future__ import annotations
"""
DeepSeek API adapter — OpenAI-compatible, inherits from OpenRouterAdapter.

Differences from OpenRouter:
  - Endpoint: https://api.deepseek.com/v1/chat/completions
  - No OpenRouter-specific headers (HTTP-Referer, X-Title)
  - Reasoning content field: "reasoning_content" (not "reasoning")
  - No payload reasoning toggle — model name (deepseek-reasoner) controls it
"""

import asyncio
import json

from adapters.openrouter_api import OpenRouterAdapter, _APIResult
from adapters.stream_events import KIND_THINKING, StreamEvent

_DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"


class DeepSeekAdapter(OpenRouterAdapter):

    def _build_payload(self, messages: list[dict], use_streaming: bool = False) -> dict:
        payload: dict = {
            "model": self.config.model,
            "messages": messages,
        }
        if use_streaming:
            payload["stream"] = True
        if self.tool_registry:
            tool_defs = self.tool_registry.get_tool_definitions()
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
        ai_text = message.get("content") or ""

        # DeepSeek uses "reasoning_content" for thinking tokens
        if on_stream_event is not None:
            reasoning = str(message.get("reasoning_content") or "").strip()
            if reasoning:
                await on_stream_event(StreamEvent(kind=KIND_THINKING, summary=reasoning[:400]))

        tool_calls = message.get("tool_calls") or None
        return _APIResult(text=ai_text, tool_calls=tool_calls, finish_reason=finish_reason)

    async def _stream_api_once(self, payload, headers, on_stream_event) -> _APIResult:
        text_chunks: list[str] = []
        tool_calls_acc: dict[int, dict] = {}
        finish_reason = "stop"

        async with self.client.stream("POST", _DEEPSEEK_URL, json=payload, headers=headers) as response:
            response.raise_for_status()

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

                choices = data.get("choices", [])
                if not choices:
                    continue

                choice = choices[0]
                delta = choice.get("delta", {})
                finish_reason = choice.get("finish_reason") or finish_reason

                # DeepSeek streams thinking in "reasoning_content"
                reasoning_text = str(delta.get("reasoning_content") or "").strip()
                if reasoning_text and on_stream_event:
                    asyncio.create_task(
                        on_stream_event(StreamEvent(kind=KIND_THINKING, summary=reasoning_text[:400]))
                    )

                content = delta.get("content", "")
                if content:
                    text_chunks.append(content)
                    if on_stream_event:
                        from adapters.stream_events import KIND_TEXT_DELTA
                        asyncio.create_task(
                            on_stream_event(StreamEvent(kind=KIND_TEXT_DELTA, summary=content[:120]))
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

        full_text = "".join(text_chunks)
        tool_calls = list(tool_calls_acc.values()) if tool_calls_acc else None
        return _APIResult(text=full_text, tool_calls=tool_calls, finish_reason=finish_reason)

    async def generate_response(self, prompt, request_id, is_retry=False, silent=False, on_stream_event=None):
        # Inject DeepSeek headers instead of OpenRouter's
        import time
        started = time.perf_counter()
        self._ensure_client()

        use_streaming = on_stream_event is not None
        max_loops = self.tool_registry.max_loops if self.tool_registry else 1

        messages = [
            {"role": "system", "content": self.sys_prompt},
            {"role": "user", "content": prompt},
        ]
        headers = self._deepseek_headers()
        last_text = ""
        result = None

        try:
            self._touch_activity()
            for loop_idx in range(max_loops):
                payload = self._build_payload(messages, use_streaming=use_streaming)
                if use_streaming:
                    result = await self._stream_api_once(payload, headers, on_stream_event)
                else:
                    result = await self._call_api_once(payload, headers, on_stream_event)

                last_text = result.text
                if not result.tool_calls or not self.tool_registry:
                    break

                assistant_msg: dict = {"role": "assistant"}
                if result.text:
                    assistant_msg["content"] = result.text
                assistant_msg["tool_calls"] = result.tool_calls
                messages.append(assistant_msg)
                await self._run_tool_calls(result.tool_calls, messages, on_stream_event)

                if loop_idx == max_loops - 1:
                    self.logger.warning(f"Tool loop limit ({max_loops}) reached for {request_id}")

            from adapters.base import BackendResponse
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            return BackendResponse(
                text=last_text,
                duration_ms=duration_ms,
                is_success=True,
                stop_reason=result.finish_reason if result else "stop",
            )

        except Exception as e:
            from adapters.base import BackendResponse
            import asyncio as _asyncio
            if isinstance(e, _asyncio.CancelledError):
                raise
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            return BackendResponse(text="", duration_ms=duration_ms, error=str(e), is_success=False)
