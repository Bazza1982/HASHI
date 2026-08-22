"""Side-effect-aware provider recovery policy for HER v2."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderRetryPolicy:
    """Allow exactly one typed provider recovery without an elapsed deadline.

    Structured-envelope correction remains a separate semantic repair loop;
    ``max_provider_retries`` applies only to provider/transport failures.
    Runtime separately decides whether replay is safe after observed tool
    activity.  This policy never limits how long a healthy provider operation
    or tool-enabled execution may run.
    """

    max_provider_retries: int = 1

    def __post_init__(self) -> None:
        if self.max_provider_retries != 1:
            raise ValueError("HER v2 provider recovery must be exactly one retry")


DEFAULT_PROVIDER_RETRY_POLICY = ProviderRetryPolicy()


__all__ = [
    "DEFAULT_PROVIDER_RETRY_POLICY",
    "ProviderRetryPolicy",
]
