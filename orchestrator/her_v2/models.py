"""Canonical HER v2 value types.

These types contain no provider-specific concepts.  In particular, HER effort
is orchestration policy and is never reused as a provider reasoning value.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class StrEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class TriageClassification(StrEnum):
    DIRECT_RESPONSE = "DIRECT_RESPONSE"
    SIMPLE_TASK = "SIMPLE_TASK"
    COMPLEX_TASK = "COMPLEX_TASK"
    HIGH_VOLUME_TASK = "HIGH_VOLUME_TASK"
    CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"


WORK_CLASSIFICATIONS = frozenset(
    {
        TriageClassification.SIMPLE_TASK,
        TriageClassification.COMPLEX_TASK,
        TriageClassification.HIGH_VOLUME_TASK,
    }
)


class Effort(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


class LifecycleState(StrEnum):
    RECEIVED = "RECEIVED"
    TRIAGED = "TRIAGED"
    PLANNED = "PLANNED"
    EXECUTING = "EXECUTING"
    REPLANNING = "REPLANNING"
    EXECUTION_COMPLETED = "EXECUTION_COMPLETED"
    REVIEWING = "REVIEWING"
    FINALISING = "FINALISING"
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_LIMITATIONS = "COMPLETED_WITH_LIMITATIONS"
    FAILED = "FAILED"
    ERROR = "ERROR"
    STOPPED = "STOPPED"
    PENDING_USER_INPUT = "PENDING_USER_INPUT"


class TerminalState(StrEnum):
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_LIMITATIONS = "COMPLETED_WITH_LIMITATIONS"
    FAILED = "FAILED"
    ERROR = "ERROR"
    STOPPED = "STOPPED"
    PENDING_USER_INPUT = "PENDING_USER_INPUT"


TERMINAL_STATES = frozenset(LifecycleState(item.value) for item in TerminalState)


class Stage(StrEnum):
    IMMEDIATE_RESPONSE = "immediate_response"
    TRIAGE = "triage"
    PLANNING = "planning"
    EXECUTION = "execution"
    REPLANNING = "replanning"
    REVIEW = "review"
    FINALISATION = "finalisation"
    MEDITATION = "meditation"
    DREAM = "dream"


class Route(StrEnum):
    """User-configurable effective model/reasoning routes.

    Execution is deliberately split by triage classification because those
    paths may use different model slots even though they share one lifecycle
    stage.
    """

    IMMEDIATE_RESPONSE = "immediate_response"
    TRIAGE = "triage"
    PLANNING = "planning"
    EXECUTION_SIMPLE = "execution_simple"
    EXECUTION_COMPLEX = "execution_complex"
    EXECUTION_HIGH_VOLUME = "execution_high_volume"
    REPLANNING = "replanning"
    REVIEW = "review"
    FINALISATION = "finalisation"
    MEDITATION = "meditation"
    DREAM = "dream"


ROUTE_STAGES: Mapping[Route, Stage] = {
    Route.IMMEDIATE_RESPONSE: Stage.IMMEDIATE_RESPONSE,
    Route.TRIAGE: Stage.TRIAGE,
    Route.PLANNING: Stage.PLANNING,
    Route.EXECUTION_SIMPLE: Stage.EXECUTION,
    Route.EXECUTION_COMPLEX: Stage.EXECUTION,
    Route.EXECUTION_HIGH_VOLUME: Stage.EXECUTION,
    Route.REPLANNING: Stage.REPLANNING,
    Route.REVIEW: Stage.REVIEW,
    Route.FINALISATION: Stage.FINALISATION,
    Route.MEDITATION: Stage.MEDITATION,
    Route.DREAM: Stage.DREAM,
}


DEFAULT_ROUTES_BY_STAGE: Mapping[Stage, Route] = {
    Stage.IMMEDIATE_RESPONSE: Route.IMMEDIATE_RESPONSE,
    Stage.TRIAGE: Route.TRIAGE,
    Stage.PLANNING: Route.PLANNING,
    Stage.EXECUTION: Route.EXECUTION_COMPLEX,
    Stage.REPLANNING: Route.REPLANNING,
    Stage.REVIEW: Route.REVIEW,
    Stage.FINALISATION: Route.FINALISATION,
    Stage.MEDITATION: Route.MEDITATION,
    Stage.DREAM: Route.DREAM,
}


EXECUTION_ROUTES: Mapping[TriageClassification, Route] = {
    TriageClassification.SIMPLE_TASK: Route.EXECUTION_SIMPLE,
    TriageClassification.COMPLEX_TASK: Route.EXECUTION_COMPLEX,
    TriageClassification.HIGH_VOLUME_TASK: Route.EXECUTION_HIGH_VOLUME,
}


class ReviewOutcome(StrEnum):
    PASS = "PASS"
    CONDITIONAL_PASS = "CONDITIONAL_PASS"
    FAIL = "FAIL"


class ExecutionDisposition(StrEnum):
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_LIMITATIONS = "COMPLETED_WITH_LIMITATIONS"
    FAILED = "FAILED"
    USER_INPUT_REQUIRED = "USER_INPUT_REQUIRED"


@dataclass(frozen=True)
class StageRequest:
    turn_id: str
    request_ref: str
    stage: Stage
    role: str
    attempt: int
    goal: str
    classification: TriageClassification | None
    effort: Effort
    plan_id: str | None = None
    context: Mapping[str, Any] = field(default_factory=dict)
    allow_tools: bool = False
    allow_side_effects: bool = False
    invocation_id: str = ""
    retry_invariant_hash: str = ""
    progress_callback: Callable[[str, str, bool], None] | None = field(
        default=None, compare=False, repr=False
    )
    provider_activity_callback: Callable[[Mapping[str, Any]], None] | None = field(
        default=None, compare=False, repr=False
    )


@dataclass(frozen=True)
class StageResponse:
    text: str = ""
    data: Mapping[str, Any] = field(default_factory=dict)
    reasoning_trace: str | None = None
    provider: str = ""
    model: str = ""
    usage: Mapping[str, int] = field(default_factory=dict)
    evidence_refs: tuple[str, ...] = ()
    provider_attempt: int = 1


@dataclass(frozen=True)
class TriageDecision:
    classification: TriageClassification
    goal: str
    clarification: str = ""


@dataclass(frozen=True)
class ExecutionOutcome:
    disposition: ExecutionDisposition
    summary: str
    evidence_refs: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    clarification: str = ""
    work_performed: tuple[str, ...] = ()
    verification: tuple[str, ...] = ()
    remaining_work: tuple[str, ...] = ()


@dataclass(frozen=True)
class FinalisationOutcome:
    """Canonical ledger payload plus the Persona-rendered user message.

    ``execution_result_present`` distinguishes the Plan B ``null`` result from
    the legacy report-only envelope accepted during rolling upgrades.
    """

    execution_result: ExecutionOutcome | None
    final_message: str
    execution_result_present: bool = True


@dataclass(frozen=True)
class ReviewFinding:
    outcome: ReviewOutcome
    summary: str
    findings: tuple[str, ...] = ()


@dataclass(frozen=True)
class SubAgentAssignment:
    assignment_id: str
    task: str
    profile: str
    tools: tuple[str, ...] = ()
    allow_side_effects: bool = False


@dataclass(frozen=True)
class SubAgentResult:
    assignment_id: str
    disposition: ExecutionDisposition
    summary: str
    evidence_refs: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()


@dataclass(frozen=True)
class DeliveryRecord:
    kind: str
    text: str
    event_id: str


@dataclass(frozen=True)
class TurnResult:
    turn_id: str
    terminal_state: TerminalState
    text: str
    classification: TriageClassification | None
    ledger: Mapping[str, Any]
    delivery_records: tuple[DeliveryRecord, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    error: str = ""
    final_was_immediate: bool = False
    final_already_delivered: bool = False
    delivery_id: str = ""
    delivery_kind: str = ""
    delivery_event_id: str = ""
    review_count: int = 0
    replan_count: int = 0


def terminal_lifecycle(state: TerminalState) -> LifecycleState:
    return LifecycleState(state.value)


def as_terminal(state: LifecycleState) -> TerminalState:
    if state not in TERMINAL_STATES:
        raise ValueError(f"{state.value} is not terminal")
    return TerminalState(state.value)
