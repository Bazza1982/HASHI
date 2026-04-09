from __future__ import annotations
import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx

from adapters.base import BaseBackend, BackendCapabilities, BackendResponse
from adapters.stream_events import (
    KIND_FILE_EDIT,
    KIND_FILE_READ,
    KIND_PROGRESS,
    KIND_SHELL_EXEC,
    KIND_TEXT_DELTA,
    KIND_THINKING,
    KIND_TOOL_END,
    KIND_TOOL_START,
    StreamCallback,
    StreamEvent,
)


@dataclass
class _APIResult:
    """Internal intermediate result from a single API call."""
    text: str
    tool_calls: Optional[list]   # None = no tool calls, just text
    finish_reason: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    thinking_tokens: int = 0


class OpenRouterAdapter(BaseBackend):
    MAX_TOOL_LOOPS = 25

    def _define_capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            supports_sessions=False,
            supports_files=False,
            supports_tool_use=True,
            supports_thinking_stream=True,
            supports_headless_mode=True,
        )

    def __init__(self, agent_config, global_config, api_key: str = None):
        super().__init__(agent_config, global_config, api_key)
        self.logger = logging.getLogger(f"Backend.OpenRouter.{self.config.name}")
        self.client = None
        self.sys_prompt = "You are a helpful AI assistant."
        self.reasoning_enabled = False
        self.tool_registry = None   # Injected by FlexibleBackendManager if tools configured

    def set_reasoning_enabled(self, enabled: bool) -> None:
        self.reasoning_enabled = bool(enabled)

    def _ensure_client(self):
        if self.client is None or getattr(self.client, "is_closed", False):
            self.client = httpx.AsyncClient(timeout=float(self.PROCESS_TIMEOUT_SEC))

    def _summarize_reasoning_detail(self, detail) -> str:
        if not isinstance(detail, dict):
            return ""
        detail_type = str(detail.get("type") or "").strip()
        if detail_type == "reasoning.text":
            return str(detail.get("text") or "").strip()
        if detail_type == "reasoning.summary":
            return str(detail.get("summary") or "").strip()
        if detail_type == "reasoning.encrypted":
            return "[Encrypted reasoning]"
        return (
            str(detail.get("text") or "").strip()
            or str(detail.get("summary") or "").strip()
        )

    async def initialize(self) -> bool:
        self.config.workspace_dir.mkdir(parents=True, exist_ok=True)
        if not self.api_key:
            self.logger.error("No OpenRouter API key provided in secrets.json")
            return False
        self._ensure_client()

        try:
            if self.config.system_md and Path(self.config.system_md).exists():
                self.sys_prompt = Path(self.config.system_md).read_text(encoding="utf-8")
        except Exception as e:
            self.logger.warning(f"Could not read system_md: {e}")

        self.logger.info("OpenRouter adapter initialized in stateless mode.")
        return True

    async def handle_new_session(self) -> bool:
        self.logger.info("OpenRouter backend is stateless. /new acknowledged.")
        return True

    async def get_key_info(self) -> dict | None:
        try:
            self._ensure_client()
            response = await self.client.get(
                "https://openrouter.ai/api/v1/key",
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            self.logger.error(f"Failed to fetch OpenRouter key info: {e}")
            return None

    # ------------------------------------------------------------------
    # Payload builder
    # ------------------------------------------------------------------

    # Default tiers for OpenRouter — None means send all allowed tools.
    # Subclasses (e.g. OllamaAdapter) override with smaller defaults.
    DEFAULT_TOOL_TIERS: list[str] | None = None

    def _build_payload(self, messages: list[dict], use_streaming: bool = False,
                       tool_tiers: list[str] | None = ...) -> dict:
        payload: dict = {
            "model": self.config.model,
            "messages": messages,
        }
        if self.reasoning_enabled:
            payload["reasoning"] = {"enabled": True, "exclude": False}
        if use_streaming:
            payload["stream"] = True
            payload["stream_options"] = {"include_usage": True}
        if self.tool_registry:
            tiers = self.DEFAULT_TOOL_TIERS if tool_tiers is ... else tool_tiers
            tool_defs = self.tool_registry.get_tool_definitions(tiers=tiers)
            if tool_defs:
                payload["tools"] = tool_defs
        return payload

    # ------------------------------------------------------------------
    # Stream event helper
    # ------------------------------------------------------------------

    async def _emit(self, on_stream_event: StreamCallback, kind: str, summary: str,
                    tool_name: str = "", file_path: str = "") -> None:
        if on_stream_event is None:
            return
        try:
            await on_stream_event(
                StreamEvent(kind=kind, summary=summary, tool_name=tool_name, file_path=file_path)
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Tool execution with stream events
    # ------------------------------------------------------------------

    async def _run_tool_calls(
        self,
        tool_calls: list[dict],
        messages: list[dict],
        on_stream_event: StreamCallback,
    ) -> None:
        """Execute all tool_calls and append tool result messages to `messages`."""
        for tc in tool_calls:
            fn = tc.get("function", {})
            tool_name = fn.get("name", "unknown")
            tc_id = tc.get("id", "")
            raw_args = fn.get("arguments", "{}")

            # Determine stream event kind
            if tool_name == "bash":
                evt_kind = KIND_SHELL_EXEC
            elif tool_name == "file_read":
                evt_kind = KIND_FILE_READ
            elif tool_name == "file_write":
                evt_kind = KIND_FILE_EDIT
            else:
                evt_kind = KIND_TOOL_START

            await self._emit(on_stream_event, KIND_TOOL_START,
                             f"Tool: {tool_name}", tool_name=tool_name)
            await self._emit(on_stream_event, evt_kind,
                             f"{tool_name}: {raw_args[:120]}", tool_name=tool_name)

            # Parse arguments
            try:
                arguments = json.loads(raw_args) if raw_args else {}
            except json.JSONDecodeError as e:
                result_text = f"Error: could not parse tool arguments: {e}"
                await self._emit(on_stream_event, KIND_TOOL_END,
                                 f"{tool_name}: argument parse error", tool_name=tool_name)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": result_text,
                })
                continue

            # Execute
            result = await self.tool_registry.execute(tool_name, arguments, tool_call_id=tc_id)

            output_preview = result.output[:100].replace("\n", " ")
            await self._emit(on_stream_event, KIND_TOOL_END,
                             f"{tool_name}: {output_preview}", tool_name=tool_name)

            messages.append({
                "role": "tool",
                "tool_call_id": tc_id,
                "content": result.output,
            })

    # ------------------------------------------------------------------
    # Non-streaming single API call
    # ------------------------------------------------------------------

    async def _call_api_once(
        self,
        payload: dict,
        headers: dict,
        on_stream_event: StreamCallback,
    ) -> _APIResult:
        response = await self.client.post(
            self.global_config.openrouter_url,
            json=payload,
            headers=headers,
        )
        response.raise_for_status()
        data = response.json()
        choices = data.get("choices") or []
        if not choices:
            return _APIResult(text="", tool_calls=None, finish_reason="error")

        choice = choices[0]
        message = choice.get("message") or {}
        finish_reason = choice.get("finish_reason") or "stop"
        ai_text = message.get("content") or ""

        # Emit reasoning if present
        if on_stream_event is not None:
            reasoning_text = str(message.get("reasoning") or "").strip()
            if reasoning_text:
                await on_stream_event(
                    StreamEvent(kind=KIND_THINKING, summary=reasoning_text[:400])
                )
            for detail in message.get("reasoning_details") or []:
                snippet = self._summarize_reasoning_detail(detail)
                if snippet:
                    await on_stream_event(
                        StreamEvent(kind=KIND_THINKING, summary=snippet[:400])
                    )

        tool_calls = message.get("tool_calls") or None

        # Extract real token usage from API response
        usage = data.get("usage") or {}
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        thinking_tokens = usage.get("thinking_tokens", 0)

        return _APIResult(
            text=ai_text, tool_calls=tool_calls, finish_reason=finish_reason,
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            thinking_tokens=thinking_tokens,
        )

    # ------------------------------------------------------------------
    # Streaming single API call (accumulates tool_calls deltas)
    # ------------------------------------------------------------------

    async def _stream_api_once(
        self,
        payload: dict,
        headers: dict,
        on_stream_event: StreamCallback,
    ) -> _APIResult:
        text_chunks: list[str] = []
        # tool_calls_acc: dict[int, dict] indexed by tool call index
        tool_calls_acc: dict[int, dict] = {}
        finish_reason = "stop"
        stream_usage: dict = {}  # usage from final streaming chunk

        async with self.client.stream(
            "POST",
            self.global_config.openrouter_url,
            json=payload,
            headers=headers,
        ) as response:
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

                # Capture usage from streaming chunks (sent in final chunk)
                if data.get("usage"):
                    stream_usage = data["usage"]

                choices = data.get("choices", [])
                if not choices:
                    continue

                choice = choices[0]
                delta = choice.get("delta", {})
                finish_reason = choice.get("finish_reason") or finish_reason

                # Text content
                content = delta.get("content", "")
                reasoning_text = str(delta.get("reasoning") or "").strip()
                reasoning_details = delta.get("reasoning_details") or []

                if reasoning_text and on_stream_event:
                    asyncio.create_task(
                        on_stream_event(StreamEvent(kind=KIND_THINKING, summary=reasoning_text[:400]))
                    )
                elif reasoning_details and on_stream_event:
                    for detail in reasoning_details:
                        snippet = self._summarize_reasoning_detail(detail)
                        if snippet:
                            asyncio.create_task(
                                on_stream_event(StreamEvent(kind=KIND_THINKING, summary=snippet[:400]))
                            )

                if content:
                    text_chunks.append(content)
                    if on_stream_event:
                        asyncio.create_task(
                            on_stream_event(StreamEvent(kind=KIND_TEXT_DELTA, summary=content[:120]))
                        )

                # Accumulate tool_calls deltas
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
        return _APIResult(
            text=full_text, tool_calls=tool_calls, finish_reason=finish_reason,
            prompt_tokens=stream_usage.get("prompt_tokens", 0),
            completion_tokens=stream_usage.get("completion_tokens", 0),
            thinking_tokens=stream_usage.get("thinking_tokens", 0),
        )

    # ------------------------------------------------------------------
    # Main generate_response with tool loop
    # ------------------------------------------------------------------

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
        max_loops = self.tool_registry.max_loops if self.tool_registry else 1

        messages = [
            {"role": "system", "content": self.sys_prompt},
            {"role": "user", "content": prompt},
        ]

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "HTTP-Referer": "https://github.com/Bazza1982/HASHI",
            "X-Title": "Bridge-U Orchestrator",
        }

        last_text = ""
        # Accumulate token usage across all tool loops
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

                # Accumulate usage from each API call
                total_prompt += result.prompt_tokens
                total_completion += result.completion_tokens
                total_thinking += result.thinking_tokens

                last_text = result.text

                # No tool calls — we're done
                if not result.tool_calls or not self.tool_registry:
                    break

                tool_loop_count += 1
                total_tool_calls += len(result.tool_calls)
                self.logger.debug(
                    f"Tool loop {loop_idx + 1}/{max_loops}: "
                    f"{len(result.tool_calls)} tool call(s)"
                )

                # Append assistant message (with tool_calls) to conversation
                assistant_msg: dict = {"role": "assistant"}
                if result.text:
                    assistant_msg["content"] = result.text
                assistant_msg["tool_calls"] = result.tool_calls
                messages.append(assistant_msg)

                # Execute tools, append results
                await self._run_tool_calls(result.tool_calls, messages, on_stream_event)

                # If this was the last allowed loop, break
                if loop_idx == max_loops - 1:
                    self.logger.warning(
                        f"Tool loop limit ({max_loops}) reached for request {request_id}"
                    )
                    if on_stream_event:
                        await self._emit(
                            on_stream_event, KIND_PROGRESS,
                            f"⚠️ Tool loop limit ({max_loops}) reached"
                        )

            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            from adapters.base import TokenUsage
            usage = TokenUsage(
                input_tokens=total_prompt,
                output_tokens=total_completion,
                thinking_tokens=total_thinking,
            ) if (total_prompt or total_completion) else None
            return BackendResponse(
                text=last_text,
                duration_ms=duration_ms,
                is_success=True,
                stop_reason=result.finish_reason if "result" in dir() else "stop",
                usage=usage,
                tool_call_count=total_tool_calls,
                tool_loop_count=tool_loop_count,
            )

        except asyncio.CancelledError:
            self.logger.warning(f"Request cancelled for {request_id}")
            raise
        except Exception as e:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            return BackendResponse(text="", duration_ms=duration_ms, error=str(e), is_success=False)

    async def shutdown(self):
        if self.client is not None and not getattr(self.client, "is_closed", False):
            await self.client.aclose()
        self.client = None
