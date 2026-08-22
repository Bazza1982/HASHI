from __future__ import annotations
import asyncio
import json
import logging
import time
from dataclasses import dataclass
from itertools import count
from pathlib import Path
from typing import Any, Mapping, Optional

import httpx

from adapters.base import BaseBackend, BackendCapabilities, BackendResponse
from adapters.stream_events import (
    KIND_FILE_EDIT,
    KIND_FILE_READ,
    KIND_SHELL_EXEC,
    KIND_TEXT_DELTA,
    KIND_THINKING,
    KIND_TOOL_END,
    KIND_TOOL_START,
    StreamCallback,
    StreamEvent,
)
from orchestrator.enterprise.policy import evaluate_governance_policy


HASHI_COMPACTION_CAPABILITIES = {
    "prompt_isolation": True,
    "tool_disablement": True,
    # OpenRouter aggregates heterogeneous models; an exact Agent grant must
    # opt the selected model into semantic compaction.
    "semantic_reasoning": False,
    "local_or_slow": False,
}
HASHI_MODEL_CAPACITY_PROFILES: dict[str, dict[str, Any]] = {}

_STABLE_CONTEXT_CAPACITY_CODES = frozenset(
    {
        "context_length_exceeded",
        "context_window_exceeded",
        "maximum_context_length_exceeded",
        "prompt_too_long",
    }
)


def _stable_provider_error_code(response: httpx.Response | None) -> str:
    """Return only provider-owned stable codes; never infer capacity from HTTP 400."""

    if response is None:
        return ""
    try:
        payload = response.json()
    except Exception:
        return ""
    candidates: list[Any] = []
    if isinstance(payload, Mapping):
        error = payload.get("error")
        if isinstance(error, Mapping):
            candidates.extend((error.get("code"), error.get("type")))
            metadata = error.get("metadata")
            if isinstance(metadata, Mapping):
                candidates.extend((metadata.get("code"), metadata.get("type")))
        candidates.extend((payload.get("code"), payload.get("type")))
    for candidate in candidates:
        normalized = str(candidate or "").strip().lower()
        if normalized in _STABLE_CONTEXT_CAPACITY_CODES:
            return "CONTEXT_CAPACITY_REJECTED"
    return ""


@dataclass
class _APIResult:
    """Internal intermediate result from a single API call."""
    text: str
    tool_calls: Optional[list]   # None = no tool calls, just text
    finish_reason: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    thinking_tokens: int = 0
    reasoning_content: str = ""
    structured_data: dict[str, Any] | None = None


def _retry_after_seconds(response: httpx.Response | None) -> float | None:
    if response is None:
        return None
    raw = str(response.headers.get("retry-after") or "").strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def _provider_request_id(response: httpx.Response | None) -> str:
    if response is None:
        return ""
    for name in ("x-request-id", "request-id", "cf-ray", "x-amzn-requestid"):
        value = str(response.headers.get(name) or "").strip()
        if value:
            return value
    return ""


