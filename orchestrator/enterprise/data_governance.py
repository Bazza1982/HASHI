from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Iterable


class DataClassification(str, Enum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


class DataEgressDecision(str, Enum):
    ALLOW = "allow"
    APPROVAL_REQUIRED = "approval_required"
    DENY = "deny"


_CLASSIFICATION_RANK = {
    DataClassification.PUBLIC: 0,
    DataClassification.INTERNAL: 1,
    DataClassification.CONFIDENTIAL: 2,
    DataClassification.RESTRICTED: 3,
}


@dataclass(frozen=True)
class DataFinding:
    kind: str
    classification: DataClassification
    snippet: str

    def to_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "classification": self.classification.value,
            "snippet": self.snippet,
        }


@dataclass(frozen=True)
class DataGovernancePolicy:
    max_auto_egress: DataClassification = DataClassification.INTERNAL
    max_approval_egress: DataClassification = DataClassification.CONFIDENTIAL
    allowed_residency_regions: tuple[str, ...] = ()


@dataclass(frozen=True)
class DataGovernanceAssessment:
    classification: DataClassification
    findings: tuple[DataFinding, ...]
    decision: DataEgressDecision
    reason: str
    destination_region: str | None = None

    def to_dict(self) -> dict:
        return {
            "classification": self.classification.value,
            "findings": [finding.to_dict() for finding in self.findings],
            "decision": self.decision.value,
            "reason": self.reason,
            "destination_region": self.destination_region,
        }


_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"\b(api[_-]?key|token|secret|password|client[_-]?secret)\s*[:=]\s*([^\s,;]{8,})",
    re.IGNORECASE,
)
_PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_CREDIT_CARD_RE = re.compile(r"\b(?:\d[ -]*?){13,19}\b")


def classify_text(value: object) -> tuple[DataClassification, tuple[DataFinding, ...]]:
    text = str(value or "")
    findings: list[DataFinding] = []

    for match in _EMAIL_RE.finditer(text):
        findings.append(
            DataFinding(
                kind="email_address",
                classification=DataClassification.CONFIDENTIAL,
                snippet=_mask_email(match.group(0)),
            )
        )

    for match in _SECRET_ASSIGNMENT_RE.finditer(text):
        findings.append(
            DataFinding(
                kind="secret_assignment",
                classification=DataClassification.RESTRICTED,
                snippet=f"{match.group(1)}=[REDACTED]",
            )
        )

    if _PRIVATE_KEY_RE.search(text):
        findings.append(
            DataFinding(
                kind="private_key",
                classification=DataClassification.RESTRICTED,
                snippet="-----BEGIN [REDACTED] PRIVATE KEY-----",
            )
        )

    for match in _CREDIT_CARD_RE.finditer(text):
        digits = re.sub(r"\D", "", match.group(0))
        if _passes_luhn(digits):
            findings.append(
                DataFinding(
                    kind="payment_card",
                    classification=DataClassification.RESTRICTED,
                    snippet=f"****{digits[-4:]}",
                )
            )

    if findings:
        return _max_classification(finding.classification for finding in findings), tuple(findings)
    if text.strip():
        return DataClassification.INTERNAL, ()
    return DataClassification.PUBLIC, ()


def assess_data_egress(
    value: object,
    *,
    policy: DataGovernancePolicy | None = None,
    destination_region: str | None = None,
) -> DataGovernanceAssessment:
    policy = policy or DataGovernancePolicy()
    classification, findings = classify_text(value)
    normalized_region = _normalize_region(destination_region)
    if policy.allowed_residency_regions and normalized_region not in policy.allowed_residency_regions:
        return DataGovernanceAssessment(
            classification=classification,
            findings=findings,
            decision=DataEgressDecision.DENY,
            reason="destination_region_not_allowed",
            destination_region=normalized_region,
        )
    if _rank(classification) <= _rank(policy.max_auto_egress):
        decision = DataEgressDecision.ALLOW
        reason = "classification_within_auto_egress"
    elif _rank(classification) <= _rank(policy.max_approval_egress):
        decision = DataEgressDecision.APPROVAL_REQUIRED
        reason = "classification_requires_approval"
    else:
        decision = DataEgressDecision.DENY
        reason = "classification_exceeds_approval_threshold"
    return DataGovernanceAssessment(
        classification=classification,
        findings=findings,
        decision=decision,
        reason=reason,
        destination_region=normalized_region,
    )


def _max_classification(values: Iterable[DataClassification]) -> DataClassification:
    return max(values, key=_rank)


def _rank(value: DataClassification) -> int:
    return _CLASSIFICATION_RANK[DataClassification(value)]


def _normalize_region(value: str | None) -> str | None:
    normalized = str(value or "").strip().lower()
    return normalized or None


def _mask_email(value: str) -> str:
    local, _, domain = value.partition("@")
    if not local:
        return f"[REDACTED]@{domain}"
    return f"{local[0]}***@{domain}"


def _passes_luhn(digits: str) -> bool:
    if not digits or len(digits) < 13 or len(digits) > 19:
        return False
    total = 0
    reverse_digits = digits[::-1]
    for index, char in enumerate(reverse_digits):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0
