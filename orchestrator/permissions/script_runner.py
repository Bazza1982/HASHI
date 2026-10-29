"""Runner-owned offline scripts; no user Python runs in a trusted process."""
from __future__ import annotations

import asyncio
import base64
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path

from orchestrator.permissions.authority import PermissionDenied
from orchestrator.permissions.descriptors import ToolDescriptor, describe_tool
from orchestrator.permissions.ipc_rpc import current_peer, emit_private_event
from adapters.stream_events import StreamEvent
from orchestrator.permissions.models import Scope, normalize_scope
from orchestrator.permissions.script_protocol import TOOL_NAME, digest, paths, validate
from orchestrator.permissions.windows_container import TaskContainerProfile, protect_task_scratch
from orchestrator.permissions.windows_file_binding import BoundWindowsDirectory, open_bound_directory, open_bound_read
from orchestrator.permissions.windows_jobs import WindowsJob, ParentJobCleanupFailed
from orchestrator.permissions.windows_process import WindowsProcessCleanupFailed, launch_suspended


def unpack_plan(raw):
    if not isinstance(raw, dict) or set(raw) != {"arguments", "snapshots", "cwd", "descriptor"}:
        raise PermissionDenied("invalid fixed script plan")
    arguments = validate(raw["arguments"])
    cwd = Path(raw["cwd"])
    if not cwd.is_absolute():
        raise PermissionDenied("script workzone identity is missing")
    expected = describe_tool(TOOL_NAME, arguments, cwd=cwd, read_roots=())
    value = raw["descriptor"]
    descriptor = ToolDescriptor(value["tool_name"], tuple(Scope(**item) for item in value["scopes"]),
                                value["arguments_digest"], value["coverage"], tuple(value["limitations"]))
    if descriptor != expected:
        raise PermissionDenied("script descriptor changed")
    snapshots = raw["snapshots"]
    if not isinstance(snapshots, list) or len(snapshots) != len(arguments["inputs"]):
        raise PermissionDenied("script input snapshots are missing")
    total, decoded = 0, []
    for declared, item in zip(arguments["inputs"], snapshots):
        if not isinstance(item, dict) or set(item) != {"name", "data", "sha256"} or item["name"] != declared["name"]:
            raise PermissionDenied("script input snapshot identity changed")
        data = base64.b64decode(item["data"], validate=True)
        total += len(data)
        if total > 4 * 1024 * 1024 or sha256(data).hexdigest() != item["sha256"]:
            raise PermissionDenied("script input snapshot exceeds its bound or changed")
        decoded.append((item["name"], data))
    return arguments, descriptor, decoded


