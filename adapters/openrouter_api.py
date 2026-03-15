from __future__ import annotations
import asyncio
import logging
import time
from pathlib import Path

import httpx

from adapters.base import BaseBackend, BackendCapabilities, BackendResponse
from adapters.stream_events import KIND_TEXT_DELTA, KIND_THINKING, StreamCallback, StreamEvent


class OpenRouterAdapter(BaseBackend):
    def _define_capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            supports_sessions=False,
            supports_files=False,
            supports_tool_use=False,
            supports_thinking_stream=True,
            supports_headless_mode=True,
        )

    def __init__(self, agent_config, global_config, api_key: str = None):
        super().__init__(agent_config, global_config, api_key)
        self.logger = logging.getLogger(f"Backend.OpenRouter.{self.config.name}")
        self.client = None
        self.sys_prompt = "You are a helpful AI assistant."
        self.reasoning_enabled = False

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

        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": self.sys_prompt},
                {"role": "user", "content": prompt},
            ],
        }
        if self.reasoning_enabled:
            payload["reasoning"] = {
                "enabled": True,
                "exclude": False,
            }
        if use_streaming:
            payload["stream"] = True

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "HTTP-Referer": "https://github.com/Bazza1982/HASHI",
            "X-Title": "Bridge-U Orchestrator",
        }

        try:
            self._touch_activity()

            if use_streaming:
                return await self._stream_response(
                    payload, headers, started, request_id, on_stream_event
                )

            response = await self.client.post(
                self.global_config.openrouter_url,
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()
            choices = data.get("choices") or []
            if not choices:
                duration_ms = round((time.perf_counter() - started) * 1000, 2)
                return BackendResponse(
                    text="",
                    duration_ms=duration_ms,
                    error="OpenRouter returned empty choices",
                    is_success=False,
                )

            message = choices[0].get("message") or {}
            ai_text = message.get("content") or ""
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

            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            return BackendResponse(text=ai_text, duration_ms=duration_ms, is_success=True)
        except asyncio.CancelledError:
            self.logger.warning(f"Request cancelled for {request_id}")
            raise
        except Exception as e:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            return BackendResponse(text="", duration_ms=duration_ms, error=str(e), is_success=False)

    async def _stream_response(
        self,
        payload: dict,
        headers: dict,
        started: float,
        request_id: str,
        on_stream_event: StreamCallback,
    ) -> BackendResponse:
        import json

        chunks: list[str] = []

        try:
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

                    choices = data.get("choices", [])
                    if not choices:
                        continue

                    delta = choices[0].get("delta", {})
                    content = delta.get("content", "")
                    reasoning_text = str(delta.get("reasoning") or "").strip()
                    reasoning_details = delta.get("reasoning_details") or []

                    if reasoning_text:
                        asyncio.create_task(
                            on_stream_event(
                                StreamEvent(kind=KIND_THINKING, summary=reasoning_text[:400])
                            )
                        )
                    elif reasoning_details:
                        for detail in reasoning_details:
                            snippet = self._summarize_reasoning_detail(detail)
                            if snippet:
                                asyncio.create_task(
                                    on_stream_event(
                                        StreamEvent(kind=KIND_THINKING, summary=snippet[:400])
                                    )
                                )

                    if content:
                        chunks.append(content)
                        asyncio.create_task(
                            on_stream_event(
                                StreamEvent(kind=KIND_TEXT_DELTA, summary=content[:120])
                            )
                        )
        except Exception as e:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            return BackendResponse(text="", duration_ms=duration_ms, error=str(e), is_success=False)

        full_text = "".join(chunks)
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        return BackendResponse(text=full_text, duration_ms=duration_ms, is_success=True)

    async def shutdown(self):
        if self.client is not None and not getattr(self.client, "is_closed", False):
            await self.client.aclose()
        self.client = None
