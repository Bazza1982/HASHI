"""Configuration and provider-role routing for HER v2."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from .models import (
    DEFAULT_ROUTES_BY_STAGE,
    EXECUTION_ROUTES,
    ROUTE_STAGES,
    Effort,
    Route,
    Stage,
    TerminalState,
    TriageClassification,
)


class HERv2ConfigurationError(ValueError):
    pass


REMOVED_HER_V2_LIMIT_FIELDS = frozenset(
    {
        "absolute_timeout_s",
        "attempt_limit",
        "deadline_s",
        "execution_timeout_s",
        "hard_timeout_sec",
        "hard_timeout_s",
        "max_attempts",
        "max_calls",
        "max_completion_tokens",
        "max_iterations",
        "max_loops",
        "max_new_tokens",
        "max_output_tokens",
        "max_retries",
        "max_rounds",
        "max_steps",
        "max_subagents",
        "max_tokens",
        "max_tool_iterations",
        "max_turns",
        "output_token_limit",
        "process_timeout",
        "reporting_attempts",
        "request_timeout_s",
        "retries",
        "retry_attempts",
        "retry_limit",
        "stage_timeout_sec",
        "stage_timeout_s",
        "structured_repair_attempts",
        "timeout",
        "timeout_seconds",
        "timeout_s",
        "time_budget_s",
        "total_timeout_s",
        "token_budget",
        "turn_limit",
        "wall_clock_timeout_s",
    }
)

REMOVED_HER_V2_PROFILE_LIMIT_FIELDS = frozenset(
    REMOVED_HER_V2_LIMIT_FIELDS
)


def _reject_removed_limits(
    raw: Mapping[str, Any],
    *,
    fields: frozenset[str],
    location: str,
) -> None:
    found: set[str] = set()

    def visit(value: Any, prefix: str = "") -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                name = str(key)
                path = f"{prefix}.{name}" if prefix else name
                if name in fields:
                    found.add(path)
                visit(item, path)
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                visit(item, f"{prefix}[{index}]")

    visit(raw)
    if not found:
        return
    raise HERv2ConfigurationError(
        f"{location} contains removed execution limit field(s): "
        f"{', '.join(sorted(found))}. HER v2 permits only idle-progress liveness "
        "detection plus the explicitly designed Replan and Review limits; "
        "legacy HER ceilings must not be applied."
    )


@dataclass(frozen=True)
class ProviderProfile:
    name: str
    engine: str
    model: str
    reasoning: str | None = None
    options: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise HERv2ConfigurationError("provider profile name is required")
        if not self.engine.strip() or self.engine in {"her", "claw-cli", "her-v2"}:
            raise HERv2ConfigurationError(
                f"profile {self.name!r} must select a non-HER provider engine"
            )
        if not self.model.strip():
            raise HERv2ConfigurationError(
                f"profile {self.name!r} must select a model"
            )
        _reject_removed_limits(
            self.options,
            fields=REMOVED_HER_V2_PROFILE_LIMIT_FIELDS,
            location=f"her_v2.profiles.{self.name}.options",
        )


DEFAULT_STAGE_ROLES: Mapping[Stage, str] = {
    Stage.IMMEDIATE_RESPONSE: "lightweight",
    Stage.TRIAGE: "triage",
    Stage.PLANNING: "premium",
    Stage.EXECUTION: "premium",
    Stage.STRUCTURE_REPAIR: "premium",
    Stage.REPLANNING: "premium",
    Stage.REVIEW: "reviewer",
    Stage.FINALISATION: "premium",
    Stage.MEDITATION: "lightweight",
    Stage.DREAM: "lightweight",
}


@dataclass(frozen=True)
class HERv2Config:
    profiles: Mapping[str, ProviderProfile]
    stage_roles: Mapping[Stage, str]
    stage_reasoning: Mapping[Stage, str] = field(default_factory=dict)
    slot_models: Mapping[str, str] = field(default_factory=dict)
    route_model_slots: Mapping[Route, str] = field(default_factory=dict)
    route_reasoning: Mapping[Route, str] = field(default_factory=dict)
    replan_limits: Mapping[Effort, int] = field(
        default_factory=lambda: {
            Effort.LOW: 0,
            Effort.MEDIUM: 0,
            Effort.HIGH: 50,
            Effort.XHIGH: 100,
            Effort.MAX: 200,
        }
    )
    review_limits: Mapping[Effort, int] = field(
        default_factory=lambda: {
            Effort.LOW: 0,
            Effort.MEDIUM: 0,
            Effort.HIGH: 0,
            Effort.XHIGH: 1,
            Effort.MAX: 3,
        }
    )
    user_idle_timeout_s: float = 1800.0
    audit_failure_terminal: TerminalState = TerminalState.ERROR
    meditation_enabled: bool = False
    shadow_mode: bool = False

    def __post_init__(self) -> None:
        if self.shadow_mode:
            raise HERv2ConfigurationError(
                "HER v2 shadow mode has been permanently retired; use normal mode"
            )
        if self.user_idle_timeout_s <= 0:
            raise HERv2ConfigurationError("idle-progress timeout must be positive")
        if self.audit_failure_terminal not in {
            TerminalState.ERROR,
            TerminalState.STOPPED,
        }:
            raise HERv2ConfigurationError(
                "audit_failure_terminal must be ERROR or STOPPED"
            )
        missing = {
            role
            for stage, role in self.stage_roles.items()
            if stage not in {Stage.MEDITATION, Stage.DREAM}
            and role not in self.profiles
        }
        if missing:
            raise HERv2ConfigurationError(
                f"stage roles reference missing profiles: {', '.join(sorted(missing))}"
            )

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> HERv2Config:
        _reject_removed_limits(
            raw,
            fields=REMOVED_HER_V2_LIMIT_FIELDS,
            location="her_v2",
        )
        profiles_raw = raw.get("profiles")
        if not isinstance(profiles_raw, Mapping) or not profiles_raw:
            raise HERv2ConfigurationError("her_v2.profiles must be a non-empty object")
        profiles: dict[str, ProviderProfile] = {}
        for name, value in profiles_raw.items():
            if not isinstance(value, Mapping):
                raise HERv2ConfigurationError(f"profile {name!r} must be an object")
            _reject_removed_limits(
                value,
                fields=REMOVED_HER_V2_PROFILE_LIMIT_FIELDS,
                location=f"her_v2.profiles.{name}",
            )
            known = {
                "engine",
                "model",
                "reasoning",
            }
            profiles[str(name)] = ProviderProfile(
                name=str(name),
                engine=str(value.get("engine") or "").strip(),
                model=str(value.get("model") or "").strip(),
                reasoning=(
                    str(value.get("reasoning")).strip()
                    if value.get("reasoning") is not None
                    else None
                ),
                options={key: item for key, item in value.items() if key not in known},
            )

        roles_raw = raw.get("stage_roles") or {}
        if not isinstance(roles_raw, Mapping):
            raise HERv2ConfigurationError("her_v2.stage_roles must be an object")
        stage_roles = dict(DEFAULT_STAGE_ROLES)
        for stage_name, role in roles_raw.items():
            try:
                stage = Stage(str(stage_name).strip().lower())
            except ValueError as exc:
                raise HERv2ConfigurationError(
                    f"unknown HER v2 stage role: {stage_name!r}"
                ) from exc
            stage_roles[stage] = str(role).strip()

        stage_reasoning_raw = raw.get("stage_reasoning") or {}
        if not isinstance(stage_reasoning_raw, Mapping):
            raise HERv2ConfigurationError("her_v2.stage_reasoning must be an object")
        stage_reasoning: dict[Stage, str] = {}
        for stage_name, value in stage_reasoning_raw.items():
            try:
                stage = Stage(str(stage_name).strip().lower())
            except ValueError as exc:
                raise HERv2ConfigurationError(
                    f"unknown HER v2 reasoning stage: {stage_name!r}"
                ) from exc
            reasoning = str(value or "").strip()
            if not reasoning:
                raise HERv2ConfigurationError(
                    f"provider reasoning for stage {stage.value!r} must be non-empty"
                )
            stage_reasoning[stage] = reasoning

        slot_models_raw = raw.get("slot_models") or {}
        if not isinstance(slot_models_raw, Mapping):
            raise HERv2ConfigurationError("her_v2.slot_models must be an object")
        slot_models: dict[str, str] = {}
        for raw_slot, value in slot_models_raw.items():
            slot = str(raw_slot).strip().lower()
            if slot == "quick":
                slot = "fast"
            if slot not in {"fast", "pro"}:
                raise HERv2ConfigurationError(
                    f"unknown HER v2 model slot: {raw_slot!r}"
                )
            model = str(value or "").strip()
            if not model:
                raise HERv2ConfigurationError(
                    f"model for slot {slot!r} must be non-empty"
                )
            slot_models[slot] = model

        route_model_slots_raw = raw.get("route_model_slots") or {}
        if not isinstance(route_model_slots_raw, Mapping):
            raise HERv2ConfigurationError(
                "her_v2.route_model_slots must be an object"
            )
        route_model_slots: dict[Route, str] = {}
        for route_name, value in route_model_slots_raw.items():
            try:
                route = Route(str(route_name).strip().lower())
            except ValueError as exc:
                raise HERv2ConfigurationError(
                    f"unknown HER v2 route model slot: {route_name!r}"
                ) from exc
            slot = str(value or "").strip().lower()
            if slot == "quick":
                slot = "fast"
            allowed_slots = {"fast", "pro"}
            if route is Route.STRUCTURE_REPAIR:
                allowed_slots.add("inherit")
            if slot not in allowed_slots:
                raise HERv2ConfigurationError(
                    f"invalid model slot {slot!r} for route {route.value!r}"
                )
            route_model_slots[route] = slot
        for route, slot in route_model_slots.items():
            if slot != "inherit" and slot not in slot_models:
                raise HERv2ConfigurationError(
                    f"route {route.value!r} selects undefined model slot {slot!r}"
                )

        route_reasoning_raw = raw.get("route_reasoning") or {}
        if not isinstance(route_reasoning_raw, Mapping):
            raise HERv2ConfigurationError("her_v2.route_reasoning must be an object")
        route_reasoning: dict[Route, str] = {}
        for route_name, value in route_reasoning_raw.items():
            try:
                route = Route(str(route_name).strip().lower())
            except ValueError as exc:
                raise HERv2ConfigurationError(
                    f"unknown HER v2 reasoning route: {route_name!r}"
                ) from exc
            reasoning = str(value or "").strip()
            if not reasoning:
                raise HERv2ConfigurationError(
                    f"provider reasoning for route {route.value!r} must be non-empty"
                )
            route_reasoning[route] = reasoning

        replan_limits = _effort_int_map(
            raw.get("replan_limits"),
            {Effort.LOW: 0, Effort.MEDIUM: 0, Effort.HIGH: 50, Effort.XHIGH: 100, Effort.MAX: 200},
        )
        review_limits = _effort_int_map(
            raw.get("review_limits"),
            {Effort.LOW: 0, Effort.MEDIUM: 0, Effort.HIGH: 0, Effort.XHIGH: 1, Effort.MAX: 3},
        )
        return cls(
            profiles=profiles,
            stage_roles=stage_roles,
            stage_reasoning=stage_reasoning,
            slot_models=slot_models,
            route_model_slots=route_model_slots,
            route_reasoning=route_reasoning,
            replan_limits=replan_limits,
            review_limits=review_limits,
            user_idle_timeout_s=float(raw.get("user_idle_timeout_s", 1800.0)),
            audit_failure_terminal=_audit_failure_terminal(
                raw.get("audit_failure_terminal", "ERROR")
            ),
            meditation_enabled=_strict_bool(
                raw.get("meditation_enabled", False), "meditation_enabled"
            ),
            shadow_mode=_strict_bool(raw.get("shadow_mode", False), "shadow_mode"),
        )

    def _configured_route_profile(
        self,
        profile: ProviderProfile,
        route: Route,
    ) -> ProviderProfile:
        stage = ROUTE_STAGES[route]
        model = profile.model
        slot = self.route_model_slots.get(route)
        if slot in {"fast", "pro"} and self.slot_models.get(slot):
            model = self.slot_models[slot]
        reasoning = self.route_reasoning.get(route)
        if reasoning is None:
            reasoning = self.stage_reasoning.get(stage, profile.reasoning)
        return replace(profile, model=model, reasoning=reasoning)

    def profile_for_route(
        self,
        route: Route | str,
        *,
        base_profile: ProviderProfile | None = None,
    ) -> ProviderProfile:
        parsed = (
            route
            if isinstance(route, Route)
            else Route(str(route).strip().lower())
        )
        stage = ROUTE_STAGES[parsed]
        profile = base_profile
        if profile is None:
            if parsed in {
                Route.EXECUTION_SIMPLE,
                Route.EXECUTION_COMPLEX,
                Route.EXECUTION_HIGH_VOLUME,
            }:
                preferred_role = {
                    Route.EXECUTION_SIMPLE: "lightweight",
                    Route.EXECUTION_COMPLEX: "premium",
                    Route.EXECUTION_HIGH_VOLUME: "orchestrator",
                }[parsed]
                role = (
                    preferred_role
                    if preferred_role in self.profiles
                    else self.stage_roles.get(stage)
                )
            else:
                role = self.stage_roles.get(stage)
            if not role or role not in self.profiles:
                raise HERv2ConfigurationError(
                    f"no configured provider profile for route {parsed.value}"
                )
            profile = self.profiles[role]
        return self._configured_route_profile(profile, parsed)

    def profile_for(self, stage: Stage) -> ProviderProfile:
        role = self.stage_roles.get(stage)
        if not role or role not in self.profiles:
            raise HERv2ConfigurationError(
                f"no configured provider profile for stage {stage.value}"
            )
        return self._configured_route_profile(
            self.profiles[role],
            DEFAULT_ROUTES_BY_STAGE[stage],
        )

    def execution_profile_for(
        self, classification: TriageClassification
    ) -> ProviderProfile:
        return self.profile_for_route(
            EXECUTION_ROUTES.get(classification, Route.EXECUTION_COMPLEX)
        )


def _effort_int_map(
    raw: Any, defaults: Mapping[Effort, int]
) -> Mapping[Effort, int]:
    result = dict(defaults)
    if raw is None:
        return result
    if not isinstance(raw, Mapping):
        raise HERv2ConfigurationError("effort limit configuration must be an object")
    for key, value in raw.items():
        try:
            effort = Effort(str(key).strip().lower())
        except ValueError as exc:
            raise HERv2ConfigurationError(f"unknown HER effort: {key!r}") from exc
        parsed = int(value)
        if parsed < 0:
            raise HERv2ConfigurationError("effort limits cannot be negative")
        result[effort] = parsed
    return result


def _strict_bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise HERv2ConfigurationError(f"{name} must be a JSON boolean")
    return value


def _audit_failure_terminal(value: Any) -> TerminalState:
    try:
        terminal = TerminalState(str(value or "ERROR").strip().upper())
    except ValueError as exc:
        raise HERv2ConfigurationError(
            "audit_failure_terminal must be ERROR or STOPPED"
        ) from exc
    if terminal not in {TerminalState.ERROR, TerminalState.STOPPED}:
        raise HERv2ConfigurationError(
            "audit_failure_terminal must be ERROR or STOPPED"
        )
    return terminal