class RestrictedScriptRuns:
    def __init__(self, runner, results, *, memory_bytes=1024**3, scratch_bytes=64 * 1024 * 1024):
        if (type(memory_bytes) is not int or not 64 * 1024 * 1024 <= memory_bytes <= 4 * 1024**3
                or type(scratch_bytes) is not int or not 1024 * 1024 <= scratch_bytes <= 128 * 1024 * 1024):
            raise ValueError("invalid installed script resource limits")
        self.runner, self.results = runner, results
        self.memory_bytes, self.scratch_bytes = memory_bytes, scratch_bytes
        self._owned, self._locks, self._closing = {}, {}, False
        self.entry = runner.capsule.root / "app/orchestrator/permissions/script_worker.py"
        self.entry_digest = runner.capsule.file_digest(self.entry)

    def _identity(self, binding, operation_id):
        from orchestrator.permissions.runner_records import binding_digest

        return binding_digest(binding), operation_id

    def _verify(self):
        self.runner._verify_images()
        if sha256(self.entry.read_bytes()).hexdigest() != self.entry_digest:
            raise PermissionDenied("the fixed script entry changed")

    async def prepare(self, *, binding, operation_id, parent_job_name, root_job_name, plan):
        arguments, descriptor, snapshots = unpack_plan(plan)
        identity = self._identity(binding, operation_id)
        if binding.context.instance_id != self.runner.instance_id:
            raise PermissionDenied("script belongs to another instance")
        peer = current_peer()
        if peer is None or peer.user_sid != self.runner.safety_sid or peer.appcontainer_sid is not None:
            raise PermissionDenied("the script has no actual Safety helper peer")
        async with self._locks.setdefault(identity, asyncio.Lock()):
            if self._closing:
                raise PermissionDenied("the script Runner is closing")
            self._verify()
            existing = self._owned.get(identity)
            if existing is not None:
                if (existing["plan_digest"] != digest(plan) or existing["started"]
                        or existing["peer"] != peer.process_id):
                    raise PermissionDenied("script was already started or rebound")
                return asdict(existing["process"].observation)
            reserved = self.runner.records.reserve(binding, operation_id=operation_id)
            scratch = Path(reserved["scratch"])
            record = {"binding": binding, "operation_id": operation_id, "descriptor": descriptor,
                      "arguments": arguments, "plan_digest": digest(plan), "scratch": scratch, "profile": None,
                      "process": None, "parents": [], "started": False, "lock": asyncio.Lock(), "peer": peer.process_id}
            self._owned[identity] = record
            try:
                root = WindowsJob.open_parent(root_job_name, self.runner.safety_sid)
                record["parents"].append(root)
                parent = WindowsJob.open_parent(parent_job_name, self.runner.safety_sid, max_processes=2)
                record["parents"].append(parent)
                if not parent.contains_pid(peer.process_id) or not root.contains_pid(peer.process_id):
                    raise PermissionDenied("the actual script helper is outside its reserved Jobs")
                profile = TaskContainerProfile(reserved["profile"])
                record["profile"] = profile
                scratch.mkdir(exist_ok=False)
                protect_task_scratch(scratch, runner_sid=self.runner.runner_sid, appcontainer_sid=profile.sid)
                source, inputs, outputs, _, _ = paths(scratch)
                inputs.mkdir()
                outputs.mkdir()
                source.write_bytes(arguments["script"].encode("utf-8"))
                for name, data in snapshots:
                    (inputs / name).write_bytes(data)
                process = launch_suspended(
                    executable=self.runner.executable,
                    arguments=["-I", "-S", "-B", str(self.entry), str(scratch)],
                    cwd=scratch, environment={**self.runner.environment, "TMP": str(scratch), "TEMP": str(scratch)},
                    expected_user_sid=self.runner.runner_sid, executable_digest=self.runner.executable_digest,
                    appcontainer_sid=profile.sid, require_lpac=True, max_processes=1, memory_bytes=self.memory_bytes,
                    job_name=reserved["job_name"], parent_jobs=(int(root.handle), int(parent.handle)),
                )
                record["process"] = process
                self.runner.records.observe(binding, process.observation, operation_id=operation_id)
                return asdict(process.observation)
            except BaseException as error:
                if isinstance(error, ParentJobCleanupFailed):
                    record["parents"].append(error.parent)
                if isinstance(error, WindowsProcessCleanupFailed):
                    record["launch_failure"] = error
                await self.stop(binding=binding, operation_id=operation_id)
                raise
            finally:
                for parent in tuple(record["parents"]):
                    parent.close()
                    record["parents"].remove(parent)

    def _within_scratch_limit(self, record):
        root = record["scratch"]
        scope = normalize_scope("file", str(root), ["list", "read"], cwd=root)
        bounds = [open_bound_directory(scope)]
        queue, total, count = [(bounds[0], str(root))], 0, 0
        try:
            while queue:
                bound, directory = queue.pop(0)
                for item in bound.entries():
                    count += 1
                    if item["reparse"] or count > 4096:
                        return False
                    if item["directory"]:
                        handle = bound._api.open_child(bound._handles[-1], item["name"], directory=True)
                        child = BoundWindowsDirectory(bound._api, [handle])
                        bounds.append(child)
                        target = str(Path(directory) / item["name"])
                        bound._api.check(handle, target, directory=True)
                        queue.append((child, target))
                    else:
                        total += item["size"]
                        if total > self.scratch_bytes:
                            return False
            return True
        finally:
            for bound in reversed(bounds):
                bound.close()

    @staticmethod
    def _small_file(path, maximum):
        scope = normalize_scope("file", str(path), ["read"], cwd=path.parent)
        with open_bound_read(scope) as bound:
            return bound.read(max_bytes=maximum)

    def _collect(self, record, exit_code, limited):
        scratch = record["scratch"]
        _, _, outputs, stdout, stderr = paths(scratch)
        result = {"state": "resource_limit" if limited else "completed", "exit_code": exit_code,
                  "stdout": "", "stderr": "", "outputs": [], "temporary_outputs": True}
        for name, path, maximum in (("stdout", stdout, 128 * 1024), ("stderr", stderr, 64 * 1024)):
            try:
                result[name] = self._small_file(path, maximum).decode("utf-8", errors="replace")
            except (OSError, ValueError):
                result[name] = "[output unavailable or above its return limit]"
        total = 0
        for name in record["arguments"]["outputs"]:
            try:
                data = self._small_file(outputs / name, 512 * 1024)
                total += len(data)
                if total > 512 * 1024:
                    raise ValueError("temporary output bound")
                result["outputs"].append({"name": name, "data": base64.b64encode(data).decode("ascii"),
                                           "sha256": sha256(data).hexdigest(), "size": len(data)})
            except (OSError, ValueError):
                result["outputs"].append({"name": name, "error": "temporary output unavailable or above its return limit"})
        payload = json.dumps(result, ensure_ascii=False)
        if len(payload.encode("utf-8")) > 900000:
            result["stdout"], result["stderr"] = "[output clipped to keep artifacts bounded]", ""
            payload = json.dumps(result, ensure_ascii=False)
        return payload

    async def run(self, *, binding, operation_id):
        identity = self._identity(binding, operation_id)
        record = self._owned.get(identity)
        peer = current_peer()
        if record is None or record["binding"] != binding or peer is None or peer.process_id != record["peer"]:
            raise PermissionDenied("no prepared script belongs to this actual helper")
        try:
            async with record["lock"]:
                if record["started"] or self._closing:
                    raise PermissionDenied("a script can start only once")
                record["started"] = True
                record["process"].start()
            limited = False
            activity_at = 0.0
            while not record["process"].poll():
                now = asyncio.get_running_loop().time()
                if now - activity_at >= 1:
                    await emit_private_event(StreamEvent(kind="tool_activity", metadata={
                        "state": "running", "liveness": True, "progress": False, "counters": {},
                    }))
                    activity_at = now
                if not await asyncio.to_thread(self._within_scratch_limit, record):
                    limited = True
                    break
                await asyncio.sleep(.1)
            exit_code = None if limited else record["process"].exit_code()
            await asyncio.to_thread(record["process"].stop, timeout_ms=4500)
            outcome = "succeeded" if exit_code == 0 and not limited else "failed"
            record["collect_outcome"] = outcome
            record["collecting"] = asyncio.create_task(asyncio.to_thread(self._collect, record, exit_code, limited))
            payload = await asyncio.shield(record["collecting"])
            receipt = self.results.put(binding.context, operation_id, record["descriptor"], outcome=outcome, payload=payload)
            return {"outcome": receipt.outcome, "payload": receipt.payload, "result_digest": receipt.result_digest}
        finally:
            await self.stop(binding=binding, operation_id=operation_id)

    async def stop(self, *, binding, operation_id):
        identity = self._identity(binding, operation_id)
        record = self._owned.get(identity)
        if record is None:
            await asyncio.to_thread(self.runner.records.retire, binding, operation_id=operation_id)
            return {"stopped": True}
        async with record["lock"]:
            if record.get("launch_failure") is not None:
                await asyncio.to_thread(record["launch_failure"].retry_cleanup)
                record.pop("launch_failure")
            process = record["process"]
            if process is not None:
                await asyncio.to_thread(process.stop, timeout_ms=4500)
            collecting = record.get("collecting")
            if collecting is not None:
                done, pending = await asyncio.wait({collecting}, timeout=5)
                if pending:
                    raise OSError("script result collection remains owned")
                payload = collecting.result()
                self.results.put(binding.context, operation_id, record["descriptor"],
                                 outcome=record["collect_outcome"], payload=payload)
            for parent in tuple(record["parents"]):
                parent.close()
                record["parents"].remove(parent)
            await asyncio.to_thread(self.runner.records.retire, binding, operation_id=operation_id,
                                    process_stopped=process is not None)
            if record["profile"] is not None:
                record["profile"]._closed = True
            self._owned.pop(identity, None)
            return {"stopped": True}

    async def result(self, *, binding, operation_id, descriptor):
        raw = descriptor
        descriptor = ToolDescriptor(raw["tool_name"], tuple(Scope(**item) for item in raw["scopes"]),
                                    raw["arguments_digest"], raw["coverage"], tuple(raw["limitations"]))
        if descriptor.tool_name != TOOL_NAME:
            raise PermissionDenied("the requested result is not a script")
        await self.stop(binding=binding, operation_id=operation_id)
        receipt = self.results.get(binding.context, operation_id, descriptor)
        return ({"outcome": receipt.outcome, "payload": receipt.payload, "result_digest": receipt.result_digest}
                if receipt is not None else None)

    async def stop_binding(self, binding):
        results = await asyncio.gather(*(self.stop(binding=binding, operation_id=record["operation_id"])
                                        for record in tuple(self._owned.values()) if record["binding"] == binding),
                                       return_exceptions=True)
        if any(isinstance(result, BaseException) for result in results):
            raise OSError("script resources remain unverified")
        # Startup-recovered records have no in-memory owner.
        for _, value in self.runner.records.pending():
            if value["binding"] == asdict(binding) and value.get("operation_id") is not None:
                await self.stop(binding=binding, operation_id=value["operation_id"])
        return True
