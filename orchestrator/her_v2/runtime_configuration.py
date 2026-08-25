"""Runtime-selectable provider, model-slot, and reasoning configuration for HER v2.

The persisted selection is deliberately separate from generic backend/model state.
HER effort remains orchestration policy and is never represented here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Any

from orchestrator.flexible_backend_registry import (
    BACKEND_REGISTRY,
    HER_V2_ENGINE,
    canonical_backend_engine,
    get_available_models,
    get_backend_entry,
    get_backend_label,
)

from .config import DEFAULT_STAGE_ROLES
from .models import DEFAULT_ROUTES_BY_STAGE, ROUTE_STAGES, Route, Stage

HER_V2_CONFIGURATION_STATE_KEY = "her_v2_configuration"
HER_V2_CONFIGURATION_DRAFT_STATE_KEY = "her_v2_configuration_draft"
HER_V2_CONFIGURATION_PRESETS_STATE_KEY = "her_v2_configuration_presets"
HER_V2_MODEL_SLOTS = ("fast", "pro")
HER_V2_ROUTING_MODES = ("single", "hybrid")
HER_V2_MIXED_VALUE = "mixed"
HER_V2_STANDARD_REASONING = ("off", "low", "medium", "high", "xhigh", "max")

_FAST_STAGES = frozenset(
    {
        Stage.DIRECT,
        Stage.IMMEDIATE_RESPONSE,
        Stage.TRIAGE,
        Stage.MEDITATION,
        Stage.DREAM,
    }
)


def _unique_strings(values: Sequence[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _common(values: Sequence[str]) -> str:
    unique = _unique_strings(values)
    if not unique:
        return ""
    return unique[0] if len(unique) == 1 else HER_V2_MIXED_VALUE


def _reasoning_value(value: Any, *, allow_inherit: bool = False) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if allow_inherit and text in {"", "default", "inherit"}:
        return None
    if not text:
        raise ValueError("provider reasoning value is required")
    if len(text) > 64 or any(ord(char) < 32 for char in text):
        raise ValueError("invalid provider reasoning value")
    return text


def _stage_roles(raw: Mapping[str, Any]) -> dict[Stage, str]:
    result = dict(DEFAULT_STAGE_ROLES)
    configured = raw.get("stage_roles")
    if isinstance(configured, Mapping):
        for name, role in configured.items():
            try:
                stage = Stage(str(name).strip().lower())
            except ValueError:
                continue
            role_name = str(role or "").strip()
            if role_name:
                result[stage] = role_name
    return result


def profile_model_slots(raw: Mapping[str, Any]) -> dict[str, str]:
    profiles = raw.get("profiles")
    if not isinstance(profiles, Mapping) or not profiles:
        raise ValueError("HER v2 profiles are required")

    explicit: dict[str, str] = {}
    slots = raw.get("model_slots")
    if isinstance(slots, Mapping):
        for slot in HER_V2_MODEL_SLOTS:
            value = slots.get(slot)
            names = value.get("profiles") if isinstance(value, Mapping) else value
            if isinstance(names, list):
                for name in names:
                    profile = str(name or "").strip()
                    if profile:
                        explicit[profile] = slot

    roles = _stage_roles(raw)
    fast_roles = {roles[stage] for stage in _FAST_STAGES if stage in roles}
    pro_roles = {
        roles[stage] for stage in Stage if stage not in _FAST_STAGES and stage in roles
    }
    if "orchestrator" in profiles:
        pro_roles.add("orchestrator")

    result: dict[str, str] = {}
    for raw_name in profiles:
        name = str(raw_name)
        is_fast_only = name not in pro_roles and (
            name in fast_roles or name in {"lightweight", "triage"}
        )
        if name in explicit:
            result[name] = explicit[name]
        elif is_fast_only:
            result[name] = "fast"
        else:
            result[name] = "pro"
    return result


def route_profile_names(raw: Mapping[str, Any]) -> dict[Route, str]:
    profiles = raw.get("profiles")
    available = set(profiles) if isinstance(profiles, Mapping) else set()
    roles = _stage_roles(raw)
    execution_fallback = roles.get(Stage.EXECUTION, "premium")
    execution_profiles = {
        Route.EXECUTION_SIMPLE: "lightweight",
        Route.EXECUTION_COMPLEX: "premium",
        Route.EXECUTION_HIGH_VOLUME: "orchestrator",
    }
    result = {
        route: roles.get(stage, "")
        for route, stage in ROUTE_STAGES.items()
        if route not in execution_profiles
    }
    for route, preferred in execution_profiles.items():
        result[route] = preferred if preferred in available else execution_fallback
    return result


def default_route_model_slots(raw: Mapping[str, Any]) -> dict[str, str]:
    profile_slots = profile_model_slots(raw)
    route_profiles = route_profile_names(raw)
    return {
        route.value: profile_slots.get(profile, "pro")
        for route, profile in route_profiles.items()
    }


def _route(raw: Route | str) -> Route:
    return raw if isinstance(raw, Route) else Route(str(raw).strip().lower())


def _route_slot(route: Route, raw: Any) -> str:
    slot = str(raw or "").strip().lower()
    if slot == "quick":
        slot = "fast"
    if slot not in {"fast", "pro"}:
        raise ValueError(f"invalid model slot {slot!r} for route {route.value!r}")
    return slot


@dataclass(frozen=True)
class ProviderModelTarget:
    provider: str
    model: str

    def __post_init__(self) -> None:
        provider = canonical_backend_engine(self.provider)
        if not provider or provider in {HER_V2_ENGINE, HER_V2_MIXED_VALUE}:
            raise ValueError("HER v2 target requires a concrete call provider")
        if not str(self.model or "").strip():
            raise ValueError("HER v2 target requires a model")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "model", str(self.model).strip())

    def to_dict(self) -> dict[str, str]:
        return {"provider": self.provider, "model": self.model}


def _target(
    raw: Any, *, fallback_provider: str = "", fallback_model: str = ""
) -> ProviderModelTarget:
    if isinstance(raw, ProviderModelTarget):
        return raw
    if isinstance(raw, Mapping):
        provider = str(raw.get("provider") or raw.get("engine") or fallback_provider)
        model = str(raw.get("model") or fallback_model)
        return ProviderModelTarget(provider, model)
    return ProviderModelTarget(fallback_provider, fallback_model)


@dataclass(frozen=True)
class HERv2RuntimeConfiguration:
    routing_mode: str
    provider: str
    fast_provider: str
    fast_model: str
    pro_provider: str
    pro_model: str
    profile_reasoning: Mapping[str, str | None]
    stage_reasoning: Mapping[str, str]
    route_model_slots: Mapping[str, str]
    route_reasoning: Mapping[str, str]
    route_targets: Mapping[str, ProviderModelTarget] = field(default_factory=dict)

    def __post_init__(self) -> None:
        mode = str(self.routing_mode or "single").strip().lower()
        if mode not in HER_V2_ROUTING_MODES:
            raise ValueError(f"invalid HER v2 routing mode: {mode!r}")
        fast = ProviderModelTarget(self.fast_provider, self.fast_model)
        pro = ProviderModelTarget(self.pro_provider, self.pro_model)
        if mode == "single" and fast.provider != pro.provider:
            raise ValueError(
                "single-provider mode requires one provider for Quick and Pro"
            )
        parsed_routes = {
            _route(name).value: _target(value)
            for name, value in self.route_targets.items()
        }
        if self.route_model_slots.get(Route.DIRECT.value, "fast") != "fast":
            raise ValueError("the Direct route always uses the Quick model slot")
        if Route.DIRECT.value in parsed_routes:
            raise ValueError(
                "the Direct route always uses the Quick model slot and cannot "
                "select a custom target"
            )
        object.__setattr__(self, "routing_mode", mode)
        object.__setattr__(self, "provider", _common([fast.provider, pro.provider]))
        object.__setattr__(self, "fast_provider", fast.provider)
        object.__setattr__(self, "fast_model", fast.model)
        object.__setattr__(self, "pro_provider", pro.provider)
        object.__setattr__(self, "pro_model", pro.model)
        object.__setattr__(self, "route_targets", parsed_routes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "routing_mode": self.routing_mode,
            "provider": self.provider,
            "fast_model": self.fast_model,
            "pro_model": self.pro_model,
            "targets": {
                "fast": self.target_for_slot("fast").to_dict(),
                "pro": self.target_for_slot("pro").to_dict(),
            },
            "profile_reasoning": dict(self.profile_reasoning),
            "stage_reasoning": dict(self.stage_reasoning),
            "route_model_slots": dict(self.route_model_slots),
            "route_reasoning": dict(self.route_reasoning),
            "route_targets": {
                route: target.to_dict() for route, target in self.route_targets.items()
            },
        }

    def reasoning_for_stage(self, raw: Mapping[str, Any], stage: Stage | str) -> str:
        parsed = (
            stage if isinstance(stage, Stage) else Stage(str(stage).strip().lower())
        )
        return self.reasoning_for_route(raw, DEFAULT_ROUTES_BY_STAGE[parsed])

    def model_slot_for_route(self, route: Route | str) -> str:
        parsed = _route(route)
        if parsed is Route.DIRECT:
            return "fast"
        return self.route_model_slots.get(parsed.value, "pro")

    def target_for_slot(self, slot: str) -> ProviderModelTarget:
        normalized = str(slot or "").strip().lower()
        if normalized == "quick":
            normalized = "fast"
        if normalized == "fast":
            return ProviderModelTarget(self.fast_provider, self.fast_model)
        if normalized == "pro":
            return ProviderModelTarget(self.pro_provider, self.pro_model)
        raise ValueError(f"invalid HER v2 model slot: {slot!r}")

    def target_for_route(self, route: Route | str) -> ProviderModelTarget:
        parsed = _route(route)
        custom = self.route_targets.get(parsed.value)
        if custom is not None:
            return custom
        return self.target_for_slot(self.model_slot_for_route(parsed))

    def route_target_mode(self, route: Route | str) -> str:
        parsed = _route(route)
        if parsed.value in self.route_targets:
            return "custom"
        return self.model_slot_for_route(parsed)

    def all_targets(self) -> tuple[ProviderModelTarget, ...]:
        values = [self.target_for_slot("fast"), self.target_for_slot("pro")]
        values.extend(self.route_targets.values())
        result: list[ProviderModelTarget] = []
        for target in values:
            if target not in result:
                result.append(target)
        return tuple(result)

    def reasoning_for_route(self, raw: Mapping[str, Any], route: Route | str) -> str:
        parsed = _route(route)
        override = self.route_reasoning.get(parsed.value)
        if override is not None:
            return override
        stage = ROUTE_STAGES[parsed]
        override = self.stage_reasoning.get(stage.value)
        if override is not None:
            return override
        if parsed is Route.DIRECT:
            return "high"
        role = route_profile_names(raw).get(parsed, "")
        value = self.profile_reasoning.get(role)
        return str(value) if value is not None else "default"


def resolve_her_v2_configuration(
    raw: Mapping[str, Any],
    override: Mapping[str, Any] | None = None,
) -> HERv2RuntimeConfiguration:
    profiles = raw.get("profiles")
    if not isinstance(profiles, Mapping) or not profiles:
        raise ValueError("HER v2 profiles are required")
    slots = profile_model_slots(raw)
    profile_rows = {
        str(name): value
        for name, value in profiles.items()
        if isinstance(value, Mapping)
    }
    fast_provider = _common(
        [
            str(value.get("engine") or "").strip()
            for name, value in profile_rows.items()
            if slots.get(name) == "fast"
        ]
    )
    pro_provider = _common(
        [
            str(value.get("engine") or "").strip()
            for name, value in profile_rows.items()
            if slots.get(name) == "pro"
        ]
    )
    fast_model = _common(
        [
            str(value.get("model") or "").strip()
            for name, value in profile_rows.items()
            if slots.get(name) == "fast"
        ]
    )
    pro_model = _common(
        [
            str(value.get("model") or "").strip()
            for name, value in profile_rows.items()
            if slots.get(name) == "pro"
        ]
    )
    common_provider = _common([fast_provider, pro_provider])
    fast_provider = fast_provider or pro_provider
    pro_provider = pro_provider or fast_provider
    fast_model = fast_model or pro_model
    pro_model = pro_model or fast_model
    routing_mode = str(raw.get("routing_mode") or "").strip().lower()
    explicit_routing_mode = bool(routing_mode)
    if not routing_mode:
        routing_mode = "single" if fast_provider == pro_provider else "hybrid"
    if routing_mode not in HER_V2_ROUTING_MODES:
        raise ValueError(f"invalid HER v2 routing mode: {routing_mode!r}")

    configured_slot_models = raw.get("slot_models")
    if isinstance(configured_slot_models, Mapping):
        fast_model = str(
            configured_slot_models.get("fast")
            or configured_slot_models.get("quick")
            or fast_model
        ).strip()
        pro_model = str(configured_slot_models.get("pro") or pro_model).strip()

    configured_targets = raw.get("targets")
    if isinstance(configured_targets, Mapping):
        fast = _target(
            configured_targets.get("fast") or configured_targets.get("quick"),
            fallback_provider=fast_provider,
            fallback_model=fast_model,
        )
        pro = _target(
            configured_targets.get("pro"),
            fallback_provider=pro_provider,
            fallback_model=pro_model,
        )
        fast_provider, fast_model = fast.provider, fast.model
        pro_provider, pro_model = pro.provider, pro.model
    profile_reasoning: dict[str, str | None] = {
        name: (
            str(value.get("reasoning")).strip().lower()
            if value.get("reasoning") is not None
            else None
        )
        for name, value in profile_rows.items()
    }
    configured_stage_reasoning = raw.get("stage_reasoning")
    stage_reasoning = {
        Stage(str(name).strip().lower()).value: str(value).strip().lower()
        for name, value in (
            configured_stage_reasoning.items()
            if isinstance(configured_stage_reasoning, Mapping)
            else ()
        )
        if value is not None and str(value).strip()
    }
    route_model_slots = default_route_model_slots(raw)
    configured_route_slots = raw.get("route_model_slots")
    if isinstance(configured_route_slots, Mapping):
        for name, value in configured_route_slots.items():
            route = _route(str(name))
            route_model_slots[route.value] = _route_slot(route, value)
    configured_route_reasoning = raw.get("route_reasoning")
    route_reasoning: dict[str, str] = {}
    if isinstance(configured_route_reasoning, Mapping):
        for name, value in configured_route_reasoning.items():
            normalized = _reasoning_value(value, allow_inherit=True)
            if normalized is not None:
                route_reasoning[_route(str(name)).value] = normalized
    configured_route_targets = raw.get("route_targets")
    route_targets: dict[str, ProviderModelTarget] = {}
    if isinstance(configured_route_targets, Mapping):
        for name, value in configured_route_targets.items():
            route_targets[_route(str(name)).value] = _target(value)

    if isinstance(override, Mapping):
        selected_provider = str(override.get("provider") or "").strip()
        if selected_provider and selected_provider != HER_V2_MIXED_VALUE:
            fast_provider = canonical_backend_engine(selected_provider)
            pro_provider = fast_provider
        selected_fast = str(override.get("fast_model") or "").strip()
        selected_pro = str(override.get("pro_model") or "").strip()
        if selected_fast:
            fast_model = selected_fast
        if selected_pro:
            pro_model = selected_pro
        selected_mode = str(override.get("routing_mode") or "").strip().lower()
        if selected_mode:
            if selected_mode not in HER_V2_ROUTING_MODES:
                raise ValueError(f"invalid HER v2 routing mode: {selected_mode!r}")
            routing_mode = selected_mode
            explicit_routing_mode = True
        selected_targets = override.get("targets")
        if isinstance(selected_targets, Mapping):
            fast = _target(
                selected_targets.get("fast") or selected_targets.get("quick"),
                fallback_provider=fast_provider,
                fallback_model=fast_model,
            )
            pro = _target(
                selected_targets.get("pro"),
                fallback_provider=pro_provider,
                fallback_model=pro_model,
            )
            fast_provider, fast_model = fast.provider, fast.model
            pro_provider, pro_model = pro.provider, pro.model
        selected_profiles = override.get("profile_reasoning")
        if isinstance(selected_profiles, Mapping):
            for name, value in selected_profiles.items():
                profile = str(name)
                if profile in profile_reasoning:
                    profile_reasoning[profile] = _reasoning_value(
                        value,
                        allow_inherit=True,
                    )
        selected_stages = override.get("stage_reasoning")
        if isinstance(selected_stages, Mapping):
            stage_reasoning = {}
            for name, value in selected_stages.items():
                stage = Stage(str(name).strip().lower())
                normalized = _reasoning_value(value, allow_inherit=True)
                if normalized is not None:
                    stage_reasoning[stage.value] = normalized
        selected_route_slots = override.get("route_model_slots")
        if isinstance(selected_route_slots, Mapping):
            for name, value in selected_route_slots.items():
                route = _route(str(name))
                route_model_slots[route.value] = _route_slot(route, value)
        selected_route_reasoning = override.get("route_reasoning")
        if isinstance(selected_route_reasoning, Mapping):
            route_reasoning = {}
            for name, value in selected_route_reasoning.items():
                route = _route(str(name))
                normalized = _reasoning_value(value, allow_inherit=True)
                if normalized is not None:
                    route_reasoning[route.value] = normalized
        selected_route_targets = override.get("route_targets")
        if isinstance(selected_route_targets, Mapping):
            route_targets = {
                _route(str(name)).value: _target(value)
                for name, value in selected_route_targets.items()
            }

    if not fast_provider or not pro_provider or not fast_model or not pro_model:
        raise ValueError("HER v2 Quick/Pro provider configuration is incomplete")
    if routing_mode == "single" and fast_provider != pro_provider:
        if explicit_routing_mode:
            raise ValueError(
                "single-provider mode requires one provider for Quick and Pro"
            )
        routing_mode = "hybrid"
    return HERv2RuntimeConfiguration(
        routing_mode=routing_mode,
        provider=common_provider,
        fast_provider=fast_provider,
        fast_model=fast_model,
        pro_provider=pro_provider,
        pro_model=pro_model,
        profile_reasoning=profile_reasoning,
        stage_reasoning=stage_reasoning,
        route_model_slots=route_model_slots,
        route_reasoning=route_reasoning,
        route_targets=route_targets,
    )


def _provider_engine(name: str, raw: Mapping[str, Any]) -> str:
    explicit = canonical_backend_engine(str(raw.get("engine") or "").strip())
    if explicit:
        return explicit
    normalized = str(name or "").strip().lower()
    if normalized.endswith("-api"):
        return normalized
    return f"{normalized}-api"


def _entry_models(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    values: list[Any] = []
    for row in rows:
        models = row.get("models")
        if isinstance(models, list):
            values.extend(models)
        values.extend(
            [
                row.get("default_model"),
                row.get("model"),
                row.get("fast_model"),
                row.get("pro_model"),
            ]
        )
    return [
        model
        for model in _unique_strings(values)
        if model.casefold() != "role-configured"
    ]


def build_her_v2_provider_options(
    allowed_backends: Sequence[Mapping[str, Any]],
    provider_profiles: Mapping[str, Mapping[str, Any]],
    her_v2: Mapping[str, Any],
) -> list[dict[str, Any]]:
    base = resolve_her_v2_configuration(her_v2)
    allowed_by_engine: dict[str, list[Mapping[str, Any]]] = {}
    for row in allowed_backends:
        engine = canonical_backend_engine(str(row.get("engine") or "").strip())
        if engine and engine != HER_V2_ENGINE:
            allowed_by_engine.setdefault(engine, []).append(row)

    metadata: dict[str, tuple[str, Mapping[str, Any]]] = {}
    ordered_engines: list[str] = []
    for raw_name, raw_profile in provider_profiles.items():
        if not isinstance(raw_profile, Mapping):
            continue
        name = str(raw_name).strip()
        engine = _provider_engine(name, raw_profile)
        metadata[engine] = (name, raw_profile)
        if engine not in ordered_engines:
            ordered_engines.append(engine)

    profiles = her_v2.get("profiles")
    if isinstance(profiles, Mapping):
        for value in profiles.values():
            if not isinstance(value, Mapping):
                continue
            engine = canonical_backend_engine(str(value.get("engine") or "").strip())
            if engine and engine != HER_V2_ENGINE and engine not in ordered_engines:
                ordered_engines.append(engine)

    for engine in allowed_by_engine:
        if engine not in ordered_engines:
            ordered_engines.append(engine)

    options: list[dict[str, Any]] = []
    for engine in ordered_engines:
        name, profile = metadata.get(
            engine,
            (engine.removesuffix("-api"), {}),
        )
        rows = allowed_by_engine.get(engine, [])
        row_models = _entry_models(rows)
        configured_models = _entry_models([profile])
        registry_models = get_available_models(engine)
        models = _unique_strings([*row_models, *configured_models, *registry_models])
        status = str(profile.get("status") or "stable").strip().lower()
        reason = None
        if status == "disabled":
            reason = "provider is disabled"
        elif engine not in BACKEND_REGISTRY:
            reason = "provider adapter is not installed"
        elif not models:
            reason = "provider has no configured models"

        explicit_fast = str(profile.get("fast_model") or "").strip()
        explicit_pro = str(profile.get("pro_model") or "").strip()
        for row in rows:
            explicit_fast = (
                explicit_fast or str(row.get("her_v2_fast_model") or "").strip()
            )
            explicit_pro = (
                explicit_pro or str(row.get("her_v2_pro_model") or "").strip()
            )
        if explicit_fast not in models:
            explicit_fast = ""
        if explicit_pro not in models:
            explicit_pro = ""

        fast_model = explicit_fast
        pro_model = explicit_pro
        fast_target = base.target_for_slot("fast")
        pro_target = base.target_for_slot("pro")
        if engine == fast_target.provider and fast_target.model in models:
            fast_model = fast_model or fast_target.model
        if engine == pro_target.provider and pro_target.model in models:
            pro_model = pro_model or pro_target.model
        registry_entry = get_backend_entry(engine)
        registry_default = str(registry_entry.get("default_model") or "")
        registry_fast = str(registry_entry.get("fast_model") or "")
        registry_pro = str(registry_entry.get("pro_model") or "")
        fast_model = fast_model or (
            row_models[0]
            if row_models
            else registry_fast or registry_default or (models[0] if models else "")
        )
        pro_model = pro_model or (
            row_models[-1]
            if row_models
            else registry_pro or registry_default or (models[-1] if models else "")
        )
        options.append(
            {
                "name": name,
                "engine": engine,
                "label": get_backend_label(engine),
                "status": status,
                "models": models,
                "fast_model": fast_model,
                "pro_model": pro_model,
                "available": reason is None,
                "reason": reason,
            }
        )
    return options


def select_her_v2_provider(
    current: HERv2RuntimeConfiguration,
    option: Mapping[str, Any],
) -> HERv2RuntimeConfiguration:
    if not option.get("available"):
        raise ValueError(str(option.get("reason") or "provider is unavailable"))
    return replace(
        current,
        routing_mode="single",
        provider=str(option["engine"]),
        fast_provider=str(option["engine"]),
        fast_model=str(option["fast_model"]),
        pro_provider=str(option["engine"]),
        pro_model=str(option["pro_model"]),
        route_targets={},
    )


def select_her_v2_hybrid(
    current: HERv2RuntimeConfiguration,
) -> HERv2RuntimeConfiguration:
    return replace(current, routing_mode="hybrid")


def set_her_v2_slot_model(
    current: HERv2RuntimeConfiguration,
    slot: str,
    model: str,
    *,
    provider: str | None = None,
    allowed_models: Sequence[str],
) -> HERv2RuntimeConfiguration:
    normalized_slot = str(slot or "").strip().lower()
    requested = str(model or "").strip()
    if normalized_slot not in HER_V2_MODEL_SLOTS:
        raise ValueError("HER v2 model slot must be fast or pro")
    if requested not in allowed_models:
        selected_provider = (
            provider or current.target_for_slot(normalized_slot).provider
        )
        raise ValueError(
            f"model {requested!r} is not allowed by configured provider {selected_provider!r}"
        )
    selected_provider = canonical_backend_engine(
        provider or current.target_for_slot(normalized_slot).provider
    )
    updates = {
        f"{normalized_slot}_provider": selected_provider,
        f"{normalized_slot}_model": requested,
    }
    if current.routing_mode == "single":
        other_slot = "pro" if normalized_slot == "fast" else "fast"
        other = current.target_for_slot(other_slot)
        if other.provider != selected_provider:
            raise ValueError(
                "single-provider mode cannot assign different Quick and Pro providers"
            )
    return replace(current, **updates)


def set_her_v2_route_model_slot(
    current: HERv2RuntimeConfiguration,
    route: Route | str,
    slot: str,
) -> HERv2RuntimeConfiguration:
    parsed = _route(route)
    normalized = _route_slot(parsed, slot)
    if parsed is Route.DIRECT and normalized != "fast":
        raise ValueError("the Direct route always uses the Quick model slot")
    updated = dict(current.route_model_slots)
    updated[parsed.value] = normalized
    route_targets = dict(current.route_targets)
    route_targets.pop(parsed.value, None)
    return replace(
        current,
        route_model_slots=updated,
        route_targets=route_targets,
    )


def set_her_v2_route_target(
    current: HERv2RuntimeConfiguration,
    route: Route | str,
    *,
    provider: str,
    model: str,
    allowed_models: Sequence[str],
) -> HERv2RuntimeConfiguration:
    if current.routing_mode != "hybrid":
        raise ValueError("custom task-route targets require Hybrid mode")
    parsed = _route(route)
    if parsed is Route.DIRECT:
        raise ValueError(
            "the Direct route always uses the Quick model slot and cannot select "
            "a custom target"
        )
    target = ProviderModelTarget(provider, model)
    if target.model not in allowed_models:
        raise ValueError(
            f"model {target.model!r} is not allowed by configured provider {target.provider!r}"
        )
    updated = dict(current.route_targets)
    updated[parsed.value] = target
    return replace(current, route_targets=updated)


def set_her_v2_route_reasoning(
    current: HERv2RuntimeConfiguration,
    route: Route | str,
    reasoning: str | None,
) -> HERv2RuntimeConfiguration:
    parsed = _route(route)
    normalized = _reasoning_value(reasoning, allow_inherit=True)
    updated = dict(current.route_reasoning)
    if normalized is None:
        updated.pop(parsed.value, None)
    else:
        updated[parsed.value] = normalized
    return replace(current, route_reasoning=updated)


def apply_her_v2_runtime_configuration(
    raw: Mapping[str, Any],
    selected: HERv2RuntimeConfiguration,
) -> dict[str, Any]:
    result = deepcopy(dict(raw))
    profiles = result.get("profiles")
    if not isinstance(profiles, dict):
        raise TypeError("HER v2 profiles must be an object")
    slots = profile_model_slots(result)
    for raw_name, raw_profile in profiles.items():
        if not isinstance(raw_profile, dict):
            raise TypeError(f"HER v2 profile {raw_name!r} must be an object")
        name = str(raw_name)
        slot = slots.get(name, "pro")
        target = selected.target_for_slot(slot)
        raw_profile["engine"] = target.provider
        raw_profile["model"] = target.model
        if name in selected.profile_reasoning:
            reasoning = selected.profile_reasoning[name]
            if reasoning is None:
                raw_profile.pop("reasoning", None)
            else:
                raw_profile["reasoning"] = reasoning
    if selected.stage_reasoning:
        result["stage_reasoning"] = dict(selected.stage_reasoning)
    else:
        result.pop("stage_reasoning", None)
    result["routing_mode"] = selected.routing_mode
    result["targets"] = {
        "fast": selected.target_for_slot("fast").to_dict(),
        "pro": selected.target_for_slot("pro").to_dict(),
    }
    # Keep the model-only compatibility view for older readers. New routing
    # resolves the full provider/model target above.
    result["slot_models"] = {
        "fast": selected.fast_model,
        "pro": selected.pro_model,
    }
    result["route_model_slots"] = dict(selected.route_model_slots)
    if selected.route_targets:
        result["route_targets"] = {
            route: target.to_dict() for route, target in selected.route_targets.items()
        }
    else:
        result.pop("route_targets", None)
    if selected.route_reasoning:
        result["route_reasoning"] = dict(selected.route_reasoning)
    else:
        result.pop("route_reasoning", None)
    return result
