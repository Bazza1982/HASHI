"""Conservative, request-observed provider fallback policy for HER v2."""

from __future__ import annotations

from dataclasses import replace

from orchestrator import ui_language

from .config import HERv2Config, ProviderProfile
from .interfaces import ProviderFailureCode, StageInvocationError


FALLBACK_MEANINGFUL_OUTPUT_TIMEOUT_S = 300.0


FALLBACK_ELIGIBLE_FAILURE_CODES = frozenset(
    {
        ProviderFailureCode.PROVIDER_REQUEST_TIMEOUT.value,
        ProviderFailureCode.PROVIDER_RATE_LIMITED.value,
        ProviderFailureCode.PROVIDER_CAPACITY_UNAVAILABLE.value,
        ProviderFailureCode.PROVIDER_QUOTA_EXHAUSTED.value,
        ProviderFailureCode.PROVIDER_MODEL_UNAVAILABLE.value,
        ProviderFailureCode.PROVIDER_SERVER_ERROR.value,
        ProviderFailureCode.PROVIDER_CONNECTION_FAILED.value,
        ProviderFailureCode.PROVIDER_RESPONSE_START_TIMEOUT.value,
        ProviderFailureCode.PROVIDER_INCOMPLETE_STREAM.value,
        ProviderFailureCode.PROVIDER_INCOMPLETE_STREAM_TIMEOUT.value,
        ProviderFailureCode.PROVIDER_REASONING_ONLY_TIMEOUT.value,
        ProviderFailureCode.PROVIDER_STREAM_IDLE_TIMEOUT.value,
        ProviderFailureCode.PROVIDER_EMPTY_RESPONSE.value,
    }
)


def fallback_model_class(config: HERv2Config, profile: ProviderProfile) -> str:
    """Derive quality class from the owning HER model slot, never model prose."""

    declared = str(profile.options.get("_her_model_class") or "").strip().casefold()
    if declared in {"light", "pro"}:
        return declared
    return (
        "light"
        if str(config.profile_model_slots.get(profile.name, "pro")) == "fast"
        else "pro"
    )


def fallback_failure_eligible(error: StageInvocationError) -> bool:
    return bool(
        error.retryable and error.error_code in FALLBACK_ELIGIBLE_FAILURE_CODES
    )


def next_fallback_profile(
    config: HERv2Config,
    *,
    primary: ProviderProfile,
    current: ProviderProfile,
    current_level: int,
    model_class: str,
) -> tuple[int, ProviderProfile] | None:
    """Select the next valid level while enforcing provider and quality rules."""

    if not config.fallback_enabled:
        return None
    for level in range(max(0, int(current_level)) + 1, 3):
        target = config.fallback_target(level, model_class)
        if target is None:
            continue
        if level == 1 and target.engine != primary.engine:
            continue
        if level == 2 and target.engine == primary.engine:
            continue
        if (target.engine, target.model) in {
            (primary.engine, primary.model),
            (current.engine, current.model),
        }:
            continue
        return level, replace(
            primary,
            engine=target.engine,
            model=target.model,
        )
    return None


def fallback_warning_text(
    *,
    failed: ProviderProfile,
    target: ProviderProfile,
    level: int,
) -> str:
    return ui_language.tr(
        "her.fallback.warning",
        failed_provider=failed.engine,
        failed_model=failed.model,
        level=level,
        target_provider=target.engine,
        target_model=target.model,
    )
