"""Killable fixed Windows file helpers with held objects and final start fencing.

Only installed trusted code constructs this owner. No execution RPC accepts
these paths, environment, signer or policy callback from an agent.
"""
from __future__ import annotations

import asyncio
import base64
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile

from orchestrator.permissions.authority import PermissionDenied
from orchestrator.permissions.descriptors import describe_tool
from orchestrator.permissions.pao_run_fence import PaoRunFence
from orchestrator.permissions.windows_file_binding import (
    BoundWindowsCreate, BoundWindowsRead, open_bound_read, open_bound_write,
)
from orchestrator.permissions.windows_file_supervisor import _render_read
from orchestrator.permissions.windows_process import inheritable_handle_copy, launch_suspended


class WindowsFileProcessSupervisor:
    def __init__(self, authority, *, cwd, read_roots, result_store, issue_stop, run_guard,
                 helper_executable, helper_entry, executable_digest, entry_digest,
                 private_root, environment, expected_user_sid, validate_resource):
        if (not isinstance(run_guard, PaoRunFence) or not callable(issue_stop)
                or not callable(validate_resource) or os.name != "nt"):
            raise ValueError("a native trusted file supervisor is required")
        self._authority, self._guard, self._store = authority, run_guard, result_store
        self._stop_receipt, self._validate = issue_stop, validate_resource
        self._cwd, self._roots = Path(cwd), tuple(Path(p) for p in read_roots)
        self._executable, self._entry = Path(helper_executable), Path(helper_entry)
        self._executable_digest, self._entry_digest = executable_digest, entry_digest
        self._private = Path(private_root)
        if (not all(p.is_absolute() for p in (self._cwd, self._executable, self._entry, self._private))
                or not self._private.is_dir()
                or sha256(self._entry.read_bytes()).hexdigest() != entry_digest):
            raise ValueError("a qualified helper and protected artifact root are required")
        self._environment, self._sid = dict(environment), expected_user_sid
        self._capacity = asyncio.Semaphore(1)
        self._active, self._closed = {}, False
        self._tasks = set()

    def supports(self, tool_name):
        return tool_name in {"file_read", "file_write"}

    def _authorized(self, context, claim, operation_id, descriptor):
        if self._closed or any(self._validate(context, scope) is not True for scope in descriptor.scopes):
            raise PermissionDenied("file execution policy changed")
        with self._guard(context):
            self._authority.check_running_claim(
                context, claim.claim_id, operation_id=operation_id,
                arguments_digest=descriptor.arguments_digest, scopes=descriptor.scopes,
            )

    async def execute(self, context, operation_id, descriptor, arguments):
        frozen = json.loads(json.dumps(arguments, allow_nan=False))
        actual = describe_tool(descriptor.tool_name, frozen, cwd=self._cwd, read_roots=self._roots)
        if actual != descriptor or len(descriptor.scopes) != 1 or not self.supports(descriptor.tool_name):
            raise PermissionDenied("file plan differs from its reviewed descriptor")
        scope = descriptor.scopes[0]
        if self._validate(context, scope) is not True:
            raise PermissionDenied("file is outside the installed execution policy")
        if descriptor.tool_name == "file_read":
            if set(frozen) - {"path", "offset", "limit"}:
                raise PermissionDenied("unsupported file read arguments")
            offset, limit = frozen.get("offset", 1), frozen.get("limit", 500)
            if type(offset) is not int or type(limit) is not int:
                raise PermissionDenied("invalid read range")
            data = None
        else:
            if set(frozen) != {"path", "content"} or not isinstance(frozen["content"], str):
                raise PermissionDenied("unsupported file write arguments")
            data = frozen["content"].encode("utf-8")
            if len(data) > 1024 * 1024:
                raise PermissionDenied("file data exceeds the supervised limit")
        async with self._capacity:
            if self._closed:
                raise PermissionDenied("file supervisor is closed")
            bound = copy_kernel = copy_handle = owned = claim = None
            started, stopped, stop_recorded = False, False, False
            task = asyncio.current_task()
            self._tasks.add(task)
            # Input and result artifacts belong to the protected service, not
            # the business filesystem. They never enter diagnostics or URLs.
            folder = Path(tempfile.mkdtemp(prefix="file-", dir=self._private))
            try:
                bound = open_bound_read(scope) if data is None else open_bound_write(scope)
                copy_kernel, copy_handle = inheritable_handle_copy(bound._handles[-1])
                action = "read" if isinstance(bound, BoundWindowsRead) else (
                    "create" if isinstance(bound, BoundWindowsCreate) else "write")
                result_path = folder / "result.json"
                plan = {
                    "format": "hashi-file-helper-v1", "action": action, "handles": [copy_handle],
                    "directory": getattr(bound, "_directory", ""), "name": getattr(bound, "_name", ""),
                    "data": base64.b64encode(data or b"").decode("ascii"), "result": str(result_path),
                }
                encoded = json.dumps(plan, separators=(",", ":"), allow_nan=False).encode("utf-8")
                plan_path = folder / "plan.json"
                with plan_path.open("xb") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                if sha256(self._entry.read_bytes()).hexdigest() != self._entry_digest:
                    raise PermissionDenied("qualified file helper changed")
                owned = launch_suspended(
                    executable=self._executable,
                    arguments=["-I", "-S", str(self._entry), str(plan_path), sha256(encoded).hexdigest()],
                    cwd=folder, environment=self._environment, expected_user_sid=self._sid,
                    executable_digest=self._executable_digest, inherited_handles=(copy_handle,),
                )
                self._active[operation_id] = owned
                with self._guard(context):
                    if self._closed or self._validate(context, scope) is not True:
                        raise PermissionDenied("file execution policy changed before start")
                    claim = self._authority.prepare(
                        context, operation_id, descriptor.arguments_digest, descriptor.scopes,
                    )
                    # ResumeThread is the only effect inside the SQL barrier.
                    # File I/O and the process wait occur after both locks close.
                    self._authority.dispatch_guarded(
                        context, claim.claim_id, operation_id=operation_id,
                        arguments_digest=descriptor.arguments_digest, scopes=descriptor.scopes,
                        effect=owned.start,
                    )
                    started = True
                while not owned.poll():
                    self._authorized(context, claim, operation_id, descriptor)
                    await asyncio.sleep(.1)
                exit_code = owned.exit_code()
                owned.stop(timeout_ms=4500)
                stopped = True
                if exit_code or not result_path.is_file() or result_path.stat().st_size > 24 * 1024 * 1024:
                    raise OSError("trusted file result is unavailable")
                result = json.loads(result_path.read_bytes())
                if not isinstance(result, dict) or set(result) != {"outcome", "data"} or result["outcome"] != "succeeded":
                    raise OSError("trusted file outcome requires reconciliation")
                if data is None:
                    payload = _render_read(scope.resource, base64.b64decode(result["data"], validate=True),
                                           max(1, offset), max(1, min(500, limit)))
                else:
                    payload = f"OK: wrote {len(frozen['content'])} characters to {scope.resource}"
                receipt = self._store.put(context, operation_id, descriptor, outcome="succeeded", payload=payload)
                self._authority.finish(context, claim.claim_id, outcome=receipt.outcome,
                                        result_digest=receipt.result_digest)
                stop = self._stop_receipt(context, claim.claim_id, {
                    "process": owned.observation.process_id, "arguments_digest": descriptor.arguments_digest,
                    "plan_digest": sha256(encoded).hexdigest(), "job_stopped": True,
                })
                self._authority.confirm_resource_stopped(context, claim.claim_id, receipt_reference=stop)
                stop_recorded = True
                return receipt
            except BaseException:
                # A failed ResumeThread may already have started. Inspect the
                # authoritative operation; never release once by a local flag.
                if claim is not None:
                    state = self._authority.operation_state(operation_id)
                    if state == "started":
                        self._authority.finish(context, claim.claim_id, outcome="unknown", result_digest=None)
                    elif state == "prepared":
                        self._authority.cancel_prepared(context, claim.claim_id)
                raise
            finally:
                if owned is not None and not stopped:
                    owned.stop(timeout_ms=4500)
                    stopped = True
                if bound is not None:
                    bound.close()
                if copy_handle is not None and not copy_kernel.CloseHandle(copy_handle):
                    raise OSError("file transfer handle cleanup is unverified")
                if claim is not None and stopped and not stop_recorded:
                    stop = self._stop_receipt(context, claim.claim_id, {
                        "process": owned.observation.process_id, "job_stopped": True,
                        "arguments_digest": descriptor.arguments_digest,
                    })
                    self._authority.confirm_resource_stopped(context, claim.claim_id, receipt_reference=stop)
                self._active.pop(operation_id, None)
                self._tasks.discard(task)
                # Preserve failed plans/results for reconciliation; cleanup is
                # not evidence that a business write did or did not happen.
                if started and self._authority.operation_state(operation_id) == "succeeded":
                    for entry in folder.iterdir():
                        entry.unlink()
                    folder.rmdir()

    async def recover(self, context, operation_id, descriptor):
        with self._guard(context):
            return self._store.get(context, operation_id, descriptor)

    async def close(self):
        self._closed = True
        for owned in tuple(self._active.values()):
            owned.stop(timeout_ms=4500)
        tasks = tuple(task for task in self._tasks if task is not asyncio.current_task())
        for task in tasks:
            task.cancel()
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=5)
            if pending:
                raise OSError("file supervisor settlement remains unverified")
        return not self._active
