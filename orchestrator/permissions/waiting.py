"""Wait for a task's durable permission decision without polling a model.

This is an internal coordination primitive. It is not yet wired into HERV3,
does not persist a Python continuation, and does not start a resource. A ready
result only permits the caller to enter its normal resource queue; the real
supervisor must recheck and commit start after that queue.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import math
from typing import Iterable, Protocol

from orchestrator.permissions.authority import (
    AccessRequest,
    ExecutionStatus,
    OperationConflict,
    OperationStatus,
    PermissionDenied,
)
from orchestrator.permissions.models import ExecutionContext, Scope, canonical_scope_set, scope_digest


class WaitAuthority(Protocol):
    def execution_status(self, context: ExecutionContext) -> ExecutionStatus: ...

    def operation_status_for_task(self, context: ExecutionContext, operation_id: str) -> OperationStatus: ...

    def find_operation_for_task(
        self, context: ExecutionContext, operation_id: str
    ) -> OperationStatus | None: ...

    def get_request(self, context: ExecutionContext, request_id: str) -> AccessRequest: ...

    def evaluate(
        self, context: ExecutionContext, scopes: Iterable[Scope], *, operation_id: str | None = None
    ) -> bool: ...

    def request_access(
        self, context: ExecutionContext, scopes: Iterable[Scope], **kwargs: object
    ) -> AccessRequest: ...


@dataclass(frozen=True, slots=True)
class WaitResult:
    state: str
    request_id: str
    operation_id: str


class PermissionWaiter:
    """Wait on event hints and periodically reconcile against the authority."""

    def __init__(self, authority: WaitAuthority, *, reconcile_seconds: float = 1.0) -> None:
        methods = ("execution_status", "operation_status_for_task", "get_request", "evaluate", "request_access")
        if not all(callable(getattr(authority, name, None)) for name in methods):
            raise ValueError("permission authority is required")
        if (isinstance(reconcile_seconds, bool) or not isinstance(reconcile_seconds, (int, float))
                or not math.isfinite(reconcile_seconds) or reconcile_seconds <= 0):
            raise ValueError("reconciliation interval must be positive")
        self._authority = authority
        self._interval = float(reconcile_seconds)
        self._changed = asyncio.Event()

    def uses_authority(self, authority: WaitAuthority) -> bool:
        """The coordinator and execution gate must share the same authority port."""
        return self._authority is authority

    def notify(self) -> None:
        """Notify waiters on their event loop; a notification is not authority."""
        self._changed.set()

    async def wait(
        self,
        context: ExecutionContext,
        request_id: str,
        operation_id: str,
        arguments_digest: str,
        scopes: Iterable[Scope],
        *,
        reason: str,
        timeout_seconds: float | None = None,
    ) -> WaitResult:
        if not isinstance(context, ExecutionContext):
            raise PermissionDenied("trusted execution context is required")
        if not isinstance(request_id, str) or not request_id or not isinstance(operation_id, str) or not operation_id:
            raise ValueError("request and operation identities are required")
        if timeout_seconds is not None and (
            isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds) or timeout_seconds <= 0
        ):
            raise ValueError("timeout must be positive")
        normalized = canonical_scope_set(scopes)
        digest = scope_digest(normalized)
        loop = asyncio.get_running_loop()
        deadline = None if timeout_seconds is None else loop.time() + timeout_seconds
        current_id = request_id

        while True:
            status = await asyncio.to_thread(self._authority.execution_status, context)
            if status.task_state in {"completed", "cancelled", "failed_terminal"}:
                return WaitResult(status.task_state, current_id, operation_id)
            if status.task_state == "recovering":
                return WaitResult("recovering", current_id, operation_id)
            if status.binding_state != "active":
                return WaitResult("fenced", current_id, operation_id)
            operation = await asyncio.to_thread(
                self._authority.operation_status_for_task, context, operation_id
            )
            if operation.arguments_digest != arguments_digest or operation.scope_digest != digest:
                raise OperationConflict("waiting operation facts changed")
            if operation.execution_epoch != context.execution_epoch:
                return WaitResult("fenced", current_id, operation_id)
            if operation.state != "waiting":
                return WaitResult(operation.state, current_id, operation_id)

            if status.task_state == "active":
                request = await asyncio.to_thread(self._authority.get_request, context, current_id)
                if request.operation_id != operation_id:
                    raise PermissionDenied("request does not bind the waiting operation")
                covered = await asyncio.to_thread(
                    self._authority.evaluate, context, normalized, operation_id=operation_id
                )
                if covered:
                    return WaitResult("ready", current_id, operation_id)
                if request.state in {"denied", "withdrawn"}:
                    return WaitResult(request.state, current_id, operation_id)
                if request.state in {"decided", "expired"}:
                    try:
                        followup = await asyncio.to_thread(
                            self._authority.request_access,
                            context,
                            normalized,
                            purpose=reason,
                            operation_id=operation_id,
                            arguments_digest=arguments_digest,
                        )
                    except PermissionDenied:
                        # Only a concurrent lifecycle transition is retried.
                        # Storage, identity or clock failures remain errors and
                        # cannot become an unbounded service-polling loop.
                        current = await asyncio.to_thread(
                            self._authority.execution_status, context
                        )
                        if current.task_state != "active" or current.binding_state != "active":
                            continue
                        raise
                    if followup.state == "granted_existing":
                        continue
                    if followup.state == "denied":
                        return WaitResult("denied", followup.request_id, operation_id)
                    if followup.state != "pending" or not followup.request_id:
                        raise OperationConflict("permission request cannot be resumed")
                    current_id = followup.request_id

            timeout = self._interval
            if deadline is not None:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return WaitResult("waiting", current_id, operation_id)
                timeout = min(timeout, remaining)
            try:
                await asyncio.wait_for(self._changed.wait(), timeout=timeout)
            except TimeoutError:
                pass
            finally:
                self._changed.clear()
