"""HERV3 provider for Hashi Local Inference (HLI) via Hashi Compute.

HLI needs no API key. It reuses the established OpenAI-compatible streaming,
tool calls, and HASHI-owned tool execution. GPU allocation is solely Compute's
concern; this adapter never inspects CUDA or imposes a model-size restriction.
"""
from __future__ import annotations

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
