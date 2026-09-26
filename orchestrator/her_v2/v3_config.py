"""HER v3 single-model configuration; legacy names are storage compatibility only."""
from __future__ import annotations
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from collections.abc import Sequence
from typing import Any

from orchestrator.flexible_backend_registry import (
    BACKEND_REGISTRY,
    HER_V2_ENGINE,
    canonical_backend_engine,
    get_available_models,
    get_backend_label,
    get_default_model,
)


HER_V3_CONFIGURATION_STATE_KEY = "her_v3_configuration"


@dataclass(frozen=True)
class HERv3ModelTarget:
    """The only user-selectable model target in HER v3."""

    provider: str
    model: str

    def __post_init__(self) -> None:
        provider = canonical_backend_engine(self.provider)
        model = str(self.model or "").strip()
        if not provider or provider == "her-v2":
            raise ValueError("HER v3 target requires a concrete Provider")
        if not model:
            raise ValueError("HER v3 target requires a model")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "model", model)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "HERv3ModelTarget":
        if not isinstance(raw, Mapping):
            raise ValueError("HER v3 target must be an object")
        return cls(
            provider=str(raw.get("provider") or raw.get("engine") or "").strip(),
            model=str(raw.get("model") or "").strip(),
        )

    def to_dict(self) -> dict[str, str]:
        return {"provider": self.provider, "model": self.model}

@dataclass(frozen=True)
class CompanionConfig:
    enabled: bool = False
    interval_minutes: int = 5
    model: str = "jev-latest"
    api_key_env: str = "TYPESAFE_API_KEY"
    check_timeout_s: float = 5.0

    @classmethod
    def from_mapping(cls, raw: Any) -> "CompanionConfig":
        if raw is None:
            return cls()
        if not isinstance(raw, Mapping):
            raise ValueError("agent_companion must be an object")
        enabled = raw.get("enabled", False)
        interval = raw.get("interval_minutes", 5)
        timeout = raw.get("check_timeout_s", 5.0)
        if not isinstance(enabled, bool):
            raise ValueError("agent_companion.enabled must be a boolean")
        if isinstance(interval, bool) or interval not in (5, 10):
            raise ValueError("agent_companion.interval_minutes must be 5 or 10")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("agent_companion.check_timeout_s must be positive")
        model = str(raw.get("model", "jev-latest")).strip()
        key_env = str(raw.get("api_key_env", "TYPESAFE_API_KEY")).strip()
        if not model or not key_env.isidentifier():
            raise ValueError("agent_companion requires a model and credential environment variable")
        return cls(
            enabled=enabled,
            interval_minutes=int(interval),
            model=model,
            api_key_env=key_env,
            check_timeout_s=float(timeout),
        )


