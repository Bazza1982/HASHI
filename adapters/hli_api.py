"""HERV3 provider for Hashi Local Inference (HLI) via Hashi Compute.

HLI needs no API key. It reuses the established OpenAI-compatible streaming,
tool calls, and HASHI-owned tool execution. GPU allocation is solely Compute's
concern; this adapter never inspects CUDA or imposes a model-size restriction.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from urllib.parse import urlsplit, urlunsplit

import httpx

from adapters.openai_compatible_api import (
    OpenAICompatibleAdapter,
    normalize_chat_completions_url,
)
from adapters.openrouter_api import OpenRouterAdapter


DEFAULT_HLI_URL = "http://127.0.0.1:19446/v1/chat/completions"

HASHI_COMPACTION_CAPABILITIES = {
    "prompt_isolation": True,
    "tool_disablement": True,
    "semantic_reasoning": True,
    "local_or_slow": True,
}

# Exact context capacities learned from Hashi Compute's existing catalogue.
# Missing discovery never makes initialization fail or creates a permission gate.
HASHI_MODEL_CAPACITY_PROFILES: dict[str, dict] = {}


def _hli_native_estimate(text: str) -> int:
    """Calibrated native-token estimate for llama-server's tokenizer.

    The HERV3 fixed envelope is JSON/token dense: local measurement showed
    ~1.37 native tokens per character for the full prepared prompt. HLI uses
    a 1.5 tokens/character estimate so proactive Compact engages before the
    native per-slot boundary, not after it. This is a local estimate only;
    llama-server's own per-slot context remains the final limit.
    """
    try:
        return max(1, int(len(text) * 1.5))
    except TypeError:
        return 0


def normalized_hli_effort(value: object) -> str | None:
    """Translate HERV3 reasoning labels to llama.cpp's request vocabulary."""
    if value is None:
        return None
    if type(value) is bool:
        return None if value else "none"
    raw = str(value).strip().casefold()
    if raw in {"", "auto", "default", "enabled", "on", "true", "1"}:
        return None
    if raw in {"off", "none", "zero", "disabled", "false", "0"}:
        return "none"
    if raw in {"minimal", "low", "medium", "high", "xhigh", "max"}:
        return raw
    # A model may expose a future, template-specific effort label. Sending an
    # unknown global knob is less portable than keeping the native default.
    return None


