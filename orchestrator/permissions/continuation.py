"""Internal park signal, deliberately outside ordinary model/provider failures."""
from __future__ import annotations

from orchestrator.permissions.execution_gate import GateResult


class PermissionPark(BaseException):
    """Unwind a host to its trusted owner after a durable pending boundary.

    A park is neither a failed Tool result nor permission to replay a side
    effect. Its owner must consult the stores, fence the old executor and bind
    a current epoch before resuming the saved provider batch.
    """

    def __init__(self, result: GateResult):
        if not isinstance(result, GateResult):
            raise TypeError("a typed permission control result is required")
        self.result = result
        super().__init__("permission-controlled execution parked")
