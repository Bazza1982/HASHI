"""Compatibility exports derived from the backend registry SSOT."""

from __future__ import annotations

from orchestrator.flexible_backend_registry import (
    CLAUDE_MODEL_ALIASES as _CLAUDE_MODEL_ALIASES,
    get_all_gateway_models,
    get_available_efforts,
    get_available_models,
    get_default_gateway_model,
    get_gateway_engine_for_model,
    get_gateway_models,
)


AVAILABLE_GEMINI_MODELS = get_gateway_models("gemini-cli")
AVAILABLE_OPENROUTER_MODELS = get_available_models("openrouter-api")
AVAILABLE_CLAUDE_MODELS = get_gateway_models("claude-cli")
CLAUDE_MODEL_ALIASES = dict(_CLAUDE_MODEL_ALIASES)
AVAILABLE_CLAUDE_EFFORTS = get_available_efforts("claude-cli")
AVAILABLE_CODEX_MODELS = get_gateway_models("codex-cli")
AVAILABLE_CODEX_EFFORTS = get_available_efforts("codex-cli")
AVAILABLE_XAI_API_MODELS = get_gateway_models("xai-api")


def available_gateway_models() -> list[str]:
    return get_all_gateway_models()


def gateway_engine_for_model(model: str) -> str | None:
    return get_gateway_engine_for_model(model)


def default_gateway_model() -> str:
    return str(get_default_gateway_model() or "")
