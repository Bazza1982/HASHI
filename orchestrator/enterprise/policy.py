from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class PolicyDecision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    APPROVAL_REQUIRED = "approval_required"


@dataclass(frozen=True)
class PolicyEvaluation:
    decision: PolicyDecision
    reason: str | None = None

    @property
    def allowed(self) -> bool:
        return self.decision == PolicyDecision.ALLOW


def evaluate_governance_policy(action: str, context: dict | None = None) -> PolicyEvaluation:
    # Phase-0 policy contract: always allow in core implementation, with clear
    # extension point for future action-based policy injection.
    return PolicyEvaluation(
        decision=PolicyDecision.ALLOW,
        reason="default allow (no enterprise policy rules loaded)",
    )
