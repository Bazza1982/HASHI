from __future__ import annotations

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
            "claude-sonnet-4-6",
            "claude-opus-4-6",
            "claude-haiku-4-5",
        ],
        "default_model": "claude-sonnet-4-6",
        "efforts": ["low", "medium", "high"],
        "default_effort": "medium",
        "secret_keys": ["claude-cli_key"],
    },
    "codex-cli": {
        "label": "codex",
        "models": [
            "gpt-5.4",
            "gpt-5.3-codex",
            "gpt-5.2-codex",
            "gpt-5.2",
            "gpt-5.1-codex-max",
            "gpt-5.1-codex-mini",
        ],
        "default_model": "gpt-5.4",
        "efforts": ["low", "medium", "high", "extra_high"],
        "default_effort": "medium",
        "secret_keys": ["codex-cli_key"],
    },
    "deepseek-api": {
        "label": "deepseek",
        "models": [
            "deepseek-reasoner",
            "deepseek-chat",
        ],
        "default_model": "deepseek-reasoner",
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
    "opus": "claude-opus-4-6",
    "claude-opus": "claude-opus-4-6",
    "haiku": "claude-haiku-4-5",
}


def get_backend_entry(engine: str) -> dict:
    return BACKEND_REGISTRY.get(engine, {})


def get_backend_label(engine: str) -> str:
    return str(get_backend_entry(engine).get("label") or engine)


def get_available_models(engine: str) -> list[str]:
    return list(get_backend_entry(engine).get("models") or [])


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


def get_available_efforts(engine: str) -> list[str]:
    return list(get_backend_entry(engine).get("efforts") or [])


def get_default_effort(engine: str) -> str | None:
    default_effort = get_backend_entry(engine).get("default_effort")
    if default_effort:
        return str(default_effort)
    efforts = get_available_efforts(engine)
    return efforts[0] if efforts else None


def normalize_effort(engine: str, effort: str | None) -> str | None:
    if effort == "extra":
        effort = "extra_high"
    efforts = get_available_efforts(engine)
    if not efforts:
        return None
    if not effort:
        return get_default_effort(engine)
    effort = effort.lower()
    if effort not in efforts:
        return get_default_effort(engine)
    return effort


def get_secret_lookup_order(engine: str, agent_name: str) -> list[str]:
    raw_keys = get_backend_entry(engine).get("secret_keys") or [f"{engine}_key"]
    return [str(key).format(agent_name=agent_name) for key in raw_keys]
