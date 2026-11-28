"""HERV3 single-model configuration; legacy names are storage compatibility only."""
from __future__ import annotations
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from collections.abc import Sequence
from typing import Any

from orchestrator.flexible_backend_registry import (
    BACKEND_REGISTRY,
    HER_V2_ENGINE,
    canonical_model_provider,
    public_model_provider,
    get_backend_label,
    get_default_model,
)
from orchestrator.runtime_effort_options import get_available_models, provider_backend_rows


HER_V3_CONFIGURATION_STATE_KEY = "her_v3_configuration"


@dataclass(frozen=True)
class HERv3ModelTarget:
    """The only user-selectable model target in HERV3."""

    provider: str
    model: str

    def __post_init__(self) -> None:
        provider = canonical_model_provider(self.provider)
        model = str(self.model or "").strip()
        if not provider or provider == "her-v2":
            raise ValueError("HERV3 target requires a concrete Provider")
        if not model:
            raise ValueError("HERV3 target requires a model")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "model", model)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "HERv3ModelTarget":
        if not isinstance(raw, Mapping):
            raise ValueError("HERV3 target must be an object")
        return cls(
            provider=str(raw.get("provider") or raw.get("engine") or "").strip(),
            model=str(raw.get("model") or "").strip(),
        )

    def to_dict(self) -> dict[str, str]:
        return {"provider": public_model_provider(self.provider), "model": self.model}

