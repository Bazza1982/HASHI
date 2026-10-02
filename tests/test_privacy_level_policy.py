from __future__ import annotations

import pytest

from orchestrator.flexible_backend_registry import get_supported_privacy_levels
from orchestrator.privacy_levels import (
    PrivacyLevel,
    PrivacyPolicyError,
    parse_privacy_level,
    require_backend_compatibility,
    require_herv3_provider_compatibility,
    require_level_available,
    require_transition_confirmation,
)


@pytest.mark.parametrize(
    "engine",
    (
        "openrouter-api",
        "deepseek-api",
        "openai-compatible-api",
        "xai-api",
        "ollama-api",
    ),
)
def test_api_backends_cannot_run_as_outer_level_two_backends(engine: str) -> None:
    assert get_supported_privacy_levels(engine) == (0, 1)
    with pytest.raises(PrivacyPolicyError, match="does not support"):
        require_backend_compatibility(engine, 2)


def test_only_herv3_is_an_outer_level_two_backend() -> None:
    assert get_supported_privacy_levels("her-v2") == (0, 1, 2)
    assert require_backend_compatibility("her-v2", 2) is PrivacyLevel.BASIC_REDACTION


def test_only_qualified_deepseek_provider_runs_inside_herv3_level_two() -> None:
    assert (
        require_herv3_provider_compatibility("deepseek-api", 2)
        is PrivacyLevel.BASIC_REDACTION
    )
    for engine in ("hashi-api", "openrouter-api", "codex-cli", "unknown-backend"):
        with pytest.raises(PrivacyPolicyError, match="does not support"):
            require_herv3_provider_compatibility(engine, 2)


@pytest.mark.parametrize(
    "engine",
    ("gemini-cli", "claude-cli", "codex-cli", "grok-cli"),
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


def test_level_two_is_activatable_after_scoped_enforcement() -> None:
    assert require_level_available(0) is PrivacyLevel.OFF
    assert require_level_available(1) is PrivacyLevel.PROVIDER_TRUST
    assert require_level_available(2) is PrivacyLevel.BASIC_REDACTION


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
