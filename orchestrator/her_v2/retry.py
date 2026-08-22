"""Tiered, side-effect-aware provider recovery policy for HER v2."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Mapping

from orchestrator.flexible_backend_registry import is_cli_backend

from .models import Stage, TriageClassification


class RetryTier(IntEnum):
    TIER_1 = 1
    TIER_2 = 2
    TIER_3 = 3

    @property
    def label(self) -> str:
        return f"tier_{int(self)}"


DEFAULT_TIER_TIMEOUTS: Mapping[RetryTier, tuple[float, float]] = {
    # Fast presentation/classification work.  The recovered attempt is long
    # enough for the observed 146-second provider degradation case.
    RetryTier.TIER_1: (60.0, 180.0),
    # Planning, review, and background reasoning.
    RetryTier.TIER_2: (190.0, 300.0),
    # Local/CLI model execution, which is slow even when healthy.
    RetryTier.TIER_3: (300.0, 600.0),
}


DEFAULT_STAGE_TIERS: Mapping[Stage, RetryTier] = {
    Stage.IMMEDIATE_RESPONSE: RetryTier.TIER_1,
    Stage.TRIAGE: RetryTier.TIER_1,
    Stage.PLANNING: RetryTier.TIER_2,
    Stage.EXECUTION: RetryTier.TIER_2,
    Stage.REPLANNING: RetryTier.TIER_2,
    Stage.REVIEW: RetryTier.TIER_2,
    Stage.FINALISATION: RetryTier.TIER_1,
    Stage.MEDITATION: RetryTier.TIER_2,
    Stage.DREAM: RetryTier.TIER_2,
}


def _context_size(context: Mapping[str, Any] | None) -> int:
    try:
        return len(
            json.dumps(
                dict(context or {}),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        )
    except Exception:
        return 0


def _is_local_provider(engine: str) -> bool:
    normalized = str(engine or "").strip().casefold()
    return is_cli_backend(normalized) or normalized in {
        "ollama",
        "ollama-api",
        "local",
        "local-api",
    }


@dataclass(frozen=True)
class ProviderRetryPolicy:
    """Exactly one transient provider recovery with tiered attempt windows.

    Structured-envelope correction remains a separate semantic repair loop;
    ``max_provider_retries`` applies only to provider/transport failures.
    """

    tier_timeouts: Mapping[RetryTier, tuple[float, float]] = field(
        default_factory=lambda: dict(DEFAULT_TIER_TIMEOUTS)
    )
    max_provider_retries: int = 1
    large_context_bytes: int = 64 * 1024

    def __post_init__(self) -> None:
        if self.max_provider_retries != 1:
            raise ValueError("HER v2 provider recovery must be exactly one retry")
        for tier in RetryTier:
            values = self.tier_timeouts.get(tier)
            if not values or len(values) != 2:
                raise ValueError(f"missing timeout pair for {tier.label}")
            initial, recovery = (float(values[0]), float(values[1]))
            if initial <= 0 or recovery <= 0 or recovery < initial:
                raise ValueError(
                    f"invalid timeout pair for {tier.label}: {values!r}"
                )

    def tier_for(
        self,
        stage: Stage,
        *,
        engine: str,
        classification: TriageClassification | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> RetryTier:
        tier = DEFAULT_STAGE_TIERS[stage]
        if stage is Stage.FINALISATION and (
            classification is TriageClassification.HIGH_VOLUME_TASK
            or (context or {}).get("execution_json_valid") is False
        ):
            tier = max(tier, RetryTier.TIER_2)
        if _context_size(context) >= int(self.large_context_bytes):
            tier = min(RetryTier.TIER_3, RetryTier(int(tier) + 1))
        if _is_local_provider(engine):
            tier = RetryTier.TIER_3
        return RetryTier(tier)

    def timeout_for(self, tier: RetryTier, *, recovery_attempt: bool) -> float:
        pair = self.tier_timeouts[RetryTier(tier)]
        return float(pair[1 if recovery_attempt else 0])


DEFAULT_PROVIDER_RETRY_POLICY = ProviderRetryPolicy()


__all__ = [
    "DEFAULT_PROVIDER_RETRY_POLICY",
    "DEFAULT_STAGE_TIERS",
    "DEFAULT_TIER_TIMEOUTS",
    "ProviderRetryPolicy",
    "RetryTier",
]
