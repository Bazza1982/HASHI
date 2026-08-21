"""Configuration and provider-role routing for HER v2."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from .models import Effort, Stage, TerminalState, TriageClassification


class HERv2ConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class ProviderProfile:
    name: str
    engine: str
    model: str
    reasoning: str | None = None
    timeout_s: float = 300.0
    max_attempts: int = 2
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
        if self.timeout_s <= 0:
            raise HERv2ConfigurationError("provider timeout must be positive")
        if self.max_attempts < 1:
            raise HERv2ConfigurationError("provider attempts must be at least one")


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
    reporting_attempts: int = 3
    structured_repair_attempts: int = 3
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
    hard_timeout_s: float = 36000.0
    audit_failure_terminal: TerminalState = TerminalState.ERROR
    meditation_enabled: bool = False
    max_subagents: int = 10
    shadow_mode: bool = False

    def __post_init__(self) -> None:
        if self.shadow_mode:
            raise HERv2ConfigurationError(
                "HER v2 shadow mode has been permanently retired; use normal mode"
            )
        if self.reporting_attempts < 1 or self.structured_repair_attempts < 1:
            raise HERv2ConfigurationError("retry counts must be at least one")
        if self.user_idle_timeout_s <= 0 or self.hard_timeout_s <= 0:
            raise HERv2ConfigurationError("timeouts must be positive")
        if self.hard_timeout_s < self.user_idle_timeout_s:
            raise HERv2ConfigurationError(
                "hard timeout must be greater than or equal to idle timeout"
            )
        if self.max_subagents < 1 or self.max_subagents > 10:
            raise HERv2ConfigurationError("max_subagents must be between one and ten")
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
        profiles_raw = raw.get("profiles")
        if not isinstance(profiles_raw, Mapping) or not profiles_raw:
            raise HERv2ConfigurationError("her_v2.profiles must be a non-empty object")
        profiles: dict[str, ProviderProfile] = {}
        for name, value in profiles_raw.items():
            if not isinstance(value, Mapping):
                raise HERv2ConfigurationError(f"profile {name!r} must be an object")
            known = {
                "engine",
                "model",
                "reasoning",
                "timeout_s",
                "max_attempts",
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
                timeout_s=float(value.get("timeout_s", 300.0)),
                max_attempts=int(value.get("max_attempts", 2)),
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
            reporting_attempts=int(raw.get("reporting_attempts", 3)),
            structured_repair_attempts=int(raw.get("structured_repair_attempts", 3)),
            replan_limits=replan_limits,
            review_limits=review_limits,
            user_idle_timeout_s=float(raw.get("user_idle_timeout_s", 1800.0)),
            hard_timeout_s=float(raw.get("hard_timeout_s", 36000.0)),
            audit_failure_terminal=_audit_failure_terminal(
                raw.get("audit_failure_terminal", "ERROR")
            ),
            meditation_enabled=_strict_bool(
                raw.get("meditation_enabled", False), "meditation_enabled"
            ),
            max_subagents=int(raw.get("max_subagents", 10)),
            shadow_mode=_strict_bool(raw.get("shadow_mode", False), "shadow_mode"),
        )

    def profile_for(self, stage: Stage) -> ProviderProfile:
        role = self.stage_roles.get(stage)
        if not role or role not in self.profiles:
            raise HERv2ConfigurationError(
                f"no configured provider profile for stage {stage.value}"
            )
        profile = self.profiles[role]
        reasoning = self.stage_reasoning.get(stage)
        return replace(profile, reasoning=reasoning) if reasoning is not None else profile

    def execution_profile_for(
        self, classification: TriageClassification
    ) -> ProviderProfile:
        preferred_role = {
            TriageClassification.SIMPLE_TASK: "lightweight",
            TriageClassification.COMPLEX_TASK: "premium",
            TriageClassification.HIGH_VOLUME_TASK: "orchestrator",
        }.get(classification)
        if preferred_role and preferred_role in self.profiles:
            profile = self.profiles[preferred_role]
            reasoning = self.stage_reasoning.get(Stage.EXECUTION)
            return replace(profile, reasoning=reasoning) if reasoning is not None else profile
        return self.profile_for(Stage.EXECUTION)


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
