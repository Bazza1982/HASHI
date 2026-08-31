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
    direct: bool
    planning: bool
    replanning: bool
    review: bool
    strategy_tools: bool
    planning_tools: bool
    max_reviews: int


def resolve_policy(
    effort: Effort,
    *,
    review_limit: int,
) -> EffortPolicy:
    return EffortPolicy(
        direct=effort is Effort.ZERO,
        planning=effort not in {Effort.ZERO, Effort.LOW},
        replanning=effort in {Effort.HIGH, Effort.XHIGH, Effort.MAX},
        review=(effort in {Effort.XHIGH, Effort.MAX} and int(review_limit) > 0),
        # The measured Planned/Medium path keeps Strategy abstract and gives
        # Planning the read-only investigation surface closest to Execution.
        # Other effort tiers retain their existing tool ownership until they
        # are evaluated independently.
        strategy_tools=effort not in {Effort.ZERO, Effort.MEDIUM},
        planning_tools=effort is Effort.MEDIUM,
        max_reviews=max(0, int(review_limit)),
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
