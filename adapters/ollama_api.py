"""
Ollama API adapter — local model backend via Ollama's OpenAI-compatible API.

Differences from OpenRouter:
  - Endpoint: http://localhost:11434/v1/chat/completions (configurable)
  - No API key required
  - No OpenRouter-specific headers or reasoning toggles
  - Models are local Ollama models (e.g. gemma4:26b, qwen3:32b)
"""

from __future__ import annotations

import logging
from pathlib import Path

from adapters.base import BackendCapabilities
from adapters.openrouter_api import (
    OpenRouterAdapter,
    _APIResult,
)
from orchestrator.pcm import load_pcm_document

_DEFAULT_OLLAMA_URL = "http://localhost:11434/v1/chat/completions"

HASHI_COMPACTION_CAPABILITIES = {
    "prompt_isolation": True,
    "tool_disablement": True,
    # Ollama model capabilities vary; require an exact model-grant override.
    "semantic_reasoning": False,
    "local_or_slow": True,
}
HASHI_MODEL_CAPACITY_PROFILES = {}


class OllamaAdapter(OpenRouterAdapter):

    def __init__(self, agent_config, global_config, api_key: str = None):
        super().__init__(agent_config, global_config, api_key)
        self.logger = logging.getLogger(f"Backend.Ollama.{self.config.name}")
        # Allow override via agent extra config
        extra = getattr(self.config, "extra", {}) or {}
        self.ollama_url = extra.get("ollama_url", _DEFAULT_OLLAMA_URL)

    def _provider_evidence_url(self) -> str:
        return str(getattr(self, "ollama_url", _DEFAULT_OLLAMA_URL))

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

    async def initialize(self) -> bool:
        self.config.workspace_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_client()

        # Load system prompt
        try:
            if self.config.system_md and Path(self.config.system_md).exists():
                self.sys_prompt = load_pcm_document(
                    self.config.system_md,
                    workspace_dir=self.config.workspace_dir,
                ).system
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

    def _request_headers(self) -> dict[str, str]:
        return self._ollama_headers()

    def _chat_completions_url(self) -> str:
        return str(getattr(self, "ollama_url", _DEFAULT_OLLAMA_URL))

    def _build_payload(
        self,
        messages: list[dict],
        use_streaming: bool = False,
        tool_tiers: list[str] | None = None,
        *,
        excluded_tool_names: frozenset[str] = frozenset(),
        audio_output=None,
        allow_tools: bool = True,
    ) -> dict:
        if audio_output is not None:
            raise ValueError("Ollama does not support native audio output")
        payload: dict = {
            "model": self.config.model,
            "messages": messages,
            "options": {"num_ctx": 32768},
        }
        if use_streaming:
            payload["stream"] = True
        if allow_tools and self.tool_registry:
            tiers = tool_tiers or self.DEFAULT_TOOL_TIERS
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

    def _ollama_headers(self) -> dict:
        return {"Content-Type": "application/json"}

    async def _call_api_once(self, payload, headers, on_stream_event) -> _APIResult:
        return await super()._call_api_once(payload, headers, on_stream_event)

    async def _stream_api_once(self, payload, headers, on_stream_event) -> _APIResult:
        return await super()._stream_api_once(payload, headers, on_stream_event)

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
