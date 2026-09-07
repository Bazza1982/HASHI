"""Configurable OpenAI-compatible API provider for HER v2.

This adapter is deliberately provider-neutral and is available only as a HER
provider, never as a top-level Engine.  It lets a portable instance use an
official regional endpoint such as DashScope/Qwen without baking that vendor
or endpoint into HASHI.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from adapters.openrouter_api import OpenRouterAdapter
from orchestrator.pcm import load_pcm_document

_RESERVED_PAYLOAD_FIELDS = frozenset(
    {"model", "messages", "tools", "stream", "stream_options"}
)
_RESERVED_HEADER_NAMES = frozenset(
    {"authorization", "content-length", "host", "transfer-encoding"}
)


def normalize_chat_completions_url(value: object) -> str:
    """Validate a base URL and resolve its chat-completions endpoint."""

    raw = str(value or "").strip()
    if not raw:
        raise ValueError(
            "openai-compatible-api requires base_url or chat_completions_url"
        )
    parsed = urlsplit(raw)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("openai-compatible-api endpoint must be a plain HTTP(S) URL")
    path = parsed.path.rstrip("/")
    if not path.endswith("/chat/completions"):
        path = f"{path}/chat/completions"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


class OpenAICompatibleAdapter(OpenRouterAdapter):
    """OpenAI chat-completions transport with a configuration-owned endpoint."""

    def __init__(self, agent_config, global_config, api_key: str | None = None):
        super().__init__(agent_config, global_config, api_key)
        self.logger = logging.getLogger(
            f"Backend.OpenAICompatible.{self.config.name}"
        )
        extra = dict(getattr(self.config, "extra", None) or {})
        configured = extra.get("chat_completions_url") or extra.get("base_url")
        self._endpoint = normalize_chat_completions_url(configured)

    def _chat_completions_url(self) -> str:
        return self._endpoint

    def _request_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        configured = dict(
            (getattr(self.config, "extra", None) or {}).get("headers") or {}
        )
        for name, value in configured.items():
            normalized = str(name or "").strip()
            if not normalized or normalized.casefold() in _RESERVED_HEADER_NAMES:
                continue
            headers[normalized] = str(value)
        return headers

    def _build_payload(self, *args, **kwargs) -> dict:
        payload = super()._build_payload(*args, **kwargs)
        # OpenRouter's `reasoning` envelope is aggregator-specific. Compatible
        # services can opt into their own fields through request_options.
        payload.pop("reasoning", None)
        extra = dict(getattr(self.config, "extra", None) or {})
        options = extra.get("request_options")
        if options is not None and not isinstance(options, Mapping):
            raise ValueError("openai-compatible-api request_options must be an object")
        for key, value in dict(options or {}).items():
            normalized = str(key or "").strip()
            if normalized and normalized not in _RESERVED_PAYLOAD_FIELDS:
                payload[normalized] = value
        return payload

    async def initialize(self) -> bool:
        self.config.workspace_dir.mkdir(parents=True, exist_ok=True)
        extra = dict(getattr(self.config, "extra", None) or {})
        if not self.api_key and bool(extra.get("api_key_required", True)):
            self.logger.error(
                "No API key configured for openai-compatible-api"
            )
            return False
        self._ensure_client()
        try:
            if self.config.system_md and Path(self.config.system_md).exists():
                self.sys_prompt = load_pcm_document(
                    self.config.system_md,
                    workspace_dir=self.config.workspace_dir,
                ).system
        except (OSError, ValueError) as exc:
            self.logger.warning("Could not read agent.md: %s", exc)
        self.logger.info(
            "OpenAI-compatible adapter initialized in stateless mode (%s)",
            self._endpoint,
        )
        return True

    async def get_key_info(self) -> dict | None:
        # There is no provider-neutral key-inspection endpoint.
        return None
