from __future__ import annotations
"""
Ollama API adapter — local model backend via Ollama's OpenAI-compatible API.

Differences from OpenRouter:
  - Endpoint: http://localhost:11434/v1/chat/completions (configurable)
  - No API key required
  - No OpenRouter-specific headers or reasoning toggles
  - Models are local Ollama models (e.g. gemma4:26b, qwen3:32b)
"""

import asyncio
import json
import logging
import time
from typing import Optional

import httpx

from adapters.openrouter_api import OpenRouterAdapter, _APIResult
from adapters.base import BackendCapabilities, BackendResponse
from adapters.stream_events import (
    KIND_TEXT_DELTA,
    KIND_THINKING,
    StreamCallback,
    StreamEvent,
)

_DEFAULT_OLLAMA_URL = "http://localhost:11434/v1/chat/completions"


class OllamaAdapter(OpenRouterAdapter):

    def __init__(self, agent_config, global_config, api_key: str = None):
        super().__init__(agent_config, global_config, api_key)
        self.logger = logging.getLogger(f"Backend.Ollama.{self.config.name}")
        # Allow override via agent extra config
        extra = getattr(self.config, "extra", {}) or {}
        self.ollama_url = extra.get("ollama_url", _DEFAULT_OLLAMA_URL)

    def _define_capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            supports_sessions=False,
            supports_files=False,
            supports_tool_use=True,
            supports_thinking_stream=True,
            supports_headless_mode=True,
        )

    async def initialize(self) -> bool:
        self.config.workspace_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_client()

        # Load system prompt
        try:
            from pathlib import Path
            if self.config.system_md and Path(self.config.system_md).exists():
                self.sys_prompt = Path(self.config.system_md).read_text(encoding="utf-8")
        except Exception as e:
            self.logger.warning(f"Could not read system_md: {e}")

        # Verify Ollama is reachable
        try:
            resp = await self.client.get("http://localhost:11434/api/tags")
            if resp.status_code == 200:
                models = [m["name"] for m in resp.json().get("models", [])]
                self.logger.info(f"Ollama connected. Available models: {models}")
            else:
                self.logger.warning(f"Ollama responded with status {resp.status_code}")
        except Exception as e:
            self.logger.warning(f"Could not connect to Ollama: {e} — will retry on first request")

        self.logger.info("Ollama adapter initialized.")
        return True

    # Default tiers for Ollama — keep payload small for local models.
    # The model can still *call* any allowed tool; we just don't advertise
    # all schemas every turn. Extra tiers are loaded on demand.
    DEFAULT_TOOL_TIERS = ["core"]

    def _build_payload(self, messages: list[dict], use_streaming: bool = False,
                       tool_tiers: list[str] | None = None) -> dict:
        payload: dict = {
            "model": self.config.model,
            "messages": messages,
            "options": {"num_ctx": 32768},
        }
        if use_streaming:
            payload["stream"] = True
        if self.tool_registry:
            tiers = tool_tiers or self.DEFAULT_TOOL_TIERS
            tool_defs = self.tool_registry.get_tool_definitions(tiers=tiers)
            if tool_defs:
                payload["tools"] = tool_defs
        return payload

    def _ollama_headers(self) -> dict:
        return {"Content-Type": "application/json"}

    async def _call_api_once(self, payload, headers, on_stream_event) -> _APIResult:
        response = await self.client.post(self.ollama_url, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()
        choices = data.get("choices") or []
        if not choices:
            return _APIResult(text="", tool_calls=None, finish_reason="error")

        choice = choices[0]
        message = choice.get("message") or {}
        finish_reason = choice.get("finish_reason") or "stop"
        ai_text = message.get("content") or ""

        # Emit reasoning/thinking if present
        if on_stream_event is not None:
            reasoning_text = str(message.get("reasoning") or "").strip()
            if reasoning_text:
                await on_stream_event(StreamEvent(kind=KIND_THINKING, summary=reasoning_text[:400]))

        tool_calls = message.get("tool_calls") or None
        return _APIResult(text=ai_text, tool_calls=tool_calls, finish_reason=finish_reason)

    async def _stream_api_once(self, payload, headers, on_stream_event) -> _APIResult:
        text_chunks: list[str] = []
        tool_calls_acc: dict[int, dict] = {}
        finish_reason = "stop"

        async with self.client.stream("POST", self.ollama_url, json=payload, headers=headers) as response:
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

                # Emit reasoning/thinking chunks
                reasoning_text = str(delta.get("reasoning") or "").strip()
                if reasoning_text and on_stream_event:
                    asyncio.create_task(
                        on_stream_event(StreamEvent(kind=KIND_THINKING, summary=reasoning_text[:400]))
                    )

                content = delta.get("content", "")
                if content:
                    text_chunks.append(content)
                    if on_stream_event:
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
        started = time.perf_counter()
        self._ensure_client()

        use_streaming = on_stream_event is not None
        max_loops = self.tool_registry.max_loops if self.tool_registry else 1

        messages = [
            {"role": "system", "content": self.sys_prompt},
            {"role": "user", "content": prompt},
        ]
        headers = self._ollama_headers()
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

            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            return BackendResponse(
                text=last_text,
                duration_ms=duration_ms,
                is_success=True,
                stop_reason=result.finish_reason if result else "stop",
            )

        except Exception as e:
            if isinstance(e, asyncio.CancelledError):
                raise
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            return BackendResponse(text="", duration_ms=duration_ms, error=str(e), is_success=False)