def _backend_failure_response(
    error: Exception,
    *,
    duration_ms: float,
    tool_call_count: int = 0,
    tool_loop_count: int = 0,
) -> BackendResponse:
    """Convert OpenAI-compatible transport errors into a typed response."""

    response = getattr(error, "response", None)
    status = (
        int(response.status_code)
        if isinstance(response, httpx.Response)
        else None
    )
    retryable = False
    code = "PROVIDER_UNKNOWN"
    description = "The provider request failed for an unknown technical reason."

    if status is not None:
        stable_code = _stable_provider_error_code(response)
        if stable_code:
            code = stable_code
            description = "The provider rejected the serialized request because it exceeds the model context capacity."
        elif status == 400:
            code = "PROVIDER_BAD_REQUEST"
            description = "The provider rejected the request as invalid."
        elif status == 401:
            code = "PROVIDER_AUTHENTICATION_FAILED"
            description = "The provider rejected the configured credentials."
        elif status == 403:
            code = "PROVIDER_PERMISSION_DENIED"
            description = "The provider denied access to this model or request."
        elif status == 408:
            code = "PROVIDER_REQUEST_TIMEOUT"
            description = "The provider timed out while handling the request."
            retryable = True
        elif status == 429:
            code = "PROVIDER_RATE_LIMITED"
            description = "The provider rate-limited the request."
            retryable = True
        elif 500 <= status <= 599:
            code = "PROVIDER_SERVER_ERROR"
            description = "The provider reported a temporary server failure."
            retryable = True
        elif 400 <= status <= 499:
            code = "PROVIDER_BAD_REQUEST"
            description = f"The provider rejected the request with HTTP {status}."
    elif isinstance(error, httpx.TimeoutException):
        code = "PROVIDER_REQUEST_TIMEOUT"
        description = "The provider connection or response timed out."
        retryable = True
    elif isinstance(error, httpx.RemoteProtocolError):
        code = "PROVIDER_INCOMPLETE_STREAM"
        description = "The provider response stream ended before completion."
        retryable = True
    elif isinstance(error, (httpx.InvalidURL, httpx.UnsupportedProtocol)):
        code = "PROVIDER_CONFIGURATION_ERROR"
        description = "The configured provider URL or protocol is invalid."
    elif isinstance(error, httpx.ConnectError):
        lowered = str(error).casefold()
        if any(token in lowered for token in ("certificate", "ssl", "tls")):
            code = "PROVIDER_TLS_ERROR"
            description = "The provider TLS certificate or trust configuration failed."
        else:
            code = "PROVIDER_CONNECTION_FAILED"
            description = "A connection to the provider could not be established."
            retryable = True
    elif isinstance(error, httpx.NetworkError):
        code = "PROVIDER_CONNECTION_FAILED"
        description = "The provider connection was interrupted."
        retryable = True
    elif isinstance(error, (json.JSONDecodeError, UnicodeDecodeError)):
        code = "PROVIDER_INCOMPLETE_STREAM"
        description = "The provider returned an incomplete or invalid response body."
        retryable = True

    side_effects_possible = bool(tool_call_count)
    if side_effects_possible:
        # The runtime performs the final replay-safety decision using actual
        # tool activity.  Preserve uncertainty rather than claiming safety.
        retryable = bool(retryable)
    return BackendResponse(
        text="",
        duration_ms=duration_ms,
        error=str(error),
        is_success=False,
        tool_call_count=int(tool_call_count),
        tool_loop_count=int(tool_loop_count),
        error_code=code,
        error_retryable=retryable,
        http_status=status,
        provider_request_id=_provider_request_id(
            response if isinstance(response, httpx.Response) else None
        )
        or None,
        retry_after_s=_retry_after_seconds(
            response if isinstance(response, httpx.Response) else None
        ),
        side_effects_possible=side_effects_possible,
        stream_metadata={"provider_failure_description": description},
    )


def _assistant_content_text(content: Any) -> str:
    """Normalize common OpenAI-compatible content shapes into assistant text."""

    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, Mapping):
        for key in ("text", "output_text", "content"):
            if key in content:
                return _assistant_content_text(content.get(key))
        return json.dumps(dict(content), ensure_ascii=False)
    if isinstance(content, list):
        return "".join(_assistant_content_text(item) for item in content)
    return str(content)


def _message_structured_data(message: Mapping[str, Any]) -> dict[str, Any] | None:
    """Preserve an API-native parsed object without granting it authority."""

    for key in ("parsed", "structured_output"):
        value = message.get(key)
        if isinstance(value, Mapping):
            return dict(value)
    content = message.get("content")
    if isinstance(content, Mapping):
        return dict(content)
    return None