class HLIAdapter(OpenAICompatibleAdapter):
    """Keyless, model-agnostic HERV3 adapter; not a separate HASHI Engine."""

    DEFAULT_TOOL_TIERS = ["core"]

    def __init__(self, agent_config, global_config, api_key=None):
        # Bypass the generic provider's required endpoint/API-key assumptions,
        # but inherit its tested SSE/tool handling unchanged.
        OpenRouterAdapter.__init__(self, agent_config, global_config, api_key=None)
        self.logger = logging.getLogger(f"Backend.HLI.{self.config.name}")
        extra = dict(getattr(self.config, "extra", None) or {})
        self._endpoint = normalize_chat_completions_url(
            extra.get("hli_url") or extra.get("chat_completions_url")
            or extra.get("base_url") or DEFAULT_HLI_URL
        )
        # `OpenAICompatibleAdapter.initialize` reads this compatibility setting.
        self.config.extra = {**extra, "api_key_required": False}

    def _request_headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json"}

    def _ensure_client(self) -> None:
        """Direct local/LAN Compute connection without proxies or read deadline.

        llama.cpp can legitimately need several minutes for cold GGUF loading,
        a lengthy prompt or a large reasoning response. `/stop` and caller
        cancellation, not a default generic API timeout, own interruption.
        """
        if self.client is None or getattr(self.client, "is_closed", False):
            self.client = httpx.AsyncClient(
                timeout=httpx.Timeout(None, connect=5), trust_env=False
            )

    def _provider_evidence_url(self) -> str:
        return self._endpoint

    def _build_payload(self, *args, **kwargs) -> dict:
        payload = super()._build_payload(*args, **kwargs)
        extra = dict(getattr(self.config, "extra", None) or {})
        requested = extra.get("provider_reasoning", extra.get("reasoning_effort"))
        if requested is None and getattr(self, "reasoning_enabled", None) is False:
            requested = "off"
        effort = normalized_hli_effort(requested)
        if effort is not None:
            # llama-server accepts reasoning_effort at request level. Its
            # template decides whether it has a meaningful effect.
            payload["reasoning_effort"] = effort
        return payload

    def _estimate_payload_tokens(self, payload: Mapping) -> tuple[int | None, Any | None]:
        """Shared HLI capacity measurement; informational, never a rejection."""
        from orchestrator.context_compaction import resolve_capacity_profile

        runtime = getattr(self.config, "_hashi_runtime", None)
        if runtime is None:
            return None, None
        model = str(payload.get("model") or self.config.model)
        capacity = resolve_capacity_profile(runtime, self.config.engine, model)
        if capacity is None:
            return None, None
        serialized = json.dumps(
            {key: payload.get(key) for key in ("messages", "tools") if key in payload},
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
        if "data:" in serialized or "base64," in serialized:
            return None, capacity
        return _hli_native_estimate(serialized), capacity

    def _preflight_payload_capacity(self, payload: Mapping[str, Any]) -> None:
        """HLI never hard-rejects on HASHI's conservative token estimator.

        The native llama-server per-slot context is the actual boundary. An
        oversized estimate is logged and handled by the async prepare step,
        which asks the HLI Compact route for a targeted reduction; a missing
        or failed Compact still does not create an artificial rejection.
        """
        estimated, capacity = self._estimate_payload_tokens(payload)
        if (estimated is not None and capacity is not None
                and estimated > capacity.usable_input_tokens):
            self.logger.warning(
                "HLI payload estimate %s tokens exceeds usable input %s for "
                "%s/%s; deferring to proactive Compact and native limits",
                estimated, capacity.usable_input_tokens,
                capacity.provider, capacity.model,
            )

    async def _prepare_payload_capacity(
        self,
        payload: Mapping[str, Any],
        *,
        request_id: str,
        provider_calls: list,
    ) -> None:
        """Best-effort targeted Compact when the estimate exceeds capacity.

        Compact availability or failure never rejects the request: llama-server's
        per-slot context remains the real limit. This replaces the generic
        provider's hard preflight rejection for local small models.
        """
        try:
            estimated, capacity = self._estimate_payload_tokens(payload)
            if (estimated is None or capacity is None
                    or estimated <= capacity.usable_input_tokens):
                return
            from orchestrator.context_compaction import coordinator_for

            runtime = getattr(self.config, "_hashi_runtime", None)
            coordinator = (coordinator_for(runtime, request_ref=str(request_id or ""))
                           if runtime is not None else None)
            if coordinator is None:
                return
            await coordinator.compact(
                trigger="hli_preflight",
                request_ref=str(request_id or ""),
                force=False,
                required_reduction_tokens=max(
                    1, estimated - max(1, int(capacity.usable_input_tokens * 0.40))
                ),
                continue_request_on_failure=True,
            )
        except Exception as exc:
            self.logger.debug("HLI preflight compaction unavailable (non-blocking): %s", exc)

    async def initialize(self) -> bool:
        initialized = await super().initialize()
        if initialized:
            await self._observe_model_context()
        return initialized

    async def _observe_model_context(self):
        """Optional best-effort metadata, never a preflight authorization step."""
        if self.config.model in HASHI_MODEL_CAPACITY_PROFILES:
            return
        extras = dict(getattr(self.config, "extra", None) or {})
        local_caps = extras.get("model_capabilities")
        local = (local_caps.get(self.config.model, {})
                 if isinstance(local_caps, Mapping) else {})
        if isinstance(local, Mapping) and local.get("context_window_tokens"):
            return
        parsed = urlsplit(self._endpoint)
        if not parsed.path.endswith("/v1/chat/completions"):
            return
        path = parsed.path.removesuffix("/v1/chat/completions") + "/v1/model-capabilities"
        url = urlunsplit(parsed._replace(path=path))
        try:
            response = await self.client.get(url, params={"model": self.config.model}, timeout=1)
            if response.status_code != 200:
                return
            row = response.json().get("model", {})
            if row.get("id") != self.config.model:
                return
            context = row.get("context")
            if not isinstance(context, Mapping):
                return
            tokens = context.get("served_context_tokens") or context.get("model_max_tokens")
            if type(tokens) is not int or tokens <= 0:
                return
            HASHI_MODEL_CAPACITY_PROFILES[self.config.model] = {
                "context_window_tokens": tokens,
                "response_headroom_tokens": min(4096, max(512, tokens // 4)),
                "capacity_provenance": "hashi_compute_hli_model_capabilities",
            }
        except Exception as exc:
            self.logger.debug("HLI context capacity unavailable (non-blocking): %s", exc)
