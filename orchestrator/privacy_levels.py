"""Privacy-level policy primitives.

Levels 0, 1, and 2 are represented by the current policy foundation. Level 0
turns the privacy framework off, while Level 1 remains the default explicit
provider-trust mode. Higher levels remain product-roadmap concepts and are
intentionally not accepted here until their guarantees can be enforced.
"""

from __future__ import annotations

from enum import IntEnum

from orchestrator.flexible_backend_registry import get_supported_privacy_levels


class PrivacyLevel(IntEnum):
    OFF = 0
    PROVIDER_TRUST = 1
    BASIC_REDACTION = 2


class PrivacyPolicyError(ValueError):
    """Raised when a requested backend/privacy combination is not truthful."""


EXECUTABLE_PRIVACY_LEVELS = (
    PrivacyLevel.OFF,
    PrivacyLevel.PROVIDER_TRUST,
)


def parse_privacy_level(value: int | str | PrivacyLevel) -> PrivacyLevel:
    try:
        return PrivacyLevel(int(value))
    except (TypeError, ValueError) as exc:
        raise PrivacyPolicyError(
            "Only privacy levels 0, 1, and 2 are currently available."
        ) from exc


def require_backend_compatibility(
    engine: str,
    level: int | str | PrivacyLevel,
) -> PrivacyLevel:
    parsed = parse_privacy_level(level)
    supported = get_supported_privacy_levels(engine)
    if int(parsed) not in supported:
        raise PrivacyPolicyError(
            f"Backend {engine!r} does not support privacy level {int(parsed)}; "
            f"supported levels: {', '.join(str(item) for item in supported)}."
        )
    return parsed


def require_level_available(
    level: int | str | PrivacyLevel,
) -> PrivacyLevel:
    parsed = parse_privacy_level(level)
    if parsed not in EXECUTABLE_PRIVACY_LEVELS:
        raise PrivacyPolicyError(
            f"Privacy level {int(parsed)} is reserved but not available until "
            "its outbound protection is installed and verified."
        )
    return parsed


def require_transition_confirmation(
    current: int | str | PrivacyLevel,
    requested: int | str | PrivacyLevel,
    *,
    confirmed: bool = False,
) -> PrivacyLevel:
    current_level = parse_privacy_level(current)
    requested_level = parse_privacy_level(requested)
    if requested_level < current_level and not confirmed:
        raise PrivacyPolicyError(
            "Lowering the privacy level requires explicit user confirmation."
        )
    return requested_level
