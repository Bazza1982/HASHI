from __future__ import annotations

CLI_ENGINES = frozenset({"gemini-cli", "claude-cli", "codex-cli", "claw-cli", "grok-cli"})

BACKEND_REGISTRY: dict[str, dict] = {
    "gemini-cli": {
        "label": "gemini",
        "models": [
            "gemini-3.1-pro-preview",
            "gemini-3-flash-preview",
            "gemini-2.5-pro",
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
        ],
        "default_model": "gemini-2.5-flash",
        "efforts": [],
        "default_effort": None,
        "secret_keys": ["gemini-cli_key"],
    },
    "claude-cli": {
        "label": "claude",
        "models": [
            "claude-opus-4-7",
            "claude-sonnet-4-6",
            "claude-opus-4-6",
            "claude-haiku-4-5",
        ],
        "default_model": "claude-sonnet-4-6",
        "efforts": ["low", "medium", "high", "xhigh", "max"],
        "default_effort": "medium",
        "secret_keys": ["claude-cli_key"],
    },
    "codex-cli": {
        "label": "codex",
        "models": [
            "gpt-5.6-sol",
            "gpt-5.6-terra",
            "gpt-5.6-luna",
            "gpt-5.5",
            "gpt-5.3-codex-spark",
            "gpt-5.4",
            "gpt-5.3-codex",
            "gpt-5.2-codex",
            "gpt-5.2",
            "gpt-5.1-codex-max",
            "gpt-5.1-codex-mini",
        ],
        "default_model": "gpt-5.4",
        "efforts": ["low", "medium", "high", "xhigh"],
        # GPT-5.6 Sol is the only Codex model currently documented with the
        # deeper `max` reasoning tier. Keep this model-specific so the UI
        # never offers an unverified effort to Terra or Luna.
        "model_efforts": {
            "gpt-5.6-sol": ["low", "medium", "high", "xhigh", "max"],
        },
        "default_effort": "medium",
        "secret_keys": ["codex-cli_key"],
    },
    "claw-cli": {
        "label": "claw",
        "allow_custom_models": True,
        "models": [
            "deepseek/deepseek-v4-flash",
            "deepseek/deepseek-v4-pro",
            "openai/gpt-4.1-mini",
        ],
        "default_model": "deepseek/deepseek-v4-flash",
        "efforts": [],
        "default_effort": None,
        "secret_keys": [
            "{agent_name}_openrouter_key",
            "openrouter-api_key",
            "openrouter_key",
        ],
    },
    "grok-cli": {
        "label": "grok",
        "models": [
            "grok-composer-2.5-fast",
            "grok-build",
        ],
        "default_model": "grok-composer-2.5-fast",
        "efforts": [],
        "default_effort": None,
        "secret_keys": [],
    },
    "deepseek-api": {
        "label": "deepseek",
        "models": [
            "deepseek-v4-pro",
            "deepseek-v4-flash",
            "deepseek-reasoner",
            "deepseek-chat",
        ],
        "default_model": "deepseek-v4-pro",
        "efforts": [],
        "default_effort": None,
        "secret_keys": [
            "deepseek-api_key",
            "deepseek_api_key",
        ],
    },
    "ollama-api": {
        "label": "ollama",
        "models": [
            "gemma4:26b",
            "gemma4:31b",
            "qwen3:32b",
        ],
        "default_model": "gemma4:26b",
        "efforts": [],
        "default_effort": None,
        "secret_keys": [],
    },
    "xai-api": {
        "label": "xai",
        "models": [
            "grok-4.3",
            "grok-build-0.1",
            "grok-4.20-0309-reasoning",
            "grok-4.20-0309-non-reasoning",
            "grok-4.20-multi-agent-0309",
            "grok-imagine-image",
            "grok-imagine-image-quality",
        ],
        "default_model": "grok-4.3",
        "efforts": [],
        "default_effort": None,
        "secret_keys": [
            "xai_oauth_refresh_token",
            "xai_api_key",
            "XAI_API_KEY",
        ],
    },
    "openrouter-api": {
        "label": "openrouter",
        "models": [
            "deepseek/deepseek-v3.2-exp",
            "moonshotai/kimi-k2.5",
            "google/gemini-3.1-flash-lite-preview",
            "anthropic/claude-sonnet-4.6",
            "anthropic/claude-opus-4.6",
            "anthropic/claude-opus-4.5",
        ],
        "default_model": "anthropic/claude-sonnet-4.6",
        "efforts": [],
        "default_effort": None,
        "secret_keys": [
            "{agent_name}_openrouter_key",
            "openrouter-api_key",
            "openrouter_key",
        ],
    },
}

CLAUDE_MODEL_ALIASES = {
    "sonnet": "claude-sonnet-4-6",
    "claude-sonnet": "claude-sonnet-4-6",
    "claude-sonnet-4": "claude-sonnet-4-6",
    "claude-sonnet-4.6": "claude-sonnet-4-6",
    "opus": "claude-opus-4-7",
    "claude-opus": "claude-opus-4-7",
    "claude-opus-4": "claude-opus-4-7",
    "claude-opus-4.7": "claude-opus-4-7",
    "haiku": "claude-haiku-4-5",
}


def get_backend_entry(engine: str) -> dict:
    return BACKEND_REGISTRY.get(engine, {})


def is_cli_backend(engine: str | None) -> bool:
    return bool(engine and engine in CLI_ENGINES)


def get_backend_label(engine: str) -> str:
    return str(get_backend_entry(engine).get("label") or engine)


def get_available_models(engine: str) -> list[str]:
    return list(get_backend_entry(engine).get("models") or [])


def allows_custom_models(engine: str) -> bool:
    return bool(get_backend_entry(engine).get("allow_custom_models"))


def get_default_model(engine: str) -> str | None:
    default_model = get_backend_entry(engine).get("default_model")
    if default_model:
        return str(default_model)
    models = get_available_models(engine)
    return models[0] if models else None


def normalize_model(engine: str, model: str | None) -> str | None:
    if not model:
        return get_default_model(engine)
    if engine == "claude-cli":
        model = CLAUDE_MODEL_ALIASES.get(model.lower(), model)
    models = get_available_models(engine)
    if models and model not in models:
        return get_default_model(engine)
    return model


def get_available_efforts(engine: str, model: str | None = None) -> list[str]:
    entry = get_backend_entry(engine)
    if model:
        model_efforts = entry.get("model_efforts") or {}
        if model in model_efforts:
            return list(model_efforts[model] or [])
    return list(entry.get("efforts") or [])


def get_default_effort(engine: str, model: str | None = None) -> str | None:
    default_effort = get_backend_entry(engine).get("default_effort")
    if default_effort:
        return str(default_effort)
    efforts = get_available_efforts(engine, model)
    return efforts[0] if efforts else None


def normalize_effort(engine: str, effort: str | None, model: str | None = None) -> str | None:
    if effort in ("extra", "extra_high"):
        effort = "xhigh"
    efforts = get_available_efforts(engine, model)
    if not efforts:
        return None
    if not effort:
        return get_default_effort(engine, model)
    effort = effort.lower()
    if effort not in efforts:
        return get_default_effort(engine, model)
    return effort


def get_secret_lookup_order(engine: str, agent_name: str) -> list[str]:
    raw_keys = get_backend_entry(engine).get("secret_keys") or [f"{engine}_key"]
    return [str(key).format(agent_name=agent_name) for key in raw_keys]
