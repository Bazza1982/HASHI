"""HER effort policy and truthful terminal-state selection."""

from __future__ import annotations

from dataclasses import dataclass

from .models import (
    Effort,
    ExecutionDisposition,
    ReviewOutcome,
    TerminalState,
    TriageClassification,
)


@dataclass(frozen=True)
class EffortPolicy:
    planning: bool
    replanning: bool
    review: bool
    max_replans: int
    max_reviews: int


def resolve_policy(
    effort: Effort,
    *,
    replan_limit: int,
    review_limit: int,
) -> EffortPolicy:
    return EffortPolicy(
        planning=effort is not Effort.LOW,
        replanning=effort in {Effort.HIGH, Effort.XHIGH, Effort.MAX},
        review=effort in {Effort.XHIGH, Effort.MAX},
        max_replans=max(0, int(replan_limit)),
        max_reviews=max(0, int(review_limit)),
    )


def replan_eligible(
    classification: TriageClassification, policy: EffortPolicy
) -> bool:
    return policy.replanning and classification in {
        TriageClassification.COMPLEX_TASK,
        TriageClassification.HIGH_VOLUME_TASK,
    }


def terminal_for_execution(
    disposition: ExecutionDisposition,
    *,
    review_outcome: ReviewOutcome | None = None,
    material_limitations: bool = False,
) -> TerminalState:
    if disposition is ExecutionDisposition.FAILED:
        return TerminalState.FAILED
    if disposition is ExecutionDisposition.ABANDONED:
        return TerminalState.ABANDONED
    if disposition is ExecutionDisposition.REPLAN_REQUIRED:
        return TerminalState.COMPLETED_WITH_LIMITATIONS
    if (
        disposition is ExecutionDisposition.COMPLETED_WITH_LIMITATIONS
        or material_limitations
        or review_outcome in {ReviewOutcome.CONDITIONAL_PASS, ReviewOutcome.FAIL}
    ):
        return TerminalState.COMPLETED_WITH_LIMITATIONS
    return TerminalState.COMPLETED
