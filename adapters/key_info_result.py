"""Read-only Provider key inspection facts; no response body in failures."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from orchestrator.operation_failures import OperationFailure


@dataclass(frozen=True)
class KeyInfoResult:
    supported: bool
    payload: dict[str, Any] | None = None
    issue: OperationFailure | None = None
