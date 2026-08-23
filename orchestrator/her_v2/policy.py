"""HER effort policy and truthful terminal-state selection."""

from __future__ import annotations

from dataclasses import dataclass

from .models import (
    Effort,
    ExecutionDisposition,
    TerminalState,
    TriageClassification,
)


@dataclass(frozen=True)
class EffortPolicy:
    planning: bool
    replanning: bool
    review: bool
    assurance: bool
    max_replans: int
    max_reviews: int
    max_verifications: int


def resolve_policy(
    effort: Effort,
    *,
    replan_limit: int,
    review_limit: int,
    verification_limit: int = 0,
) -> EffortPolicy:
    return EffortPolicy(
        planning=effort is not Effort.LOW,
        replanning=effort in {Effort.HIGH, Effort.XHIGH, Effort.MAX},
        review=(effort in {Effort.XHIGH, Effort.MAX} and int(review_limit) > 0),
        assurance=effort is Effort.MAX and int(verification_limit) > 0,
        max_replans=max(0, int(replan_limit)),
        max_reviews=max(0, int(review_limit)),
        max_verifications=min(3, max(0, int(verification_limit))),
    )


def replan_eligible(
    classification: TriageClassification, policy: EffortPolicy
) -> bool:
    # Classification describes the task and remains immutable.  Replanning is
    # an orchestration capability selected by effort, not a second classifier.
    del classification
    return policy.replanning


def terminal_for_execution(
    disposition: ExecutionDisposition,
) -> TerminalState:
    if disposition is ExecutionDisposition.FAILED:
        return TerminalState.FAILED
    if disposition is ExecutionDisposition.USER_INPUT_REQUIRED:
        return TerminalState.PENDING_USER_INPUT
    if disposition is ExecutionDisposition.COMPLETED_WITH_LIMITATIONS:
        return TerminalState.COMPLETED_WITH_LIMITATIONS
    return TerminalState.COMPLETED
