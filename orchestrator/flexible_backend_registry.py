from __future__ import annotations

HER_ENGINE = "her"
LEGACY_HER_ENGINE = "claw-cli"
CLI_ENGINES = frozenset({"gemini-cli", "claude-cli", "codex-cli", HER_ENGINE, LEGACY_HER_ENGINE, "grok-cli"})

BACKEND_REGISTRY: dict[str, dict] = {
    "gemini-cli": {
        "label": "gemini",
        "gateway_enabled": True,
        "privacy_levels": [0, 1],
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
        "gateway_enabled": True,
        "privacy_levels": [0, 1],
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
        "gateway_enabled": True,
        "gateway_default_model": "gpt-5.4",
        "privacy_levels": [0, 1],
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
    "her": {
        "label": "HER",
        "privacy_levels": [0, 1],
        "allow_custom_models": True,
        "models": [
            "deepseek/deepseek-v4-flash",
            "deepseek/deepseek-v4-pro",
            "openai/gpt-4.1-mini",
        ],
        "default_model": "deepseek/deepseek-v4-flash",
        # HER's upstream Claw providers expose no reasoning-effort control. These
        # levels therefore represent agentic execution budget (maximum model/
        # tool-loop iterations), mapped by ClawCLIAdapter.
        "efforts": ["low", "medium", "high", "xhigh", "max", "max+"],
        "default_effort": "high",
        "secret_keys": [
            "{agent_name}_openrouter_key",
            "openrouter-api_key",
            "openrouter_key",
        ],
    },
    "grok-cli": {
        "label": "grok",
        "privacy_levels": [0, 1],
        "models": [
            "grok-4.5",
            "grok-composer-2.5-fast",
        ],
        "default_model": "grok-4.5",
        "efforts": ["low", "medium", "high"],
        "default_effort": "medium",
        "secret_keys": [],
    },
    "deepseek-api": {
        "label": "deepseek",
        "privacy_levels": [0, 1, 2],
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
        "privacy_levels": [0, 1, 2],
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
        "gateway_enabled": True,
        "privacy_levels": [0, 1, 2],
        "models": [
            "grok-4.5",
            "grok-4.3",
            "grok-build-0.1",
            "grok-4.20-0309-reasoning",
            "grok-4.20-0309-non-reasoning",
            "grok-4.20-multi-agent-0309",
            "grok-imagine-image",
            "grok-imagine-image-quality",
        ],
        "gateway_models": [
            "grok-4.5",
            "grok-4.3",
            "grok-build-0.1",
            "grok-4.20-0309-reasoning",
            "grok-4.20-0309-non-reasoning",
            "grok-4.20-multi-agent-0309",
            "grok-imagine-image",
            "grok-imagine-image-quality",
            "grok-imagine-video",
            "grok-imagine-video-1.5-preview",
        ],
        "default_model": "grok-4.5",
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
        "privacy_levels": [0, 1, 2],
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
    return BACKEND_REGISTRY.get(canonical_backend_engine(engine), {})


def canonical_backend_engine(engine: str | None) -> str:
    """Map legacy public IDs to their current HASHI backend ID."""
    value = str(engine or "").strip()
    return HER_ENGINE if value == LEGACY_HER_ENGINE else value


def default_her_backend() -> dict:
    """Return the built-in HER entry added to every flexible HASHI agent."""
    return {
        "engine": HER_ENGINE,
        "provider": "openrouter",
        "model": str(BACKEND_REGISTRY[HER_ENGINE]["default_model"]),
        "effort": str(BACKEND_REGISTRY[HER_ENGINE]["default_effort"]),
        "permission_mode": "read-only",
    }


def normalize_allowed_backends(backends: list) -> list[dict]:
    """Normalize legacy config and guarantee that built-in HER is selectable."""
    normalized: list[dict] = []
    her_seen = False
    for raw in backends or []:
        item = {"engine": raw} if isinstance(raw, str) else dict(raw)
        engine = canonical_backend_engine(item.get("engine"))
        if not engine:
            continue
        item["engine"] = engine
        if engine == HER_ENGINE:
            if her_seen:
                continue
            her_seen = True
        normalized.append(item)
    if not her_seen:
        normalized.append(default_her_backend())
    return normalized


def is_cli_backend(engine: str | None) -> bool:
    return bool(engine and engine in CLI_ENGINES)


def get_supported_privacy_levels(engine: str | None) -> tuple[int, ...]:
    if not engine:
        return (0, 1)
    levels = get_backend_entry(engine).get("privacy_levels") or [0, 1]
    return tuple(sorted({int(level) for level in levels}))


def get_backend_label(engine: str) -> str:
    return str(get_backend_entry(engine).get("label") or engine)


def get_available_models(engine: str) -> list[str]:
    return list(get_backend_entry(engine).get("models") or [])


def get_gateway_models(engine: str) -> list[str]:
    entry = get_backend_entry(engine)
    if not entry.get("gateway_enabled"):
        return []
    return list(entry.get("gateway_models") or entry.get("models") or [])


def get_all_gateway_models() -> list[str]:
    models: list[str] = []
    for engine in BACKEND_REGISTRY:
        for model in get_gateway_models(engine):
            if model not in models:
                models.append(model)
    return models


def get_gateway_engine_for_model(model: str) -> str | None:
    for engine in BACKEND_REGISTRY:
        if model in get_gateway_models(engine):
            return engine
    return None


def get_default_gateway_model() -> str | None:
    for engine, entry in BACKEND_REGISTRY.items():
        default_model = str(entry.get("gateway_default_model") or "").strip()
        if default_model and default_model in get_gateway_models(engine):
            return default_model
    models = get_all_gateway_models()
    return models[0] if models else None


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