def normalise_v3_config(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Collapse the old route/stage model matrix to one main model + auxiliary lane."""
    result = deepcopy(dict(raw))
    if "provider_policies" in raw:
        provider_backend_rows([{"engine": HER_V2_ENGINE, "her_v2": raw}])
    for key in ("profiles", "stage_roles", "targets", "route_targets", "route_reasoning", "stage_reasoning", "slot_models"):
        if raw.get(key) is not None and not isinstance(raw[key], Mapping):
            raise ValueError(f"HER {key} must be an object")
    for key in ("strategy_tools_enabled", "planning_tools_enabled"):
        if key in raw and not isinstance(raw[key], bool):
            raise ValueError(f"{key} must be a boolean (retired in HERV3)")
    if raw.get("auxiliary") is not None and not isinstance(raw["auxiliary"], Mapping):
        raise ValueError("HER auxiliary must be an object")
    provider_allowlist = raw.get("v3_provider_allowlist")
    if provider_allowlist is not None:
        if not isinstance(provider_allowlist, list) or not provider_allowlist or any(
            not isinstance(value, str) or not value.strip() for value in provider_allowlist
        ):
            raise ValueError("HERV3 provider allowlist must be a nonempty list of Providers")
        provider_allowlist = [canonical_model_provider(value) for value in provider_allowlist]

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
        raise ValueError("HERV3 main must be an object")
    source = main if explicit else legacy
    if not isinstance(source, Mapping):
        raise ValueError("HERV3 requires main.provider/model or a legacy Execution profile")

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

    # A normalized compatibility view may pass through this function again at
    # the adapter/config boundary; retain its resolved auxiliary and warnings.
    already_normalized = (
        set(profiles) == {"main", "auxiliary"}
        and roles.get("execution") == "main"
        and raw.get("routing_mode") == "single"
    )
    inherited_warnings = raw.get("migration_warnings") if already_normalized else ()
    migration_warnings: list[dict[str, str]] = [
        {key: str(item.get(key) or "") for key in ("field", "action", "detail")}
        for item in inherited_warnings
        if isinstance(item, Mapping) and item.get("field") and item.get("action")
    ] if isinstance(inherited_warnings, (tuple, list)) else []
    inherited_warning_count = len(migration_warnings)
    explicit_auxiliary = isinstance(raw.get("auxiliary"), Mapping)
    normalized_auxiliary = already_normalized and isinstance(profiles.get("auxiliary"), Mapping)
    auxiliary_origin = "main"
    if explicit_auxiliary:
        auxiliary = deepcopy(dict(raw["auxiliary"]))
        auxiliary_origin = "explicit"
    elif normalized_auxiliary:
        previous_main = profiles.get("main")
        previous_main = previous_main if isinstance(previous_main, Mapping) else {}
        previous_auxiliary = profiles["auxiliary"]
        auxiliary_origin = str(raw.get("_herv3_auxiliary_origin") or "")
        if auxiliary_origin not in {"main", "profile", "explicit"}:
            same_target = (
                previous_main.get("engine") == previous_auxiliary.get("engine")
                and previous_main.get("model") == previous_auxiliary.get("model")
            )
            auxiliary_origin = "main" if same_target else "profile"
        auxiliary = deepcopy(dict(primary if auxiliary_origin == "main" else previous_auxiliary))
    else:
        candidate_name = "lightweight" if profiles.get("lightweight") else "triage"
        aux_candidate = profiles.get(candidate_name)
        if (
            isinstance(aux_candidate, Mapping)
            and str(aux_candidate.get("engine") or aux_candidate.get("provider") or "").strip()
            and str(aux_candidate.get("model") or "").strip()
        ):
            candidate_copy = deepcopy(dict(aux_candidate))
            if "provider" in candidate_copy:
                candidate_copy["engine"] = candidate_copy.pop("provider")
            candidate_engine = canonical_model_provider(candidate_copy["engine"])
            if provider_allowlist is None or candidate_engine in provider_allowlist:
                auxiliary = candidate_copy
                auxiliary_origin = "profile"
            else:
                auxiliary = deepcopy(primary)
                migration_warnings.append(
                    {
                        "field": f"profiles.{candidate_name}",
                        "action": "fallback_to_main",
                        "detail": f"{candidate_name} profile engine is outside v3_provider_allowlist",
                    }
                )
        else:
            auxiliary = deepcopy(primary)
    if "provider" in auxiliary:
        auxiliary["engine"] = auxiliary.pop("provider")
    # Auxiliary presentation/housekeeping stages (commentary, final style,
    # meditation, dream) must not silently inherit expensive reasoning from the
    # main target; disable reasoning by default unless explicitly requested.
    if auxiliary_origin == "main" or (not explicit_auxiliary and not normalized_auxiliary):
        auxiliary["reasoning"] = "off"
    else:
        auxiliary.setdefault("reasoning", "off")

    for profile in (primary, auxiliary):
        if not str(profile.get("engine") or "").strip() or not str(profile.get("model") or "").strip():
            raise ValueError("HERV3 model targets require provider and model")
        profile["engine"] = canonical_model_provider(profile["engine"])
        if provider_allowlist is not None and profile["engine"] not in provider_allowlist:
            raise ValueError("HERV3 model target is outside the configured Provider allowlist")
        for key in ("provider_reasoning", "reasoning_effort", "_native_audio_route"):
            profile.pop(key, None)

    result["profiles"] = {"main": primary, "auxiliary": auxiliary}
    result["_herv3_auxiliary_origin"] = auxiliary_origin
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
        removed = result.pop(key, None)
        if removed:
            migration_warnings.append(
                {
                    "field": key,
                    "action": "ignored_by_v3",
                    "detail": f"legacy HERV2 field {key!r} is retired in HERV3 single-model routing",
                }
            )
    if raw.get("strategy_tools_enabled") is True:
        migration_warnings.append(
            {
                "field": "strategy_tools_enabled",
                "action": "forced_false",
                "detail": "strategy_tools_enabled is retired in HERV3",
            }
        )
    if raw.get("planning_tools_enabled") is True:
        migration_warnings.append(
            {
                "field": "planning_tools_enabled",
                "action": "forced_false",
                "detail": "planning_tools_enabled is retired in HERV3",
            }
        )
    raw_review_limits = raw.get("review_limits")
    if isinstance(raw_review_limits, Mapping) and any(
        bool(v) for v in raw_review_limits.values()
    ):
        migration_warnings.append(
            {
                "field": "review_limits",
                "action": "forced_zero",
                "detail": "stage review_limits are retired in HERV3",
            }
        )
    result["routing_mode"] = "single"
    result["direct_strategy_self_selection"] = raw.get(
        "strategy_cards", raw.get("direct_strategy_self_selection", False)
    )
    result["strategy_tools_enabled"] = False
    result["planning_tools_enabled"] = False
    result["review_limits"] = {
        name: 0 for name in ("zero", "low", "medium", "high", "xhigh", "max")
    }
    new_warnings = migration_warnings[inherited_warning_count:]
    if new_warnings:
        import logging

        # Retired HERV2 routing fields stay silently compatible: they are
        # dropped into ``migration_warnings`` for diagnostics, but they are
        # not an operational fault and must not spam the log on every load.
        logging.getLogger(__name__).debug(
            "HERV3 config normalization dropped %d retired field(s): %s",
            len(new_warnings),
            ", ".join(item["field"] for item in new_warnings),
        )
    result["migration_warnings"] = tuple(migration_warnings)
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
    if not isinstance(current_main, Mapping):
        # Legacy configurations may have only an Execution profile. Resolve
        # that profile before changing its target so its reasoning and other
        # configured options survive the conversion to an explicit v3 main.
        # The old target may have left the current allowlist; this temporary
        # projection makes no call and the final normalization still checks
        # both selected targets against the unchanged allowlist.
        previous = dict(updated)
        previous.pop("v3_provider_allowlist", None)
        current_main = normalise_v3_config(previous)["profiles"]["main"]
    current_main = dict(current_main)
    current_main.update(target.to_dict())
    updated["main"] = current_main
    # Normalization preserves a configured auxiliary, or derives it from the
    # new main target when it previously followed main.
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
    allowed_providers: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Derive HERV3 Provider/model choices without the v2 route matrix."""

    rows_by_engine: dict[str, list[Mapping[str, Any]]] = {}
    for row in provider_backend_rows(allowed_backends):
        engine = canonical_model_provider(str(row.get("engine") or ""))
        if engine and engine != HER_V2_ENGINE:
            rows_by_engine.setdefault(engine, []).append(row)

    metadata: dict[str, tuple[str, Mapping[str, Any]]] = {}
    ordered: list[str] = []
    for raw_name, raw_profile in provider_profiles.items():
        if not isinstance(raw_profile, Mapping):
            continue
        name = str(raw_name).strip()
        engine = canonical_model_provider(
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
    allowed = {canonical_model_provider(value) for value in allowed_providers} if allowed_providers is not None else None
    for engine in ordered:
        if allowed is not None and engine not in allowed:
            continue
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
        configured_models = _unique_strings(values)
        models = _unique_strings(
            model
            for row in rows or [profile]
            for model in get_available_models(
                engine, backend={**row, "models": configured_models}
            )
        )
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
        if default_model not in models:
            default_model = next(iter(models), "")
        # Profile-level PCM projection preference travels with the Provider
        # option so HERV3 model/Provider switching honours per-Provider
        # overrides without a separate configuration subsystem.
        pcm_profile_override = profile.get("pcm_profile")
        if pcm_profile_override in (None, ""):
            for row in rows:
                candidate = row.get("pcm_profile")
                if candidate not in (None, ""):
                    pcm_profile_override = candidate
                    break
        options.append(
            {
                "name": name,
                "engine": engine,
                "label": get_backend_label(engine),
                "status": status,
                "models": models,
                "default_model": default_model,
                # Compatibility field names for callers that previously chose
                # Quick/Pro. Both now mean the one HERV3 default target.
                "fast_model": default_model,
                "pro_model": default_model,
                "available": reason is None,
                "reason": reason,
                "pcm_profile": pcm_profile_override,
            }
        )
    return options
# HASHI-Fingerprint: hfp_e861c2ecae9a44b58a81ace633eff273
