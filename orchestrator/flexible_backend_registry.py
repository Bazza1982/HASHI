from __future__ import annotations

HER_V2_ENGINE = "her-v2"
RETIRED_HER_ENGINE_ALIASES = frozenset({"her"})
REMOVED_ENGINE_IDS = frozenset({"claw-cli"})
CLI_ENGINES = frozenset(
    {
        "gemini-cli",
        "claude-cli",
        "codex-cli",
        HER_V2_ENGINE,
        "grok-cli",
    }
)

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
        # Current app-server capability probes prove the full set below for
        # these two HASHI gateway models. Keep the override model-specific so
        # unverified Codex variants do not inherit `none` or `max`.
        "model_efforts": {
            "gpt-5.6-luna": ["none", "low", "medium", "high", "xhigh", "max"],
            "gpt-5.6-sol": ["none", "low", "medium", "high", "xhigh", "max"],
        },
        "default_effort": "medium",
        "secret_keys": ["codex-cli_key"],
    },
    "her-v2": {
        "label": "HER",
        "privacy_levels": [0, 1],
        "models": ["role-configured"],
        "default_model": "role-configured",
        "efforts": ["zero", "low", "medium", "high", "xhigh", "max"],
        "default_effort": "medium",
        # Each role profile resolves credentials through its concrete provider.
        "secret_keys": [],
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
            "deepseek/deepseek-v4-pro",
            "deepseek/deepseek-v4-flash",
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
    "hashi-api": {
        "label": "hashi",
        "privacy_levels": [0, 1, 2],
        "models": [
            "gpt-5.6-luna",
            "gpt-5.6-sol",
        ],
        "default_model": "gpt-5.6-luna",
        "fast_model": "gpt-5.6-luna",
        "pro_model": "gpt-5.6-sol",
        "efforts": ["none", "low", "medium", "high", "xhigh", "max"],
        "default_effort": "medium",
        "secret_keys": [],
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
    """Resolve the public HER compatibility ID to the supported runtime.

    ``her`` remains a public migration alias. ``claw-cli`` is deliberately not
    an alias: it named the removed upstream-derived runtime and must not imply
    that the clean-room HER v2 implementation is Claw-compatible.
    """

    value = str(engine or "").strip()
    return HER_V2_ENGINE if value in RETIRED_HER_ENGINE_ALIASES else value


def normalize_allowed_backends(backends: list) -> list[dict]:
    """Normalize backend IDs without silently adding an execution backend.

    HER v2 requires explicit provider-role grants, so synthesising a generic
    HER row would be both unusable and an authority expansion.  If an explicit
    ``her-v2`` row is present, obsolete alias rows are discarded rather than
    allowed to shadow its configuration.
    """

    normalized: list[dict] = []
    explicit_v2 = any(
        str(
            (raw if isinstance(raw, str) else dict(raw).get("engine")) or ""
        ).strip()
        == HER_V2_ENGINE
        for raw in backends or []
    )
    for raw in backends or []:
        item = {"engine": raw} if isinstance(raw, str) else dict(raw)
        source_engine = str(item.get("engine") or "").strip()
        if source_engine in REMOVED_ENGINE_IDS:
            raise ValueError(
                "Backend 'claw-cli' has been removed; configure 'her-v2' instead."
            )
        if explicit_v2 and source_engine in RETIRED_HER_ENGINE_ALIASES:
            continue
        engine = canonical_backend_engine(source_engine)
        if not engine:
            continue
        item["engine"] = engine
        normalized.append(item)
    return normalized


def is_cli_backend(engine: str | None) -> bool:
    return canonical_backend_engine(engine) in CLI_ENGINES


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
    if canonical_backend_engine(engine) == HER_V2_ENGINE:
        effort = {
            "fast": "low",
            "fast_path": "low",
            "planned": "medium",
            "adaptive": "high",
            "reviewed": "xhigh",
            "assured": "max",
        }.get(
            str(effort or "")
            .strip()
            .casefold()
            .replace("-", "_")
            .replace(" ", "_"),
            effort,
        )
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
    entry = get_backend_entry(engine)
    raw_keys = entry.get("secret_keys")
    if raw_keys is None:
        raw_keys = [f"{canonical_backend_engine(engine)}_key"]
    return [str(key).format(agent_name=agent_name) for key in raw_keys]
