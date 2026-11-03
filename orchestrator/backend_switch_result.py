"""One PAO-owned backend selection transaction's actual outcome."""
from __future__ import annotations

from dataclasses import dataclass

from orchestrator.operation_failures import OperationFailure


@dataclass(frozen=True)
class BackendSwitchResult:
    switched: bool
    issue: OperationFailure | None = None
