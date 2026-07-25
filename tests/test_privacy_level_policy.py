from __future__ import annotations

import pytest

from orchestrator.flexible_backend_registry import get_supported_privacy_levels
from orchestrator.privacy_levels import (
    PrivacyLevel,
    PrivacyPolicyError,
    parse_privacy_level,
    require_backend_compatibility,
    require_level_available,
    require_transition_confirmation,
)


@pytest.mark.parametrize(
    "engine",
    ("openrouter-api", "deepseek-api", "xai-api", "ollama-api"),
)
def test_api_backends_declare_level_two_support(engine: str) -> None:
    assert get_supported_privacy_levels(engine) == (0, 1, 2)
    assert (
        require_backend_compatibility(engine, 2)
        is PrivacyLevel.BASIC_REDACTION
    )


@pytest.mark.parametrize(
    "engine",
    ("gemini-cli", "claude-cli", "codex-cli", "claw-cli", "grok-cli"),
)
def test_cli_harnesses_are_level_one_only(engine: str) -> None:
    assert get_supported_privacy_levels(engine) == (0, 1)
    assert require_backend_compatibility(engine, 0) is PrivacyLevel.OFF
    with pytest.raises(PrivacyPolicyError, match="does not support"):
        require_backend_compatibility(engine, 2)


def test_unknown_backend_fails_closed_for_level_two() -> None:
    assert get_supported_privacy_levels("unknown-backend") == (0, 1)
    with pytest.raises(PrivacyPolicyError, match="does not support"):
        require_backend_compatibility("unknown-backend", 2)


def test_higher_levels_are_not_accepted_before_they_are_enforceable() -> None:
    with pytest.raises(PrivacyPolicyError, match="Only privacy levels 0, 1, and 2"):
        parse_privacy_level(3)


def test_only_levels_zero_and_one_are_currently_activatable() -> None:
    assert require_level_available(0) is PrivacyLevel.OFF
    assert require_level_available(1) is PrivacyLevel.PROVIDER_TRUST
    with pytest.raises(PrivacyPolicyError, match="not available"):
        require_level_available(2)


def test_privacy_downgrade_requires_explicit_confirmation() -> None:
    with pytest.raises(PrivacyPolicyError, match="explicit user confirmation"):
        require_transition_confirmation(2, 1)

    assert (
        require_transition_confirmation(2, 1, confirmed=True)
        is PrivacyLevel.PROVIDER_TRUST
    )
    assert (
        require_transition_confirmation(1, 0, confirmed=True)
        is PrivacyLevel.OFF
    )
