"""Narrow Windows file supervisor for isolated integration, not an installed service.

Only held-handle reads and new-file creation are supported. Existing-file
replacement, parent creation, process isolation and crash cleanup remain
unimplemented. The authority, encrypted result key, stop signer and this
supervisor must live in a trusted process. A PAO Run fence is mandatory, but\nthe trusted integration is not installed. This class is not an L2 boundary.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
from pathlib import Path
from threading import Event
from typing import Callable

from orchestrator.permissions.authority import PermissionAuthority, PermissionDenied
from orchestrator.permissions.descriptors import ToolDescriptor, describe_tool
from orchestrator.permissions.execution_gate import SupervisedResult
from orchestrator.permissions.models import ExecutionContext
from orchestrator.permissions.pao_run_fence import PaoRunFence
from orchestrator.permissions.result_store import EncryptedResultStore, ResultUnavailable
from orchestrator.permissions.windows_file_binding import open_bound_create, open_bound_read


_MAX_WRITE_BYTES = 1024 * 1024
_MAX_READ_BYTES = 16 * 1024 * 1024


async def _settled_thread(function: Callable, *args, cancel_event: Event | None = None):
    """Do not return cancellation while a synchronous effect is still running.

    This may wait for a blocked OS call. A killable independent supervisor is
    still required for bounded cancellation and L2.
    """
    worker = asyncio.create_task(asyncio.to_thread(function, *args))
    interrupted = False
    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            interrupted = True
            if cancel_event is not None:
                cancel_event.set()
        except Exception:
            break
    try:
        result = worker.result()
    except BaseException:
        if interrupted:
            raise asyncio.CancelledError from None
        raise
    if interrupted:
        raise asyncio.CancelledError
    return result


def _render_read(path: str, data: bytes, offset: int, limit: int) -> str:
    selected: list[str] = []
    remaining = 20_000
    truncated = False
    with io.StringIO(data.decode("utf-8", errors="replace"), newline=None) as handle:
        for _ in range(offset - 1):
            while True:
                chunk = handle.readline(8192)
                if not chunk:
                    return f"[{path}] No lines at offset {offset}"
                if chunk.endswith(chr(10)):
                    break
        for _ in range(limit):
            line = handle.readline(remaining + 1)
            if not line:
                break
            if len(line) > remaining:
                selected.append(line[:remaining])
                truncated = True
                break
            selected.append(line)
            remaining -= len(line)
            if remaining == 0:
                truncated = bool(handle.read(1))
                break
        else:
            truncated = bool(handle.read(1))
    header = f"[{path}] lines {offset}-{offset + len(selected) - 1}"
    content = "".join(selected)
    if truncated:
        content += chr(10) + "[truncated by HASHI; use a narrower offset or log_query]"
    return header + chr(10) + content


class WindowsFileSupervisor:
    """Execute one authorized Windows file effect with a verified cleanup receipt."""

    def __init__(
        self, authority: PermissionAuthority, *, cwd: Path, read_roots: tuple[Path, ...],
        result_store: EncryptedResultStore, issue_stop: Callable[[ExecutionContext, str], str],
        run_guard: PaoRunFence,
    ) -> None:
        if (not isinstance(authority, PermissionAuthority) or not isinstance(result_store, EncryptedResultStore)
                or not callable(issue_stop) or not isinstance(run_guard, PaoRunFence)):
            raise ValueError("trusted authority, result store and stop issuer are required")
        directory = Path(cwd)
        roots = tuple(Path(root) for root in read_roots)
        if not directory.is_absolute() or any(not root.is_absolute() for root in roots):
            raise ValueError("trusted file roots must be absolute")
        self._authority = authority
        self._cwd = directory.resolve(strict=False)
        self._read_roots = tuple(root.resolve(strict=False) for root in roots)
        self._store = result_store
        self._issue_stop = issue_stop
        self._run_guard = run_guard

    def supports(self, tool_name: str) -> bool:
        return os.name == "nt" and tool_name in {"file_read", "file_write"}

    async def execute(
        self, context: ExecutionContext, operation_id: str,
        descriptor: ToolDescriptor, arguments: dict,
    ) -> SupervisedResult:
        if not isinstance(arguments, dict):
            raise PermissionDenied("file arguments are invalid")
        try:
            encoded = json.dumps(
                arguments, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
            ).encode("utf-8")
            if len(encoded) > _MAX_WRITE_BYTES:
                raise ValueError("file arguments are too large")
            frozen = json.loads(encoded)
        except (TypeError, ValueError, UnicodeError) as exc:
            raise PermissionDenied("file arguments are invalid") from exc
        cancelled = Event()
        return await _settled_thread(
            self._execute_sync, context, operation_id, descriptor, frozen, cancelled, cancel_event=cancelled,
        )

    def _execute_sync(
        self, context: ExecutionContext, operation_id: str, descriptor: ToolDescriptor, arguments: dict,
        cancelled: Event,
    ) -> SupervisedResult:
        def require_not_cancelled() -> None:
            if cancelled.is_set():
                raise PermissionDenied("file operation was cancelled")

        require_not_cancelled()
        if not isinstance(context, ExecutionContext) or not isinstance(descriptor, ToolDescriptor):
            raise PermissionDenied("trusted file operation facts are required")
        if not self.supports(descriptor.tool_name):
            raise PermissionDenied("the file operation is unavailable")
        try:
            actual = describe_tool(
                descriptor.tool_name, arguments, cwd=self._cwd, read_roots=self._read_roots,
            )
        except (TypeError, ValueError, UnicodeError) as exc:
            raise PermissionDenied("file operation facts are invalid") from exc
        if actual != descriptor or len(descriptor.scopes) != 1:
            raise PermissionDenied("file operation facts do not match")
        scope = descriptor.scopes[0]

        def run_bound(binding, effect):
            claim = None
            try:
                with binding as bound:
                    require_not_cancelled()
                    guard = self._run_guard(context)
                    with guard:
                        claim = self._authority.prepare(
                            context, operation_id, descriptor.arguments_digest, descriptor.scopes,
                        )
                        require_not_cancelled()

                        def perform():
                            require_not_cancelled()
                            return effect(bound)

                        value = self._authority.dispatch_guarded(
                            context, claim.claim_id, operation_id=operation_id,
                            arguments_digest=descriptor.arguments_digest, scopes=descriptor.scopes, effect=perform,
                        )
                return claim, value
            except PermissionDenied:
                if cancelled.is_set() and claim is not None:
                    # The held handles have closed. A prepared claim remains
                    # reserved until the verifier accepts exact stop evidence.
                    self._authority.cancel_prepared(context, claim.claim_id)
                    reference = self._issue_stop(context, claim.claim_id)
                    self._authority.confirm_resource_stopped(context, claim.claim_id, receipt_reference=reference)
                raise

        if descriptor.tool_name == "file_read":
            if set(arguments) - {"path", "offset", "limit"}:
                raise PermissionDenied("unsupported file read arguments")
            offset, limit = arguments.get("offset", 1), arguments.get("limit", 500)
            if type(offset) is not int or type(limit) is not int:
                raise PermissionDenied("invalid file read range")
            offset, limit = max(1, offset), max(1, min(500, limit))
            claim, data = run_bound(open_bound_read(scope), lambda bound: bound.read(max_bytes=_MAX_READ_BYTES))
            if not isinstance(data, bytes):
                raise PermissionDenied("file read returned an invalid result")
            output = _render_read(scope.resource, data, offset, limit)
        else:
            if set(arguments) != {"path", "content"} or not isinstance(arguments["content"], str):
                raise PermissionDenied("unsupported file write arguments")
            try:
                data = arguments["content"].encode("utf-8")
            except UnicodeError as exc:
                raise PermissionDenied("file content is not valid text") from exc
            if len(data) > _MAX_WRITE_BYTES:
                raise PermissionDenied("file content is too large")
            claim, _ = run_bound(open_bound_create(scope), lambda bound: bound.commit(data))
            output = f"OK: wrote {len(arguments['content'])} characters to {scope.resource}"
        result = self._store.put(context, operation_id, descriptor, outcome="succeeded", payload=output)
        self._authority.finish(context, claim.claim_id, outcome=result.outcome, result_digest=result.result_digest)
        reference = self._issue_stop(context, claim.claim_id)
        self._authority.confirm_resource_stopped(context, claim.claim_id, receipt_reference=reference)
        return result

    def _recover_sync(
        self, context: ExecutionContext, operation_id: str, descriptor: ToolDescriptor,
    ) -> SupervisedResult | None:
        guard = self._run_guard(context)
        with guard:
            return self._store.get(context, operation_id, descriptor)

    async def recover(
        self, context: ExecutionContext, operation_id: str, descriptor: ToolDescriptor,
    ) -> SupervisedResult:
        result = await _settled_thread(self._recover_sync, context, operation_id, descriptor)
        if result is None:
            raise ResultUnavailable("the original result is unavailable")
        return result
