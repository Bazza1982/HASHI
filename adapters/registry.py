"""Backend adapter registry — maps engine names to adapter classes."""

from __future__ import annotations

import importlib
import importlib.util

from orchestrator.flexible_backend_registry import canonical_backend_engine


_BACKEND_ADAPTER_TARGETS: dict[str, tuple[str, str]] = {
    "gemini-cli": ("adapters.gemini_cli", "GeminiCLIAdapter"),
    "openrouter-api": ("adapters.openrouter_api", "OpenRouterAdapter"),
    "deepseek-api": ("adapters.deepseek_api", "DeepSeekAdapter"),
    "openai-compatible-api": (
        "adapters.openai_compatible_api",
        "OpenAICompatibleAdapter",
    ),
    "claude-cli": ("adapters.claude_cli", "ClaudeCLIAdapter"),
    "codex-cli": ("adapters.codex_cli", "CodexCLIAdapter"),
    "her-v2": ("adapters.her_v2", "HERv2Adapter"),
    "grok-cli": ("adapters.grok_cli", "GrokCLIAdapter"),
    "ollama-api": ("adapters.ollama_api", "OllamaAdapter"),
    "xai-api": ("adapters.xai_api", "XaiApiAdapter"),
    "hashi-api": ("adapters.hashi_api", "HashiApiAdapter"),
}


def registered_backend_engines() -> frozenset[str]:
    """Return every canonical Engine ID owned by the adapter registry."""

    return frozenset(_BACKEND_ADAPTER_TARGETS)


def packaged_backend_engines() -> frozenset[str]:
    """Return adapters physically present in this source distribution."""

    return frozenset(
        engine
        for engine, (module_name, _class_name) in _BACKEND_ADAPTER_TARGETS.items()
        if importlib.util.find_spec(module_name) is not None
    )


def get_backend_class(engine_name: str):
    engine = canonical_backend_engine(engine_name)
    try:
        module_name, class_name = _BACKEND_ADAPTER_TARGETS[engine]
    except KeyError as exc:
        raise ValueError(f"Unknown engine: {engine}") from exc
    module = importlib.import_module(module_name)
    return getattr(module, class_name)
