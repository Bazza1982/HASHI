"""Meaningful-progress tracking for HER v2 idle timeout enforcement."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class ProgressTracker:
    clock: Callable[[], float] = time.monotonic
    last_progress_at: float = field(init=False)
    _fingerprints: set[str] = field(default_factory=set, init=False)

    def __post_init__(self) -> None:
        self.last_progress_at = float(self.clock())

    def record(self, kind: str, content: str, *, meaningful: bool = True) -> bool:
        if not meaningful:
            return False
        normalized = " ".join(str(content or "").split())
        if not normalized:
            return False
        digest = hashlib.sha256(f"{kind}|{normalized}".encode("utf-8")).hexdigest()
        if digest in self._fingerprints:
            return False
        self._fingerprints.add(digest)
        self.last_progress_at = float(self.clock())
        return True

    def idle_for(self) -> float:
        return max(0.0, float(self.clock()) - self.last_progress_at)

    def expired(self, timeout_s: float) -> bool:
        return self.idle_for() >= float(timeout_s)
