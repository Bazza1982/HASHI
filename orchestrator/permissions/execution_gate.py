"""Managed-operation coordination around an independently trusted supervisor.

This internal gate is not installed in ToolRegistry or HERV3. The supervisor
must hold and enforce the actual resource/start barrier and cleanup contract;
an injected Python object or a ledger claim alone is not an OS boundary.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Protocol

from orchestrator.permissions.authority import (
    OperationConflict,
    PermissionDenied,
)
from orchestrator.permissions.descriptors import ToolDescriptor, UnknownTool, describe_tool
from orchestrator.permissions.models import ExecutionContext, Scope, canonical_scope_set, scope_covers, scope_digest
from orchestrator.permissions.waiting import PermissionWaiter, WaitAuthority


@dataclass(frozen=True, slots=True)
class SupervisedResult:
    operation_id: str
    arguments_digest: str
    outcome: str
    result_digest: str | None
    payload: Any


@dataclass(frozen=True, slots=True)
class GateResult:
    state: str
    operation_id: str
    request_id: str = ""
    payload: Any = None
    replayed: bool = False


class Supervisor(Protocol):
    """Trusted runner port; implementation must enforce its own boundary."""

    def supports(self, tool_name: str) -> bool: ...

    async def execute(
        self, context: ExecutionContext, operation_id: str,
        descriptor: ToolDescriptor, arguments: dict[str, Any],
    ) -> SupervisedResult: ...

    async def recover(
        self, context: ExecutionContext, operation_id: str, descriptor: ToolDescriptor,
    ) -> SupervisedResult: ...


class PermissionExecutionGate:
    """Prepare an operation and never dispatch through a legacy fallback."""

    def __init__(
        self,
        authority: WaitAuthority,
        waiter: PermissionWaiter,
        supervisor: Supervisor,
        *,
        context: ExecutionContext,
        cwd: Path,
        read_roots: tuple[Path, ...],
        requestable_scopes: tuple[Scope, ...],
        durable_control: bool = False,
    ) -> None:
        methods = (
            "execution_status", "operation_status_for_task", "find_operation_for_task",
            "get_request", "evaluate", "request_access",
        )
        if not isinstance(context, ExecutionContext) or not all(
            callable(getattr(authority, name, None)) for name in methods
        ):
            raise ValueError("trusted authority and execution context are required")
        if (not isinstance(waiter, PermissionWaiter) or not waiter.uses_authority(authority)
                or not all(callable(getattr(supervisor, name, None)) for name in ("supports", "execute"))):
            raise ValueError("a matching trusted waiter and supervisor are required")
        directory = Path(cwd)
        if not directory.is_absolute():
            raise ValueError("trusted working directory must be absolute")
        if type(durable_control) is not bool:
            raise ValueError("durable control must be explicit")
        self._unsupported_state = "unsupported" if durable_control else "unavailable"
        self._authority = authority
        self._waiter = waiter
        self._supervisor = supervisor
        self._context = context
        self._cwd = directory.resolve(strict=False)
        self._read_roots = tuple(Path(root) for root in read_roots)
        self._requestable = canonical_scope_set(requestable_scopes)

    async def execute(
        self,
        operation_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        reason: str,
        timeout_seconds: float | None = None,
        wait_for_approval: bool = True,
    ) -> GateResult:
        # Operation identity is an internal PAO binding, not a model parameter.
        if (not isinstance(operation_id, str) or not operation_id.strip()
                or len(operation_id) > 512 or "\x00" in operation_id):
            raise ValueError("operation identity is required")
        try:
            if not isinstance(arguments, dict):
                raise ValueError("arguments must be an object")
            descriptor = describe_tool(
                tool_name, arguments, cwd=self._cwd, read_roots=self._read_roots,
            )
            encoded = json.dumps(
                arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
            ).encode("utf-8")
            if len(encoded) > 1024 * 1024:
                raise ValueError("arguments exceed the execution limit")
            frozen_arguments = json.loads(encoded)
            frozen_descriptor = describe_tool(
                tool_name, frozen_arguments, cwd=self._cwd, read_roots=self._read_roots,
            )
            if descriptor != frozen_descriptor:
                raise ValueError("arguments changed during admission")
        except (UnknownTool, TypeError, ValueError, UnicodeError):
            return GateResult(self._unsupported_state, operation_id)
        if not descriptor.scopes:
            return GateResult(self._unsupported_state, operation_id)
        try:
            status = await asyncio.to_thread(self._authority.execution_status, self._context)
            if status.task_state != "active":
                return GateResult(status.task_state, operation_id)
            if status.binding_state != "active":
                return GateResult("fenced", operation_id)
            existing = await asyncio.to_thread(
                self._authority.find_operation_for_task, self._context, operation_id,
            )
        except OperationConflict:
            return GateResult("conflict", operation_id)
        except Exception:
            return GateResult("unavailable", operation_id)

        if existing is not None:
            if (existing.arguments_digest != descriptor.arguments_digest
                    or existing.scope_digest != scope_digest(descriptor.scopes)):
                return GateResult("conflict", operation_id)
            if existing.state in {"prepared", "started", "unknown"}:
                return GateResult("unknown", operation_id)
        try:
            supported = self._supervisor.supports(tool_name) is True
            validate_plan = getattr(self._supervisor, "validate_plan", None)
            if supported and callable(validate_plan):
                supported = validate_plan(descriptor, frozen_arguments) is True
        except Exception:
            return GateResult("unavailable", operation_id)
        covered_by_policy = bool(descriptor.scopes) and all(
            any(scope_covers(parent, required) for parent in self._requestable)
            for required in descriptor.scopes
        )
        if existing is not None:
            if existing.state in {"succeeded", "failed"}:
                if not supported or not covered_by_policy or existing.resource_state != "stopped":
                    return GateResult("result_unavailable", operation_id)
                recover = getattr(self._supervisor, "recover", None)
                if not callable(recover):
                    return GateResult("result_unavailable", operation_id)
                try:
                    receipt = await recover(self._context, operation_id, descriptor)
                except Exception:
                    return GateResult("result_unavailable", operation_id)
                return await self._verified_receipt(operation_id, descriptor, receipt, replay=True)
            if existing.state != "waiting":
                return GateResult("permission_changed", operation_id)
        if not supported or not covered_by_policy:
            return GateResult(self._unsupported_state, operation_id)
        try:
            covered = await asyncio.to_thread(
                self._authority.evaluate, self._context, descriptor.scopes, operation_id=operation_id,
            )
        except Exception:
            return GateResult("unavailable", operation_id)
        request_id = ""
        if not covered:
            try:
                request = await asyncio.to_thread(
                    self._authority.request_access, self._context, descriptor.scopes,
                    purpose=reason, operation_id=operation_id,
                    arguments_digest=descriptor.arguments_digest,
                )
            except PermissionDenied:
                return GateResult("permission_changed", operation_id)
            except OperationConflict:
                return GateResult("conflict", operation_id)
            except Exception:
                return GateResult("unavailable", operation_id)
            request_id = request.request_id
            if request.state == "denied":
                return GateResult("denied", operation_id, request_id)
            if request.state == "pending":
                if not wait_for_approval:
                    return GateResult("waiting", operation_id, request_id)
                try:
                    wait = await self._waiter.wait(
                        self._context, request_id, operation_id, descriptor.arguments_digest,
                        descriptor.scopes, reason=reason, timeout_seconds=timeout_seconds,
                    )
                except OperationConflict:
                    return GateResult("conflict", operation_id, request_id)
                except Exception:
                    return GateResult("unavailable", operation_id, request_id)
                request_id = wait.request_id
                if wait.state != "ready":
                    return GateResult(wait.state, operation_id, request_id)
            elif request.state != "granted_existing":
                return GateResult("unavailable", operation_id, request_id)

        try:
            receipt = await self._supervisor.execute(
                self._context, operation_id, descriptor, frozen_arguments,
            )
        except (PermissionDenied, OperationConflict) as exc:
            try:
                current = await asyncio.to_thread(
                    self._authority.operation_status_for_task, self._context, operation_id
                )
            except Exception:
                return GateResult("unknown", operation_id, request_id)
            if current.state != "waiting":
                # Even a denial from the runner may arrive after it started a
                # side effect. Do not mislabel that state as safe to retry.
                return GateResult("unknown", operation_id, request_id)
            state = "permission_changed" if isinstance(exc, PermissionDenied) else "conflict"
            return GateResult(state, operation_id, request_id)
        except Exception:
            # A runner failure can follow a side effect. The caller must not
            # replace the operation ID and retry without reconciliation.
            return GateResult("unknown", operation_id, request_id)
        return await self._verified_receipt(operation_id, descriptor, receipt, request_id=request_id)

    async def _verified_receipt(
        self, operation_id: str, descriptor: ToolDescriptor, receipt: object, *,
        request_id: str = "", replay: bool = False,
    ) -> GateResult:
        failure = "result_unavailable" if replay else "unknown"
        if not isinstance(receipt, SupervisedResult):
            return GateResult(failure, operation_id, request_id)
        if (receipt.operation_id != operation_id or receipt.arguments_digest != descriptor.arguments_digest
                or receipt.outcome not in {"succeeded", "failed", "unknown"}):
            return GateResult(failure, operation_id, request_id)
        try:
            stored = await asyncio.to_thread(
                self._authority.find_operation_for_task, self._context, operation_id
            )
        except Exception:
            return GateResult(failure, operation_id, request_id)
        if (stored is None or stored.arguments_digest != descriptor.arguments_digest
                or stored.scope_digest != scope_digest(descriptor.scopes)
                or stored.state != receipt.outcome or stored.result_digest != receipt.result_digest
                or stored.resource_state != "stopped"):
            return GateResult(failure, operation_id, request_id)
        if receipt.outcome == "unknown":
            return GateResult("unknown", operation_id, request_id)
        try:
            if not isinstance(receipt.payload, str):
                return GateResult(failure, operation_id, request_id)
            payload_digest = sha256(receipt.payload.encode("utf-8")).hexdigest()
        except UnicodeError:
            return GateResult(failure, operation_id, request_id)
        if payload_digest != receipt.result_digest:
            return GateResult(failure, operation_id, request_id)
        return GateResult(receipt.outcome, operation_id, request_id, receipt.payload, replayed=replay)