def _tool_target_path(arguments: dict) -> str | None:
    for key in ("path", "file_path", "target_path"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _file_resource(arguments: dict) -> str:
    path = _tool_target_path(arguments)
    return f"file:{path}" if path else "file:*"


class OpenRouterAdapter(BaseBackend):
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

    def _reasoning_detail_delta(self, detail) -> str:
        if not isinstance(detail, dict):
            return ""
        detail_type = str(detail.get("type") or "").strip()
        if detail_type == "reasoning.encrypted":
            return ""
        if detail_type == "reasoning.summary":
            return str(detail.get("summary") or "")
        return str(detail.get("text") or detail.get("summary") or "")

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
                    tool_name: str = "", file_path: str = "",
                    metadata: Mapping[str, Any] | None = None) -> None:
        if on_stream_event is None:
            return
        try:
            await on_stream_event(
                StreamEvent(
                    kind=kind,
                    summary=summary,
                    tool_name=tool_name,
                    file_path=file_path,
                    metadata=dict(metadata or {}),
                )
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

            policy = self._evaluate_tool_policy(tool_name, arguments)
            if not policy.allowed:
                result_text = self._blocked_tool_result_text(tool_name, policy)
                await self._emit(on_stream_event, KIND_TOOL_END,
                                 f"{tool_name}: blocked by policy", tool_name=tool_name)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": result_text,
                })
                continue

            # Execute
            try:
                result = await self.tool_registry.execute(
                    tool_name, arguments, tool_call_id=tc_id
                )
            except asyncio.CancelledError as exc:
                details = dict(getattr(exc, "hashi_tool_details", {}) or {})
                await self._emit(
                    on_stream_event,
                    KIND_TOOL_END,
                    f"{tool_name}: cancelled after cleanup",
                    tool_name=tool_name,
                    metadata={"tool_result_details": details},
                )
                raise

            output_preview = result.output[:100].replace("\n", " ")
            await self._emit(on_stream_event, KIND_TOOL_END,
                             f"{tool_name}: {output_preview}", tool_name=tool_name,
                             metadata={
                                 "tool_result_details": dict(result.details or {})
                             })

            messages.append({
                "role": "tool",
                "tool_call_id": tc_id,
                "content": result.output,
            })

    def _evaluate_tool_policy(self, tool_name: str, arguments: dict):
        action, resource = self._tool_policy_action_resource(tool_name, arguments)
        return evaluate_governance_policy(
            action,
            {
                "global_config": self.global_config,
                "agent_id": getattr(self.config, "name", None),
                "backend": getattr(self.config, "engine", None),
                "tool_name": tool_name,
                "tool_arguments": arguments,
                "resource": resource,
                "target_path": _tool_target_path(arguments),
            },
        )

    def _tool_policy_action_resource(self, tool_name: str, arguments: dict) -> tuple[str, str]:
        normalized = (tool_name or "").strip().lower()
        if normalized == "bash":
            return "shell.execute", "shell:bash"
        if normalized == "file_write":
            return "file.write", _file_resource(arguments)
        if normalized == "file_read":
            return "file.read", _file_resource(arguments)
        return "tool.execute", f"tool:{normalized or 'unknown'}"

    def _blocked_tool_result_text(self, tool_name: str, policy) -> str:
        if policy.decision.value == "approval_required":
            return f"Error: tool call requires approval by enterprise policy: {tool_name}"
        return f"Error: tool call blocked by enterprise policy: {tool_name}"

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
        ai_text = _assistant_content_text(message.get("content"))

        # Emit reasoning if present
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
            for detail in message.get("reasoning_details") or []:
                snippet = self._summarize_reasoning_detail(detail)
                if snippet:
                    await on_stream_event(
                        StreamEvent(
                            kind=KIND_THINKING,
                            summary=snippet[:400],
                            raw_delta=self._reasoning_detail_delta(detail),
                        )
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
            structured_data=_message_structured_data(message),
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
        finish_reason = ""
        stream_usage: dict = {}  # usage from final streaming chunk
        saw_done = False

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
                    saw_done = True
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
                reasoning_text = str(delta.get("reasoning") or "")
                reasoning_details = delta.get("reasoning_details") or []

                if reasoning_text and on_stream_event:
                    await on_stream_event(
                        StreamEvent(
                            kind=KIND_THINKING,
                            summary=reasoning_text[:400],
                            raw_delta=reasoning_text,
                        )
                    )
                elif reasoning_details and on_stream_event:
                    for detail in reasoning_details:
                        raw_delta = self._reasoning_detail_delta(detail)
                        if raw_delta:
                            await on_stream_event(
                                StreamEvent(
                                    kind=KIND_THINKING,
                                    summary=raw_delta[:400],
                                    raw_delta=raw_delta,
                                )
                            )
                            continue
                        snippet = self._summarize_reasoning_detail(detail)
                        if snippet:
                            await on_stream_event(
                                StreamEvent(
                                    kind=KIND_THINKING,
                                    summary=snippet[:400],
                                )
                            )

                if content:
                    text_chunks.append(content)
                    if on_stream_event:
                        await on_stream_event(
                            StreamEvent(kind=KIND_TEXT_DELTA, summary=content)
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
        last_structured_data = None
        # Accumulate token usage across all tool loops
        total_prompt = 0
        total_completion = 0
        total_thinking = 0
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

                # Accumulate usage from each API call
                total_prompt += result.prompt_tokens
                total_completion += result.completion_tokens
                total_thinking += result.thinking_tokens

                last_text = result.text
                last_structured_data = result.structured_data

                # No tool calls — we're done
                if not result.tool_calls or not self.tool_registry:
                    break

                tool_loop_count += 1
                total_tool_calls += len(result.tool_calls)
                self.logger.debug(
                    f"Tool loop {loop_idx + 1}: {len(result.tool_calls)} tool call(s)"
                )

                # Append assistant message (with tool_calls) to conversation
                assistant_msg: dict = {"role": "assistant"}
                if result.text:
                    assistant_msg["content"] = result.text
                assistant_msg["tool_calls"] = result.tool_calls
                messages.append(assistant_msg)

                # Execute tools, append results
                await self._run_tool_calls(result.tool_calls, messages, on_stream_event)

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
                structured_data=last_structured_data,
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
            return _backend_failure_response(
                e,
                duration_ms=duration_ms,
                tool_call_count=total_tool_calls,
                tool_loop_count=tool_loop_count,
            )

    async def shutdown(self):
        if self.client is not None and not getattr(self.client, "is_closed", False):
            await self.client.aclose()
        self.client = None
