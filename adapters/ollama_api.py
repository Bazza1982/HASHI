"""Ollama model adapter with native boolean thinking controls.

Explicit reasoning uses /api/chat and a top-level boolean think. Native
capacity admission remains a separate instance opt-in. Unconfigured legacy
callers retain the OpenAI-compatible endpoint and model default.
"""

from __future__ import annotations

import json
import logging
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from adapters.base import BackendCapabilities
from adapters.openrouter_api import (
    OpenRouterAdapter,
    ProviderContextCapacityPreflightError,
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
        self.effort = extra.get("effort")

    def _ollama_think(self) -> bool | None:
        extra = getattr(self.config, "extra", None) or {}
        configured = extra.get("provider_reasoning")
        if configured is None:
            configured = extra.get("reasoning_effort")
        if configured is None:
            configured = getattr(self, "effort", None)
        if type(configured) is bool:
            return configured
        normalized = str(configured or "").strip().casefold()
        if normalized in {"", "default", "auto"}:
            return getattr(self, "reasoning_enabled", None)
        if normalized in {"off", "none", "zero", "false", "0", "disabled"}:
            return False
        if normalized in {"enabled", "on", "true", "1"}:
            return True
        raise ValueError(
            "Ollama boolean thinking supports only enabled/off; "
            "low/medium/high are not supported by this control."
        )

    def _native_chat_enabled(self) -> bool:
        return (
            self._native_context_enabled()
            or self._ollama_think() is not None
            or urlsplit(str(getattr(self, "ollama_url", _DEFAULT_OLLAMA_URL))).path.endswith("/api/chat")
        )

    def _provider_evidence_url(self) -> str:
        return self._chat_completions_url()

    def _native_context_enabled(self) -> bool:
        return (getattr(self.config, "extra", None) or {}).get("ollama_native_context") is True

    def project_main_references(self, refs):
        from adapters.ollama_context import project_references

        return project_references(self, refs)

    async def _prepare_payload_capacity(self, payload, *, request_id, provider_calls):
        if self._native_context_enabled():
            from adapters.ollama_capacity import check_native_capacity

            await check_native_capacity(
                self, payload, request_id=request_id, provider_calls=provider_calls
            )

    async def _run_tool_calls(self, tool_calls, messages, on_stream_event, **kwargs):
        if self._native_context_enabled() and getattr(self, "_ollama_context_closing", False):
            raise ProviderContextCapacityPreflightError(
                "Tool use was closed to preserve context capacity; the requested "
                "tools were not executed. Completed results remain preserved."
            )
        first_result = len(messages)
        facts = await super()._run_tool_calls(
            tool_calls, messages, on_stream_event, **kwargs
        )
        if self._native_context_enabled():
            from adapters.ollama_capacity import page_new_tool_results

            page_new_tool_results(self, messages, first_result)
        return facts

    def _native_capacity(self):
        from orchestrator.context_compaction import resolve_capacity_profile

        capacity = resolve_capacity_profile(
            getattr(self.config, "_hashi_runtime", None),
            self.config.engine,
            self.config.model,
            profile_options=getattr(self.config, "extra", None),
        )
        if capacity is None:
            raise ProviderContextCapacityPreflightError(
                "The Ollama context pilot requires an exact configured model capacity."
            )
        return capacity

    def _preflight_payload_capacity(self, payload) -> None:
        if not self._native_context_enabled():
            return super()._preflight_payload_capacity(payload)
        # Character estimates can both undercount source/JSON and overcount
        # compressible text. Admission uses the exact final payload in the
        # asynchronous native check, after outbound privacy filtering.
        self._native_capacity()

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
            tags_url = "http://localhost:11434/api/tags"
            if self._native_chat_enabled():
                parts = urlsplit(self._chat_completions_url())
                tags_url = urlunsplit(parts._replace(path=parts.path[:-4] + "tags"))
            resp = await self.client.get(tags_url)
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
        endpoint = str(getattr(self, "ollama_url", _DEFAULT_OLLAMA_URL))
        if not self._native_chat_enabled():
            return endpoint
        parts = urlsplit(endpoint)
        if parts.path.endswith("/v1/chat/completions"):
            parts = parts._replace(path=parts.path[:-20] + "/api/chat")
        elif not parts.path.endswith("/api/chat"):
            raise ValueError("Ollama native controls require a chat endpoint URL")
        return urlunsplit(parts)

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
        if self._native_context_enabled() and getattr(self, "_ollama_context_closing", False):
            from adapters.ollama_capacity import closing_messages

            messages = closing_messages(messages)
            allow_tools = False
        payload: dict = {
            "model": self.config.model,
            "messages": messages,
            "options": {"num_ctx": 32768},
        }
        think = self._ollama_think()
        if think is not None:
            payload["think"] = think
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
        if self._native_chat_enabled():
            native_messages = deepcopy(messages)
            tool_names = {}
            for message in native_messages:
                if not isinstance(message.get("content", ""), (str, type(None))):
                    raise ValueError("The Ollama context pilot currently accepts text-only messages")
                for call in message.get("tool_calls") or []:
                    function = call["function"]
                    tool_names[call.get("id")] = function["name"]
                    arguments = function.get("arguments")
                    if isinstance(arguments, str):
                        arguments = json.loads(arguments)
                    if not isinstance(arguments, dict):
                        raise ValueError("Ollama tool arguments must be a JSON object")
                    function["arguments"] = arguments
                if message.get("role") == "tool":
                    name = tool_names.get(message.get("tool_call_id"))
                    if name:
                        message["tool_name"] = name
            payload["messages"] = native_messages
            if self._native_context_enabled():
                capacity = self._native_capacity()
                payload["options"] = {
                    "num_ctx": capacity.context_window_tokens,
                    "num_predict": capacity.response_headroom_tokens,
                }
            else:
                payload["options"]["num_predict"] = 4096
            model_options = (getattr(self.config, "extra", None) or {}).get(
                "model_capabilities", {}
            ).get(self.config.model, {})
            for parameter, minimum in (("num_batch", 1), ("num_gpu", 0)):
                value = model_options.get("ollama_" + parameter)
                if value is not None:
                    if type(value) is not int or value < minimum:
                        raise ValueError(f"Ollama {parameter} must be an integer >= {minimum}")
                    payload["options"][parameter] = value
            payload["truncate"] = False
            payload["stream"] = bool(use_streaming)
            keep_alive = (getattr(self.config, "extra", None) or {}).get(
                "ollama_keep_alive_seconds"
            )
            if keep_alive is not None:
                if type(keep_alive) is not int or keep_alive < 0:
                    raise ValueError("Ollama idle residency must be nonnegative integer seconds")
                payload["keep_alive"] = keep_alive
        return payload

    def _ollama_headers(self) -> dict:
        return {"Content-Type": "application/json"}

    async def _call_api_once(self, payload, headers, on_stream_event) -> _APIResult:
        if self._native_chat_enabled():
            from adapters.ollama_native import call_native_chat

            result = await call_native_chat(self, payload, headers, on_stream_event)
            self._ollama_last_output_tokens = result.completion_tokens
            return result
        return await super()._call_api_once(payload, headers, on_stream_event)

    async def _stream_api_once(self, payload, headers, on_stream_event) -> _APIResult:
        if self._native_chat_enabled():
            return await self._call_api_once(payload, headers, on_stream_event)
        return await super()._stream_api_once(payload, headers, on_stream_event)

    async def generate_response(
        self,
        prompt,
        request_id,
        is_retry=False,
        silent=False,
        on_stream_event=None,
        request_content=None,
        continuation_messages=None,
        resume_pending_tools=False,
        provider_call_limit=None,
        before_provider_call=None,
        transient_images=None,
    ):
        self._ollama_prompt_tokens = None
        self._ollama_last_output_tokens = 0
        self._ollama_capacity_probe_serial = 0
        self._ollama_context_closing = False
        return await super().generate_response(
            prompt,
            request_id,
            is_retry=is_retry,
            silent=silent,
            on_stream_event=on_stream_event,
            request_content=request_content,
            continuation_messages=continuation_messages,
            **({"resume_pending_tools": True} if resume_pending_tools else {}),
            provider_call_limit=provider_call_limit,
            before_provider_call=before_provider_call,
            transient_images=transient_images,
        )
