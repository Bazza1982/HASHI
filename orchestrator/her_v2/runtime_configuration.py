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
    HER_V2_ENGINE,
    canonical_backend_engine,
    get_backend_label,
)

from .config import DEFAULT_STAGE_ROLES
from .models import Stage

HER_V2_CONFIGURATION_STATE_KEY = "her_v2_configuration"
HER_V2_MODEL_SLOTS = ("fast", "pro")
HER_V2_MIXED_VALUE = "mixed"
HER_V2_STANDARD_REASONING = ("off", "low", "medium", "high", "xhigh", "max")

_FAST_STAGES = frozenset(
    {
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


@dataclass(frozen=True)
class HERv2RuntimeConfiguration:
    provider: str
    fast_model: str
    pro_model: str
    profile_reasoning: Mapping[str, str | None]
    stage_reasoning: Mapping[str, str]
    _profile_slots: Mapping[str, str] = field(
        default_factory=dict,
        compare=False,
        repr=False,
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "fast_model": self.fast_model,
            "pro_model": self.pro_model,
            "profile_reasoning": dict(self.profile_reasoning),
            "stage_reasoning": dict(self.stage_reasoning),
        }

    def slot_reasoning(self, slot: str) -> str:
        values = [
            (
                "default"
                if self.profile_reasoning.get(profile) is None
                else str(self.profile_reasoning[profile])
            )
            for profile, assigned in self._profile_slots.items()
            if assigned == slot
        ]
        return _common(values) or "default"

    def reasoning_for_stage(self, raw: Mapping[str, Any], stage: Stage | str) -> str:
        parsed = (
            stage if isinstance(stage, Stage) else Stage(str(stage).strip().lower())
        )
        override = self.stage_reasoning.get(parsed.value)
        if override is not None:
            return override
        role = _stage_roles(raw).get(parsed, "")
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
    provider = _common(
        [str(value.get("engine") or "").strip() for value in profile_rows.values()]
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

    if isinstance(override, Mapping):
        selected_provider = str(override.get("provider") or "").strip()
        if selected_provider:
            provider = canonical_backend_engine(selected_provider)
        selected_fast = str(override.get("fast_model") or "").strip()
        selected_pro = str(override.get("pro_model") or "").strip()
        if selected_fast:
            fast_model = selected_fast
        if selected_pro:
            pro_model = selected_pro
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

    if not provider or not fast_model or not pro_model:
        raise ValueError("HER v2 Fast/Pro provider configuration is incomplete")
    return HERv2RuntimeConfiguration(
        provider=provider,
        fast_model=fast_model,
        pro_model=pro_model,
        profile_reasoning=profile_reasoning,
        stage_reasoning=stage_reasoning,
        _profile_slots=slots,
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
        values.extend([row.get("default_model"), row.get("model")])
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
        if engine.endswith("-api") and engine not in ordered_engines:
            ordered_engines.append(engine)

    options: list[dict[str, Any]] = []
    for engine in ordered_engines:
        name, profile = metadata.get(
            engine,
            (engine.removesuffix("-api"), {}),
        )
        rows = allowed_by_engine.get(engine, [])
        models = _entry_models(rows)
        status = str(profile.get("status") or "stable").strip().lower()
        reason = None
        if status == "disabled":
            reason = "provider is disabled"
        elif not rows:
            reason = "provider is not allowed for this agent"
        elif not models:
            reason = "no models are allowed for this agent"

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
        if engine == base.provider:
            if base.fast_model in models and not fast_model:
                fast_model = base.fast_model
            if base.pro_model in models and not pro_model:
                pro_model = base.pro_model
        fast_model = fast_model or (models[0] if models else "")
        pro_model = pro_model or (models[-1] if models else "")
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
        provider=str(option["engine"]),
        fast_model=str(option["fast_model"]),
        pro_model=str(option["pro_model"]),
    )


def set_her_v2_slot_model(
    current: HERv2RuntimeConfiguration,
    slot: str,
    model: str,
    *,
    allowed_models: Sequence[str],
) -> HERv2RuntimeConfiguration:
    normalized_slot = str(slot or "").strip().lower()
    requested = str(model or "").strip()
    if normalized_slot not in HER_V2_MODEL_SLOTS:
        raise ValueError("HER v2 model slot must be fast or pro")
    if requested not in allowed_models:
        raise ValueError(
            f"model {requested!r} is not allowed for provider {current.provider!r}"
        )
    return replace(current, **{f"{normalized_slot}_model": requested})


def set_her_v2_reasoning(
    current: HERv2RuntimeConfiguration,
    target: str,
    reasoning: str | None,
) -> HERv2RuntimeConfiguration:
    normalized_target = str(target or "").strip().lower()
    normalized = _reasoning_value(reasoning, allow_inherit=True)
    if normalized_target in HER_V2_MODEL_SLOTS:
        updated = dict(current.profile_reasoning)
        for profile, slot in current._profile_slots.items():
            if slot == normalized_target:
                updated[profile] = normalized
        return replace(current, profile_reasoning=updated)

    try:
        stage = Stage(normalized_target)
    except ValueError as exc:
        raise ValueError(f"unknown HER v2 reasoning target: {target}") from exc
    updated_stages = dict(current.stage_reasoning)
    if normalized is None:
        updated_stages.pop(stage.value, None)
    else:
        updated_stages[stage.value] = normalized
    return replace(current, stage_reasoning=updated_stages)


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
        if selected.provider != HER_V2_MIXED_VALUE:
            raw_profile["engine"] = selected.provider
        slot = slots.get(name, "pro")
        model = selected.fast_model if slot == "fast" else selected.pro_model
        if model != HER_V2_MIXED_VALUE:
            raw_profile["model"] = model
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
    return result