def normalise_v3_config(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Collapse the old route/stage model matrix to one main model + auxiliary lane."""
    result = deepcopy(dict(raw))
    for key in ("profiles", "stage_roles", "targets", "route_targets", "route_reasoning", "stage_reasoning", "slot_models"):
        if raw.get(key) is not None and not isinstance(raw[key], Mapping):
            raise ValueError(f"HER {key} must be an object")
    for key in ("strategy_tools_enabled", "planning_tools_enabled"):
        if key in raw and not isinstance(raw[key], bool):
            raise ValueError(f"{key} must be a boolean (retired in HER v3)")
    if raw.get("auxiliary") is not None and not isinstance(raw["auxiliary"], Mapping):
        raise ValueError("HER auxiliary must be an object")

    profiles = raw.get("profiles") or {}
    roles = raw.get("stage_roles") or {}
    if not isinstance(profiles, Mapping) or not isinstance(roles, Mapping):
        raise ValueError("HER profiles and stage_roles must be objects")

    role = str(roles.get("execution") or "premium")
    legacy = profiles.get(role) or profiles.get("main") or profiles.get("lightweight")
    if legacy is None and profiles:
        legacy = next(iter(profiles.values()))
    main = raw.get("main")
    explicit = isinstance(main, Mapping)
    if main is not None and not explicit:
        raise ValueError("HER v3 main must be an object")
    source = main if explicit else legacy
    if not isinstance(source, Mapping):
        raise ValueError("HER v3 requires main.provider/model or a legacy Execution profile")

    primary = deepcopy(dict(source))
    if "provider" in primary:
        primary["engine"] = primary.pop("provider")
    if not explicit:
        targets = raw.get("targets") or {}
        routes = raw.get("route_targets") or {}
        target = routes.get("execution_complex") or targets.get("pro") or {}
        if isinstance(target, Mapping):
            primary["engine"] = target.get("provider") or target.get("engine") or primary.get("engine")
            primary["model"] = target.get("model") or (raw.get("slot_models") or {}).get("pro") or primary.get("model")
        reasoning = (raw.get("route_reasoning") or {}).get("execution_complex")
        reasoning = reasoning or (raw.get("stage_reasoning") or {}).get("execution")
        if reasoning and reasoning != "inherit":
            primary["reasoning"] = reasoning
    primary["reasoning"] = primary.get("reasoning") or "default"

    auxiliary = deepcopy(dict(raw.get("auxiliary") or primary))
    if "provider" in auxiliary:
        auxiliary["engine"] = auxiliary.pop("provider")
    auxiliary.setdefault("reasoning", "default")

    for profile in (primary, auxiliary):
        if not str(profile.get("engine") or "").strip() or not str(profile.get("model") or "").strip():
            raise ValueError("HER v3 model targets require provider and model")
        for key in ("provider_reasoning", "reasoning_effort", "_native_audio_route"):
            profile.pop(key, None)

    result["profiles"] = {"main": primary, "auxiliary": auxiliary}
    result["stage_roles"] = {
        name: "main"
        for name in (
            "direct", "triage", "planning", "execution", "replanning", "review", "finalisation"
        )
    }
    result["stage_roles"].update(
        {name: "auxiliary" for name in ("immediate_response", "meditation", "dream")}
    )
    for key in (
        "stage_reasoning", "slot_models", "targets", "model_slots", "route_targets",
        "route_reasoning", "route_model_slots", "fallback", "voice_routes",
    ):
        result.pop(key, None)
    result["routing_mode"] = "single"
    result["direct_strategy_self_selection"] = raw.get(
        "strategy_cards", raw.get("direct_strategy_self_selection", False)
    )
    result["strategy_tools_enabled"] = False
    result["planning_tools_enabled"] = False
    result["review_limits"] = {
        name: 0 for name in ("zero", "low", "medium", "high", "xhigh", "max")
    }
    return result


def resolve_v3_target(
    raw: Mapping[str, Any],
    override: Mapping[str, Any] | None = None,
) -> HERv3ModelTarget:
    """Resolve the configured main model plus an optional persisted selection."""

    if override is not None:
        return HERv3ModelTarget.from_mapping(override)
    normalized = normalise_v3_config(raw)
    main = (normalized.get("profiles") or {}).get("main")
    return HERv3ModelTarget.from_mapping(main)


def apply_v3_target(
    raw: Mapping[str, Any],
    target: HERv3ModelTarget,
) -> dict[str, Any]:
    """Apply one target and erase legacy model-routing controls from the view."""

    updated = deepcopy(dict(raw))
    current_main = updated.get("main")
    current_main = dict(current_main) if isinstance(current_main, Mapping) else {}
    current_main.update(target.to_dict())
    updated["main"] = current_main
    # The auxiliary lane follows the main target unless an explicitly separate
    # non-user-facing auxiliary target exists in configuration.
    if not isinstance(updated.get("auxiliary"), Mapping):
        updated["auxiliary"] = dict(current_main)
    return normalise_v3_config(updated)


def _unique_strings(values) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text.casefold() != "role-configured" and text not in result:
            result.append(text)
    return result


def build_v3_provider_options(
    allowed_backends: Sequence[Mapping[str, Any]],
    provider_profiles: Mapping[str, Mapping[str, Any]],
    target: HERv3ModelTarget,
) -> list[dict[str, Any]]:
    """Derive HER v3 Provider/model choices without the v2 route matrix."""

    rows_by_engine: dict[str, list[Mapping[str, Any]]] = {}
    for row in allowed_backends:
        engine = canonical_backend_engine(str(row.get("engine") or ""))
        if engine and engine != HER_V2_ENGINE:
            rows_by_engine.setdefault(engine, []).append(row)

    metadata: dict[str, tuple[str, Mapping[str, Any]]] = {}
    ordered: list[str] = []
    for raw_name, raw_profile in provider_profiles.items():
        if not isinstance(raw_profile, Mapping):
            continue
        name = str(raw_name).strip()
        engine = canonical_backend_engine(
            str(
                raw_profile.get("engine")
                or (name if name.endswith("-api") else f"{name}-api")
            ).strip()
        )
        if not engine or engine == HER_V2_ENGINE:
            continue
        metadata[engine] = (name, raw_profile)
        if engine not in ordered:
            ordered.append(engine)
    if target.provider not in ordered:
        ordered.append(target.provider)
    for engine in rows_by_engine:
        if engine not in ordered:
            ordered.append(engine)

    options: list[dict[str, Any]] = []
    for engine in ordered:
        name, profile = metadata.get(engine, (engine.removesuffix("-api"), {}))
        rows = rows_by_engine.get(engine, [])
        values: list[Any] = []
        for source in [*rows, profile]:
            models = source.get("models")
            if isinstance(models, list):
                values.extend(models)
            values.extend(
                source.get(key)
                for key in ("default_model", "model", "fast_model", "pro_model")
            )
        values.extend(get_available_models(engine))
        models = _unique_strings(values)
        status = str(profile.get("status") or "stable").strip().casefold()
        reason = None
        if status == "disabled":
            reason = "Provider is disabled"
        elif engine not in BACKEND_REGISTRY:
            reason = "Provider adapter is not installed"
        elif not models:
            reason = "Provider has no configured models"
        default_model = str(
            profile.get("pro_model")
            or profile.get("default_model")
            or profile.get("model")
            or get_default_model(engine)
            or next(iter(models), "")
        ).strip()
        if target.provider == engine and target.model in models:
            default_model = target.model
        options.append(
            {
                "name": name,
                "engine": engine,
                "label": get_backend_label(engine),
                "status": status,
                "models": models,
                "default_model": default_model,
                # Compatibility field names for callers that previously chose
                # Quick/Pro. Both now mean the one HER v3 default target.
                "fast_model": default_model,
                "pro_model": default_model,
                "available": reason is None,
                "reason": reason,
            }
        )
    return options
