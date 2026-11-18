"""Ollama model adapter with native boolean thinking controls.

Explicit reasoning uses /api/chat and a top-level boolean think. Native
capacity admission remains a separate instance opt-in. Unconfigured legacy
callers retain the OpenAI-compatible endpoint and model default.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
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


def _ollama_endpoint_key(value):
    parts = urlsplit(str(value))
    path = parts.path.rstrip("/")
    for suffix in ("/v1/chat/completions", "/api/chat"):
        if path.endswith(suffix):
            path = path[:-len(suffix)]
            break
    return parts.scheme.lower(), parts.netloc.lower(), path, parts.query


def _discovered_tool_names(messages: list[dict]) -> list[str]:
    """Expand only successful catalogue pages already present in this turn."""
    catalogue_calls = set()
    names = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "assistant":
            for call in message.get("tool_calls") or []:
                if isinstance(call, dict) and (call.get("function") or {}).get("name") == "tool_catalog":
                    catalogue_calls.add(str(call.get("id") or ""))
        if message.get("role") != "tool" or str(message.get("tool_call_id") or "") not in catalogue_calls:
            continue
        try:
            payload = json.loads(message.get("content") or "")
            if isinstance(payload, dict) and "data" in payload:
                payload = payload["data"]
                if isinstance(payload, str):
                    payload = json.loads(payload)
            if isinstance(payload, dict) and isinstance(payload.get("tools"), list):
                for item in payload["tools"]:
                    if isinstance(item, dict) and isinstance(item.get("name"), str):
                        names.append(item["name"])
        except (TypeError, ValueError):
            continue
    return list(dict.fromkeys(names))


class OllamaAdapter(OpenRouterAdapter):

    def __init__(self, agent_config, global_config, api_key: str = None):
        super().__init__(agent_config, global_config, api_key)
        self.logger = logging.getLogger(f"Backend.Ollama.{self.config.name}")
        # Allow override via agent extra config
        extra = getattr(self.config, "extra", {}) or {}
        self.ollama_url = extra.get("ollama_url", _DEFAULT_OLLAMA_URL)
        self.effort = extra.get("effort")
        self._ollama_initial_endpoint = _ollama_endpoint_key(self.ollama_url)
        self._ollama_initial_model = str(self.config.model)
        self._ollama_discovered_capacity = None

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
        extra = getattr(self.config, "extra", None) or {}
        options = self._model_context_options()
        enabled = options.get("ollama_native_context", extra.get("ollama_native_context", False))
        if "ollama_native_context" in options and type(enabled) is not bool:
            raise ValueError("Ollama model context opt-in must be boolean")
        if enabled is not True:
            return False
        return True

    def _model_context_options(self) -> Mapping:
        extra = getattr(self.config, "extra", None) or {}
        rows = extra.get("model_capabilities", {})
        row = rows.get(self.config.model, {}) if isinstance(rows, Mapping) else {}
        return row if isinstance(row, Mapping) else {}

    def _options_match_endpoint(self, options: Mapping) -> bool:
        qualified = options.get("ollama_url")
        current = _ollama_endpoint_key(self.ollama_url)
        if qualified is not None:
            return _ollama_endpoint_key(qualified) == current
        return current == self._ollama_initial_endpoint

    def _model_options_match_endpoint(self) -> bool:
        return self._options_match_endpoint(self._model_context_options())

    def _qualified_model_context_options(self) -> Mapping:
        return self._model_context_options() if self._model_options_match_endpoint() else {}

    def _configured_native_capacity(self):
        from orchestrator.context_compaction import resolve_capacity_profile

        extra = getattr(self.config, "extra", None) or {}
        row = self._model_context_options()
        capacities = extra.get("context_capacities")
        exact = capacities.get(self.config.model) if isinstance(capacities, Mapping) else None
        candidates = [row, exact]
        if str(self.config.model) == self._ollama_initial_model:
            candidates.append(extra)
        selected = next((
            candidate for candidate in candidates
            if isinstance(candidate, Mapping)
            and any(k in candidate for k in ("context_window_tokens", "context_capacity_tokens"))
            and self._options_match_endpoint(candidate)
        ), None)
        if selected is None:
            return None
        # Resolve the selected row itself. Passing all of extra would let an
        # unrelated model_capabilities row shadow the context_capacities row
        # or an unqualified runtime profile supply another endpoint's facts.
        capacity = resolve_capacity_profile(
            None, self.config.engine, self.config.model, profile_options=selected,
        )
        if capacity is None:
            raise ProviderContextCapacityPreflightError(
                "The selected Ollama model capacity or output reserve is invalid."
            )
        return capacity

    @staticmethod
    def _canonical_ollama_model(value):
        name = str(value or "").strip()
        return name if not name or ":" in name.rsplit("/", 1)[-1] else name + ":latest"

    def _ollama_service_url(self, suffix):
        parts = urlsplit(str(self.ollama_url))
        base = _ollama_endpoint_key(self.ollama_url)[2]
        return urlunsplit(parts._replace(path=base + "/api/" + suffix, fragment=""))

    async def _refresh_discovered_capacity(self):
        # An explicit exact-model capacity is already the selected local fact.
        try:
            if self._configured_native_capacity() is not None:
                return
        except ProviderContextCapacityPreflightError:
            return
        self._ensure_client()
        endpoint = _ollama_endpoint_key(self.ollama_url)
        model = str(self.config.model)
        expected_name = self._canonical_ollama_model(model)

        async def digest_for_model():
            response = await self.client.get(self._ollama_service_url("tags"),
                                             headers=self._ollama_headers(), follow_redirects=False)
            response.raise_for_status()
            data = response.json()
            rows = data.get("models") if isinstance(data, Mapping) else None
            matches = [row for row in rows or [] if isinstance(row, Mapping)
                       and self._canonical_ollama_model(row.get("name") or row.get("model"))
                       == expected_name]
            if len(matches) != 1:
                raise ValueError("selected Ollama model is not uniquely identified")
            digest = str(matches[0].get("digest") or "")
            if not re.fullmatch(r"(?:sha256:)?[0-9a-fA-F]{64}", digest):
                raise ValueError("selected Ollama model has no qualified revision")
            return digest.casefold()

        try:
            digest = await digest_for_model()
            cached = self._ollama_discovered_capacity
            if (cached and cached["endpoint"] == endpoint and cached["model"] == model
                    and cached["digest"] == digest):
                return
            response = await self.client.post(self._ollama_service_url("show"),
                                              headers=self._ollama_headers(),
                                              json={"model": model}, follow_redirects=False)
            response.raise_for_status()
            data = response.json()
            info = data.get("model_info") if isinstance(data, Mapping) else None
            values = {value for key, value in (info.items() if isinstance(info, Mapping) else [])
                      if str(key).endswith(".context_length") and type(value) is int and value > 1}
            if len(values) != 1 or await digest_for_model() != digest:
                raise ValueError("model capacity is absent, ambiguous or changed during discovery")
            from orchestrator.context_compaction import CapacityProfile, DEFAULT_RESPONSE_HEADROOM_TOKENS

            tokens = values.pop()
            options = self._qualified_model_context_options()
            requested_context = options.get("ollama_num_ctx")
            if requested_context is not None:
                if type(requested_context) is not int or requested_context < 2:
                    raise ValueError("selected Ollama context is invalid")
                if requested_context > tokens:
                    raise ValueError("selected context exceeds advertised model capacity")
                tokens = requested_context
            requested_output = options.get("ollama_num_predict", options.get("response_headroom_tokens"))
            if requested_output is None:
                output = min(DEFAULT_RESPONSE_HEADROOM_TOKENS, max(1, tokens // 4))
            elif type(requested_output) is int and 0 < requested_output < tokens:
                output = requested_output
            else:
                raise ValueError("selected Ollama output reserve is invalid")
            capacity = CapacityProfile(
                provider="ollama-api", model=model, context_window_tokens=tokens,
                provenance="ollama_model_info:" + digest,
                response_headroom_tokens=output,
            )
            self._ollama_discovered_capacity = {
                "endpoint": endpoint, "model": model, "digest": digest, "capacity": capacity,
            }
        except Exception as exc:
            self._ollama_discovered_capacity = None
            self.logger.warning("Ollama model capacity is unverified: %s", type(exc).__name__)

    def project_pcm_payload(self, payload, *, render_prompt):
        from adapters.ollama_context import project_pcm_payload

        return project_pcm_payload(self, payload, render_prompt=render_prompt)

    def project_main_references(self, refs):
        from adapters.ollama_context import project_references

        return project_references(self, refs)

    async def _prepare_payload_capacity(self, payload, *, request_id, provider_calls):
        if self._native_context_enabled() and self._native_capacity() is not None:
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
        capacity = self._configured_native_capacity()
        if capacity is not None:
            return capacity
        discovered = self._ollama_discovered_capacity
        if (discovered and discovered["endpoint"] == _ollama_endpoint_key(self.ollama_url)
                and discovered["model"] == str(self.config.model)):
            return discovered["capacity"]
        return None

    def _preflight_payload_capacity(self, payload) -> None:
        if not self._native_context_enabled():
            return super()._preflight_payload_capacity(payload)
        # Character estimates can both undercount source/JSON and overcount
        # compressible text. Admission uses the exact final payload in the
        # asynchronous native check, after outbound privacy filtering.
        limit = self._qualified_model_context_options().get("ollama_max_input_chars")
        if limit is not None:
            if type(limit) is not int or limit < 1:
                raise ValueError("Ollama input character limit must be a positive integer")
            from adapters.ollama_capacity import encoded_input_chars

            size = encoded_input_chars(payload)
            if size > limit:
                raise ProviderContextCapacityPreflightError(
                    f"Ollama input has {size} encoded characters; "
                    f"the configured qualified limit is {limit}."
                )

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
        capacity_preview: bool = False,
    ) -> dict:
        if audio_output is not None:
            raise ValueError("Ollama does not support native audio output")
        if (not capacity_preview and self._native_context_enabled()
                and getattr(self, "_ollama_context_closing", False)):
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
            from tools.office.discovery import message_tiers
            tiers = message_tiers(tool_tiers or self.DEFAULT_TOOL_TIERS, messages)
            tiers = [*tiers, *_discovered_tool_names(messages)]
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
            model_options = self._qualified_model_context_options()
            qualified_endpoint = model_options.get("ollama_url")
            matches_endpoint = (qualified_endpoint is None or
                                _ollama_endpoint_key(qualified_endpoint) == _ollama_endpoint_key(self.ollama_url))
            capacity = self._native_capacity() if self._native_context_enabled() else None
            if capacity is not None:
                payload["options"] = {
                    "num_ctx": capacity.context_window_tokens,
                    "num_predict": capacity.response_headroom_tokens,
                }
            else:
                # Do not force an arbitrary context or response length on a
                # native server. Exact-model settings apply only to their
                # configured endpoint; otherwise let the server use defaults.
                payload["options"] = {}
                if matches_endpoint:
                    context = model_options.get("ollama_num_ctx", model_options.get("context_window_tokens"))
                    prediction = model_options.get("ollama_num_predict", model_options.get("response_headroom_tokens"))
                    for parameter, value in (("num_ctx", context), ("num_predict", prediction)):
                        if value is not None:
                            if type(value) is not int or value < 1:
                                raise ValueError(f"Ollama {parameter} must be a positive integer")
                            payload["options"][parameter] = value
                    if context is not None and prediction is not None and prediction >= context:
                        raise ValueError("Ollama output budget must be smaller than context")
                else:
                    self.logger.warning("Ollama model settings belong to a different endpoint; using server defaults")
                    model_options = {}
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
        native_capacity_selected = self._native_context_enabled()
        if native_capacity_selected:
            await self._refresh_discovered_capacity()
        response = await super().generate_response(
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
        if native_capacity_selected:
            try:
                capacity = self._native_capacity()
            except ProviderContextCapacityPreflightError:
                capacity = None
            metadata = dict(response.stream_metadata or {})
            metadata["ollama_capacity"] = {
                # Metadata discovery verifies the selected endpoint/model
                # revision, not available RAM or the loaded context size.
                # A user-declared capacity is a configuration, not evidence
                # that the live server was probed.
                "verified": bool(capacity and self._ollama_discovered_capacity
                                 and self._ollama_discovered_capacity["capacity"] is capacity),
                "provenance": capacity.provenance if capacity is not None else None,
            }
            response.stream_metadata = metadata
        return response
