"""HER v3 single-model configuration; legacy names are storage compatibility only."""
from __future__ import annotations
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

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
