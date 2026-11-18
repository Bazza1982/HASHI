"""Side-effect-aware provider recovery policy for HER v2."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ProviderRetryPolicy:
    """Task-frozen same-target recovery; never a healthy-operation deadline.

    Replay safety and destination changes are decided independently.
    Capacity waits may be empty to disable the extended capacity path.
    """

    max_provider_retries: int = 1
    base_delay_s: float = 0.25
    max_delay_s: float = 5.0
    capacity_delays_s: tuple[float, ...] = (60.0, 180.0, 300.0)

    def __post_init__(self) -> None:
        if isinstance(self.max_provider_retries, bool) or not isinstance(
            self.max_provider_retries, int
        ) or self.max_provider_retries < 0:
            raise ValueError("max_provider_retries must be a non-negative integer")
        for name in ("base_delay_s", "max_delay_s"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be a finite non-negative number")
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be a finite non-negative number")
            object.__setattr__(self, name, float(value))
        if not isinstance(self.capacity_delays_s, (list, tuple)):
            raise ValueError("capacity_delays_s must be a list of finite non-negative numbers")
        delays = []
        for value in self.capacity_delays_s:
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value < 0):
                raise ValueError("capacity_delays_s must be a list of finite non-negative numbers")
            delays.append(float(value))
        object.__setattr__(self, "capacity_delays_s", tuple(delays))

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ProviderRetryPolicy":
        if not isinstance(raw, Mapping):
            raise ValueError("provider_recovery must be an object")
        known = {"max_provider_retries", "base_delay_s", "max_delay_s", "capacity_delays_s"}
        unknown = set(raw) - known
        if unknown:
            raise ValueError(f"unknown provider_recovery fields: {sorted(unknown)}")
        return cls(**dict(raw))

    def delay(self, retry_index: int, retry_after_s: float | None = None) -> float:
        configured = min(self.max_delay_s, self.base_delay_s * (2 ** min(max(0, retry_index), 60)))
        if retry_after_s is None:
            return configured
        if isinstance(retry_after_s, bool) or not isinstance(retry_after_s, (int, float)):
            return configured
        if not math.isfinite(retry_after_s) or retry_after_s < 0:
            return configured
        return max(configured, float(retry_after_s))


DEFAULT_PROVIDER_RETRY_POLICY = ProviderRetryPolicy()


__all__ = [
    "DEFAULT_PROVIDER_RETRY_POLICY",
    "ProviderRetryPolicy",
]
