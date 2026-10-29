"""Killable fixed Windows file helpers with held objects and final start fencing.

Only installed trusted code constructs this owner. No execution RPC accepts
these paths, environment, signer or policy callback from an agent.
"""
from __future__ import annotations

import asyncio
import base64
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
from uuid import uuid4

from orchestrator.permissions.authority import PermissionDenied
from orchestrator.permissions.descriptors import describe_tool
from orchestrator.permissions.pao_run_fence import PaoRunFence
from orchestrator.permissions.windows_file_binding import (
    BoundWindowsCreate, FileBindingError, FileBindingCleanupFailed, open_bound_read, open_bound_write, open_bound_directory, _path_parts,
)
from orchestrator.permissions.file_operations import FILE_TOOLS, search_options
from orchestrator.permissions.network_worker import NETWORK_TOOLS, RequestNotSent, prepare as prepare_network
from orchestrator.permissions.file_results import render_helper_result
from orchestrator.permissions.windows_process import (
    WindowsProcessCleanupFailed, inheritable_handle_copy, launch_suspended,
)


class WindowsFileProcessSupervisor:
    def __init__(self, authority, *, cwd, read_roots, result_store, issue_stop, run_guard,
                 helper_executable, helper_entry, executable_digest, entry_digest,
                 private_root, environment, expected_user_sid, validate_resource, physical_journal=None, protected_paths=(), network_options=None, runner_client=None, parent_job=None):
        if (not isinstance(run_guard, PaoRunFence) or not callable(issue_stop)
                or not callable(validate_resource) or os.name != "nt"):
            raise ValueError("a native trusted file supervisor is required")
        self._authority, self._guard, self._store = authority, run_guard, result_store
        self._stop_receipt, self._validate = issue_stop, validate_resource
        self._journal = physical_journal
        self._cwd, self._roots = Path(cwd), tuple(Path(p) for p in read_roots)
        self._executable, self._entry = Path(helper_executable), Path(helper_entry)
        self._executable_digest, self._entry_digest = executable_digest, entry_digest
        self._private = Path(private_root)
        if (not all(p.is_absolute() for p in (self._cwd, self._executable, self._entry, self._private))
                or not self._private.is_dir()
                or sha256(self._entry.read_bytes()).hexdigest() != entry_digest):
            raise ValueError("a qualified helper and protected artifact root are required")
        self._environment, self._sid = dict(environment), expected_user_sid
        self._protected_paths = tuple(str(path) for path in protected_paths)
        self._network_options = dict(network_options or {})
        self._runner, self._parent_job = runner_client, parent_job
        self.credential_names = tuple(sorted(self._network_options.get("credentials", {})))
        self._capacity = asyncio.Semaphore(1)
        self._active, self._closed = {}, False
        self._tasks = set()

    TOOL_NAMES = FILE_TOOLS | NETWORK_TOOLS | {"run_script"}

    def supports(self, tool_name):
        if tool_name == "run_script":
            return (self._runner is not None and self._parent_job is not None and self._journal is not None
                    and self._runner.scripts_qualified())
        return tool_name in self.TOOL_NAMES

    def validate_plan(self, descriptor, arguments):
        try:
            for scope in descriptor.scopes:
                if scope.kind == "file":
                    _path_parts(scope, frozenset())
        except ValueError:
            return False
        if descriptor.tool_name == "run_script":
            from orchestrator.permissions.script_protocol import validate

            try:
                validate(arguments)
            except ValueError:
                return False
            return self.supports("run_script")
        if descriptor.tool_name in NETWORK_TOOLS:
            try:
                prepare_network(descriptor.tool_name, arguments, self._network_options)
            except RequestNotSent:
                return False
        return True

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
        if (actual != descriptor or not descriptor.scopes or len(descriptor.scopes) > 32
                or (descriptor.tool_name not in ({"file_search", "run_script"} | NETWORK_TOOLS) and len(descriptor.scopes) != 1)
                or not self.supports(descriptor.tool_name)):
            raise PermissionDenied("file plan differs from its reviewed descriptor")
        if any(self._validate(context, item) is not True for item in descriptor.scopes):
            raise PermissionDenied("file is outside the installed execution policy")
        if descriptor.tool_name == "run_script":
            data = None  # The private Runner plan needs the reserved helper Job name below.
        elif descriptor.tool_name in NETWORK_TOOLS:
            data = json.dumps(prepare_network(descriptor.tool_name, frozen, self._network_options),
                              ensure_ascii=False).encode("utf-8")
        elif descriptor.tool_name == "file_read":
            if set(frozen) - {"path", "offset", "limit"}:
                raise PermissionDenied("unsupported file read arguments")
            offset, limit = frozen.get("offset", 1), frozen.get("limit", 500)
            if type(offset) is not int or type(limit) is not int:
                raise PermissionDenied("invalid read range")
            data = None
        elif descriptor.tool_name in {"file_write", "apply_patch"}:
            field = "patch" if descriptor.tool_name == "apply_patch" else "content"
            if set(frozen) != {"path", field} or not isinstance(frozen[field], str):
                raise PermissionDenied("unsupported file mutation arguments")
            data = frozen[field].encode("utf-8")
            if len(data) > 1024 * 1024:
                raise PermissionDenied("file data exceeds the supervised limit")
        else:
            if descriptor.tool_name == "file_search":
                search_options(frozen)
            elif (set(frozen) - {"path", "limit"} or type(frozen.get("limit", 200)) is not int
                    or not 1 <= frozen.get("limit", 200) <= 500):
                raise PermissionDenied("unsupported directory list arguments")
            data = json.dumps({"arguments": frozen, "roots": [item.resource for item in descriptor.scopes],
                               "protected": self._protected_paths}, ensure_ascii=False).encode("utf-8")
        async with self._capacity:
            if self._closed:
                raise PermissionDenied("file supervisor is closed")
            bound = copy_kernel = copy_handle = owned = claim = None
            started = False
            task = asyncio.current_task()
            self._tasks.add(task)
            # Input and result artifacts belong to the protected service, not
            # the business filesystem. They never enter diagnostics or URLs.
            folder = Path(tempfile.mkdtemp(prefix="file-", dir=self._private))
            from orchestrator.permissions.activity import TOOL_CALL

            resource = {"lock": asyncio.Lock(), "context": context, "descriptor": descriptor,
                        "script_may_exist": False, "tool_call_id": TOOL_CALL.get()}
            self._active[operation_id] = resource
            try:
                bounds, transfers = [], []
                resource.update(bounds=bounds, transfers=transfers)
                bound_scopes = tuple(item for item in descriptor.scopes if item.kind == "file")
                for item in (() if descriptor.tool_name in NETWORK_TOOLS else bound_scopes):
                    bound = (open_bound_directory(item) if descriptor.tool_name in {"file_list", "file_search"} else
                             open_bound_read(item, exclusive=descriptor.tool_name == "run_script")
                             if descriptor.tool_name in {"file_read", "run_script"} else open_bound_write(item))
                    bounds.append(bound)
                    copy_kernel, copy_handle = inheritable_handle_copy(bound._handles[-1])
                    transfers.append((copy_kernel, copy_handle))
                bound = bounds[0] if bounds else None
                action = ({"file_read": "read", "file_list": "list", "file_search": "search",
                           "http_request": "http", "web_fetch": "http", "run_script": "script"}.get(descriptor.tool_name)
                          or (("patch_" if descriptor.tool_name == "apply_patch" else "")
                              + ("create" if isinstance(bound, BoundWindowsCreate) else "write")))
                result_path = folder / "result.json"
                journal = getattr(self, "_journal", None)
                preassigned = uuid4().hex if journal is not None else None
                if descriptor.tool_name == "run_script":
                    from orchestrator.permissions.models import normalize_scope
                    from orchestrator.permissions.windows_job_security import private_job_name
                    from orchestrator.permissions.wire import encode

                    indices = [next(index for index, scope in enumerate(bound_scopes) if scope.resource == normalize_scope(
                        "file", item["path"], ["read"], cwd=self._cwd).resource) for item in frozen.get("inputs", [])]
                    data = json.dumps({
                        "arguments": frozen, "cwd": str(self._cwd), "descriptor": asdict(descriptor),
                        "binding": encode(self._guard.binding), "operation_id": operation_id,
                        "runner": self._runner.script_helper_descriptor(), "parent_job_name": private_job_name(preassigned),
                        "input_indices": indices, "result": str(result_path),
                    }, ensure_ascii=False).encode("utf-8")
                plan = {
                    "format": "hashi-file-helper-v1", "action": action, "handles": [handle for _, handle in transfers],
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
                claim_options, job_name = {}, None
                if journal is not None:
                    job_name = journal.reserve(
                        context=context, operation_id=operation_id, descriptor=descriptor,
                        arguments=frozen, cwd=self._cwd, claim_id=preassigned, owner_sid=self._sid,
                        plan_path=plan_path, plan_digest=sha256(encoded).hexdigest(),
                        max_processes=2 if descriptor.tool_name == "run_script" else 1,
                    )
                    resource["journal_claim"] = preassigned
                    claim_options["prepared_claim_id"] = preassigned
                owned = launch_suspended(
                    executable=self._executable,
                    arguments=["-I", "-S", "-B", str(self._entry), str(plan_path), sha256(encoded).hexdigest()],
                    cwd=folder, environment=self._environment, expected_user_sid=self._sid,
                    executable_digest=self._executable_digest, inherited_handles=tuple(handle for _, handle in transfers),
                    max_processes=2 if descriptor.tool_name == "run_script" else 1, job_name=job_name,
                    parent_jobs=(int(self._parent_job.handle),) if self._parent_job is not None else (),
                    job_assignees=(self._runner.runner_sid,) if descriptor.tool_name == "run_script" else (),
                )
                resource["process"] = owned
                if journal is not None:
                    journal.observe_process(preassigned, owned.observation, max_processes=owned._job.max_processes)
                with self._guard(context):
                    if self._closed or any(self._validate(context, item) is not True for item in descriptor.scopes):
                        raise PermissionDenied("file execution policy changed before start")
                    claim = self._authority.prepare(
                        context, operation_id, descriptor.arguments_digest, descriptor.scopes, **claim_options,
                    )
                    resource["claim"] = claim
                    # ResumeThread is the only effect inside the SQL barrier.
                    # File I/O and the process wait occur after both locks close.
                    def resume():
                        if descriptor.tool_name == "run_script":
                            resource["script_may_exist"] = True
                        owned.start()

                    self._authority.dispatch_guarded(
                        context, claim.claim_id, operation_id=operation_id,
                        arguments_digest=descriptor.arguments_digest, scopes=descriptor.scopes,
                        effect=resume,
                    )
                    started = True
                while not owned.poll():
                    self._authorized(context, claim, operation_id, descriptor)
                    progress = folder / "progress.json"
                    if descriptor.tool_name == "run_script" and progress.is_file() and progress.stat().st_size < 65536:
                        encoded_progress = progress.read_bytes()
                        progress_digest = sha256(encoded_progress).hexdigest()
                        if progress_digest != resource.get("progress_digest"):
                            from adapters.stream_events import StreamEvent
                            from orchestrator.permissions.ipc_rpc import emit_private_event

                            metadata = json.loads(encoded_progress)
                            await emit_private_event(StreamEvent(kind="tool_activity", metadata={
                                **metadata, "tool_call_id": resource["tool_call_id"], "operation_type": "run_script",
                            }))
                            resource["progress_digest"] = progress_digest
                    await asyncio.sleep(.1)
                exit_code = owned.exit_code()
                await asyncio.to_thread(owned.stop, timeout_ms=4500)
                if exit_code or not result_path.is_file() or result_path.stat().st_size > 24 * 1024 * 1024:
                    raise OSError("trusted file result is unavailable")
                result = json.loads(result_path.read_bytes())
                if not isinstance(result, dict) or set(result) != {"outcome", "data"} or result["outcome"] not in {"succeeded", "failed"}:
                    raise OSError("trusted file outcome requires reconciliation")
                payload = render_helper_result(descriptor, frozen, result)
                receipt = self._store.put(context, operation_id, descriptor, outcome=result["outcome"], payload=payload)
                self._authority.finish(context, claim.claim_id, outcome=receipt.outcome,
                                        result_digest=receipt.result_digest)
                resource["plan_digest"] = sha256(encoded).hexdigest()
                return receipt
            except BaseException as error:
                if isinstance(error, FileBindingCleanupFailed):
                    resource["binding_failure"] = error
                if isinstance(error, WindowsProcessCleanupFailed):
                    resource["launch_failure"] = error
                if (isinstance(error, FileBindingError) and claim is None and owned is None
                        and not resource.get("journal_claim")):
                    await self._cleanup(operation_id, resource)
                    with self._guard(context):
                        receipt = self._store.put(context, operation_id, descriptor, outcome="failed",
                                                  payload="File could not be opened safely; no action started.")
                        self._authority.reject_before_start(context, operation_id, descriptor.arguments_digest,
                                                            descriptor.scopes, result_digest=receipt.result_digest)
                    resource["prestart_rejected"] = True
                    return receipt
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
                try:
                    await self._cleanup(operation_id, resource)
                finally:
                    self._tasks.discard(task)
                # Preserve failed plans/results for reconciliation; cleanup is
                # not evidence that a business write did or did not happen.
                if (started or resource.get("prestart_rejected")) and self._authority.operation_state(operation_id) in {"succeeded", "failed"}:
                    for entry in folder.iterdir():
                        entry.unlink()
                    folder.rmdir()

    async def recover(self, context, operation_id, descriptor):
        with self._guard(context):
            return self._store.get(context, operation_id, descriptor)

    async def _cleanup(self, operation_id, resource):
        # The active record retains every owned handle on an unverified stop.
        # Native waits run off the service loop so revocation/other Jobs remain
        # responsive. Concurrent shutdown and Tool completion share this owner.
        async with resource["lock"]:
            for name in ("launch_failure", "binding_failure"):
                failure = resource.get(name)
                if failure is not None:
                    cleanup = asyncio.create_task(asyncio.to_thread(failure.retry_cleanup))
                    await asyncio.shield(cleanup)
                    resource.pop(name, None)
            owned = resource.get("process")
            if owned is not None:
                stop = asyncio.create_task(asyncio.to_thread(owned.stop, timeout_ms=4500))
                try:
                    await asyncio.shield(stop)
                except asyncio.CancelledError:
                    done, _ = await asyncio.wait({stop}, timeout=5)
                    if stop not in done:
                        raise OSError("native file stop remains unverified") from None
                    stop.result()
                    raise
            for bound in tuple(resource.get("bounds", ())):
                bound.close()
                resource["bounds"].remove(bound)
            for kernel, handle in tuple(resource.get("transfers", ())):
                if not kernel.CloseHandle(handle):
                    raise OSError("file transfer handle cleanup is unverified")
                resource["transfers"].remove((kernel, handle))
            if resource.get("script_may_exist"):
                if self._runner is None or await self._runner.stop_script(self._guard.binding, operation_id) is not True:
                    raise OSError("script profile retirement remains unverified")
                resource["script_may_exist"] = False
            bound = resource.get("bound")
            if bound is not None:
                bound.close()
                resource["bound"] = None
            transfer = resource.get("transfer")
            if transfer is not None:
                if not resource["transfer_kernel"].CloseHandle(transfer):
                    raise OSError("file transfer handle cleanup is unverified")
                resource["transfer"] = None
            claim = resource.get("claim")
            if claim is not None and not resource.get("stop_recorded"):
                receipt = self._stop_receipt(resource["context"], claim.claim_id, {
                    "process": owned.observation.process_id, "job_stopped": True,
                    "arguments_digest": resource["descriptor"].arguments_digest,
                })
                self._authority.confirm_resource_stopped(
                    resource["context"], claim.claim_id, receipt_reference=receipt,
                )
                resource["stop_recorded"] = True
            journal = getattr(self, "_journal", None)
            if journal is not None and resource.get("journal_claim"):
                journal.stopped(resource["journal_claim"], observed_stop=owned is not None)
                if claim is not None and self._authority.operation_state(operation_id) in {"succeeded", "failed", "waiting"}:
                    journal.finished(resource["journal_claim"])
            if self._active.get(operation_id) is resource:
                self._active.pop(operation_id)

    async def close(self):
        self._closed = True
        tasks = tuple(task for task in self._tasks if task is not asyncio.current_task())
        for task in tasks:
            task.cancel()
        cleanup = [asyncio.create_task(self._cleanup(op, resource))
                   for op, resource in tuple(self._active.items())]
        all_tasks = tasks + tuple(cleanup)
        if all_tasks:
            done, pending = await asyncio.wait(all_tasks, timeout=5)
            errors = []
            for task in done:
                try:
                    task.result()
                except asyncio.CancelledError:
                    pass
                except Exception as error:
                    errors.append(error)
            if pending or errors or self._active:
                raise OSError("file supervisor settlement remains unverified")
        return not self._active
