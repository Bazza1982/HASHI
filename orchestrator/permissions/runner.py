"""Independent nonadmin Runner for a fixed, sealed HERV3 host."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from hashlib import sha256
from multiprocessing import Pipe
from pathlib import Path

from orchestrator.permissions.authority import PermissionDenied
from orchestrator.permissions.ipc_rpc import emit_private_event
from orchestrator.permissions.runner_records import RunnerRecords, binding_digest as _binding_digest
from orchestrator.permissions.windows_container import TaskContainerProfile, protect_task_scratch
from orchestrator.permissions.windows_ipc import JsonPipe, PipePeerPolicy
from orchestrator.permissions.windows_process import WindowsProcessCleanupFailed, inheritable_handle_copy, launch_suspended
from orchestrator.permissions.windows_security import require_nonadmin_identity, require_private_path
from orchestrator.permissions.windows_jobs import WindowsJob, ParentJobCleanupFailed
from orchestrator.permissions.wire import decode


class RestrictedHerRunner:
    METHODS = frozenset({"health", "prepare_host", "run_host", "stop_host",
                         "prepare_script", "run_script", "stop_script", "script_result"})

    def __init__(self, *, runner_sid, safety_sid, worker_sid, private_root, executable,
                 entry, executable_digest, entry_digest, environment, capsule_roots, instance_id, capsule=None, script_results=None,
                 script_memory_bytes=1024**3, script_scratch_bytes=64 * 1024 * 1024):
        if not isinstance(instance_id, str) or not instance_id:
            raise ValueError("an installed instance identity is required")
        self.instance_id, self.capsule = instance_id, capsule
        self.identity = require_nonadmin_identity(runner_sid, excluded_sids=(safety_sid, worker_sid))
        if safety_sid == worker_sid:
            raise PermissionDenied("L2 Safety must be independent of the agent account")
        self.runner_sid, self.safety_sid, self.worker_sid = runner_sid, safety_sid, worker_sid
        self.private = Path(private_root)
        self.executable, self.entry = Path(executable), Path(entry)
        self.executable_digest, self.entry_digest = executable_digest, entry_digest
        self.environment = dict(environment)
        require_private_path(self.private, trusted_sids=(runner_sid,))
        for root in capsule_roots:
            require_private_path(Path(root), trusted_sids=(runner_sid, safety_sid), read_only_public=True)
        self._owned, self._locks, self._closing = {}, {}, False
        self.records = RunnerRecords(self.private, instance_id=self.instance_id, runner_sid=self.runner_sid)
        self._verify_images()
        self._scripts = None
        if script_results is not None:
            if capsule is None:
                raise ValueError("scripts require the complete sealed runtime capsule")
            from orchestrator.permissions.script_runner import RestrictedScriptRuns

            self._scripts = RestrictedScriptRuns(self, script_results, memory_bytes=script_memory_bytes,
                                                 scratch_bytes=script_scratch_bytes)

    def _verify_images(self):
        if (sha256(self.executable.read_bytes()).hexdigest() != self.executable_digest
                or sha256(self.entry.read_bytes()).hexdigest() != self.entry_digest):
            raise PermissionDenied("the sealed HER host image changed")

    def health(self):
        self._verify_images()
        if self._scripts is not None:
            self._scripts._verify()
        return {"state": "ready", "identity": self.identity, "instance_id": self.instance_id, "safety_sid": self.safety_sid,
                "worker_sid": self.worker_sid, "executable_digest": self.executable_digest,
                "entry_digest": self.entry_digest, "runner_contract": 2,
                "script_contract": 1 if self._scripts is not None else 0,
                "capsule_digest": self.capsule.digest if self.capsule is not None else None}

    async def prepare_host(self, *, binding, parent_job_name):
        identity = _binding_digest(binding)
        if binding.context.instance_id != self.instance_id:
            raise PermissionDenied("host belongs to another installed instance")
        async with self._locks.setdefault(identity, asyncio.Lock()):
            if self._closing:
                raise PermissionDenied("the Runner is closing")
            existing = self._owned.get(identity)
            if existing is not None:
                if existing["binding"] != binding or existing["started"] or existing["process"] is None:
                    raise PermissionDenied("host was already started or rebound")
                return asdict(existing["process"].observation)
            self._verify_images()
            reserved = self.records.reserve(binding)
            scratch = Path(reserved["scratch"])
            record = {"binding": binding, "profile": None, "scratch": scratch, "pipe": None,
                      "process": None, "started": False, "lock": asyncio.Lock(),
                      "transfer": None, "child": None, "parent_job": None}
            self._owned[identity] = record
            parent = child = owned = kernel = transfer = None
            try:
                parent_job = WindowsJob.open_parent(parent_job_name, self.safety_sid)
                record["parent_job"] = parent_job
                profile = TaskContainerProfile(reserved["profile"])
                record["profile"] = profile
                scratch.mkdir(exist_ok=False)
                protect_task_scratch(scratch, runner_sid=self.runner_sid, appcontainer_sid=profile.sid)
                parent, child = Pipe(duplex=True)
                record["pipe"], record["child"] = JsonPipe(parent), child
                parent = None
                kernel, transfer = inheritable_handle_copy(child.fileno())
                record.update(transfer=transfer, transfer_kernel=kernel)
                owned = launch_suspended(
                    executable=self.executable, arguments=["-I", "-S", "-B", str(self.entry), str(transfer), str(scratch)],
                    cwd=scratch, environment={**self.environment, "TMP": str(scratch), "TEMP": str(scratch)},
                    expected_user_sid=self.runner_sid,
                    executable_digest=self.executable_digest, inherited_handles=(transfer,),
                    appcontainer_sid=profile.sid, require_lpac=True, max_processes=1, job_name=reserved["job_name"],
                    parent_jobs=(int(parent_job.handle),),
                )
                record["process"] = owned
                self.records.observe(binding, owned.observation)
                return asdict(owned.observation)
            except BaseException as error:
                if isinstance(error, ParentJobCleanupFailed):
                    record["parent_job"] = error.parent
                if isinstance(error, WindowsProcessCleanupFailed):
                    record["launch_failure"] = error
                await self.stop_host(binding=binding)
                raise
            finally:
                if record["parent_job"] is not None:
                    record["parent_job"].close()
                    record["parent_job"] = None
                if record["transfer"] is not None:
                    if not kernel.CloseHandle(record["transfer"]):
                        raise OSError("host transfer handle cleanup is unverified")
                    record["transfer"] = None
                if record["child"] is not None:
                    record["child"].close()
                    record["child"] = None
                if parent is not None:
                    parent.close()

    async def run_host(self, *, binding, bootstrap):
        identity = _binding_digest(binding)
        record = self._owned.get(identity)
        if record is None or record["binding"] != binding or self._closing:
            raise PermissionDenied("no prepared host belongs to this Run")
        if (not isinstance(bootstrap, dict) or set(bootstrap) != {"endpoint", "public_key", "command"}
                or not isinstance(bootstrap["command"], dict)):
            raise PermissionDenied("invalid fixed host bootstrap")
        try:
            async with record["lock"]:
                if record["started"]:
                    raise PermissionDenied("an execution host can start only once")
                record["started"] = True
                # Safety registered the observed peer before this request. Start
                # the fixed reader before writing a potentially large bootstrap;
                # a suspended reader would fill the OS pipe buffer and deadlock.
                record["process"].start()
            await asyncio.to_thread(record["pipe"].send, bootstrap)
            while True:
                try:
                    frame = await asyncio.to_thread(record["pipe"].receive, timeout_s=.25)
                except TimeoutError:
                    if record["process"].poll():
                        raise OSError("restricted HER host exited without a complete result")
                    continue
                if not isinstance(frame, dict):
                    raise PermissionDenied("invalid host return frame")
                if frame.get("type") == "event" and set(frame) == {"type", "event"}:
                    await emit_private_event(decode(frame["event"]))
                    continue
                if frame.get("type") == "result" and set(frame) == {"type", "result"}:
                    result = frame["result"]
                    await self.stop_host(binding=binding)
                    return result
                raise OSError("restricted HER result is unavailable")
        finally:
            await self.stop_host(binding=binding)

    async def prepare_script(self, **arguments):
        if self._scripts is None:
            raise PermissionDenied("the script result owner is not installed")
        return await self._scripts.prepare(**arguments)

    async def run_script(self, **arguments):
        if self._scripts is None:
            raise PermissionDenied("the script result owner is not installed")
        return await self._scripts.run(**arguments)

    async def stop_script(self, *, binding, operation_id):
        if self._scripts is not None:
            return await self._scripts.stop(binding=binding, operation_id=operation_id)
        await asyncio.to_thread(self.records.retire, binding, operation_id=operation_id)
        return {"stopped": True}

    async def script_result(self, **arguments):
        if self._scripts is None:
            await self.stop_script(binding=arguments["binding"], operation_id=arguments["operation_id"])
            return None
        return await self._scripts.result(**arguments)

    async def stop_host(self, *, binding):
        owners = [self._stop_her_host(binding=binding)]
        if self._scripts is not None:
            owners.append(self._scripts.stop_binding(binding))
        results = await asyncio.gather(*owners, return_exceptions=True)
        if any(isinstance(result, BaseException) for result in results):
            raise OSError("Run host/script cleanup remains unverified")
        return results[0]

    async def _stop_her_host(self, *, binding):
        identity = _binding_digest(binding)
        record = self._owned.get(identity)
        if record is None:
            await asyncio.to_thread(self.records.retire, binding)
            return {"stopped": True, "already_stopped": True}
        if record["binding"] != binding:
            raise PermissionDenied("another execution owns this host")
        async with record["lock"]:
            if record.get("launch_failure") is not None:
                await asyncio.to_thread(record["launch_failure"].retry_cleanup)
                record.pop("launch_failure")
            process = record["process"]
            if process is not None:
                await asyncio.to_thread(process.stop, timeout_ms=4500)
            if record["parent_job"] is not None:
                record["parent_job"].close()
                record["parent_job"] = None
            if record["pipe"] is not None:
                record["pipe"].close()
            if record["child"] is not None:
                record["child"].close()
                record["child"] = None
            if record["transfer"] is not None:
                if not record["transfer_kernel"].CloseHandle(record["transfer"]):
                    raise OSError("host transfer handle cleanup is unverified")
                record["transfer"] = None
            await asyncio.to_thread(self.records.retire, binding, process_stopped=process is not None)
            if record["profile"] is not None:
                record["profile"]._closed = True
            self._owned.pop(identity, None)
            return {"stopped": True, "process_id": process.observation.process_id if process is not None else None,
                    "zero_job": True}

    async def close(self):
        self._closing = True
        if self._scripts is not None:
            self._scripts._closing = True
            bindings = {record["binding"] for record in tuple(self._scripts._owned.values())}
            results = await asyncio.gather(*(self._scripts.stop_binding(binding) for binding in bindings),
                                           return_exceptions=True)
            if any(isinstance(result, BaseException) for result in results):
                raise OSError("Runner script cleanup remains unverified")
        results = await asyncio.gather(
            *(self.stop_host(binding=record["binding"]) for record in tuple(self._owned.values())),
            return_exceptions=True,
        )
        if any(isinstance(result, BaseException) for result in results) or self._owned:
            raise OSError("Runner cleanup remains unverified")
        await asyncio.to_thread(self.records.recover_all)
        if self.records.pending():
            raise OSError("persistent Runner cleanup remains unverified")
        return True


class RunnerClient:
    """Safety's pinned private Runner client; no host receives this surface."""

    def __init__(self, client, *, runner_sid, safety_sid, worker_sid, instance_id, capsule_digest=None):
        self.client, self.runner_sid, self.safety_sid, self.worker_sid = client, runner_sid, safety_sid, worker_sid
        self.instance_id, self.capsule_digest = instance_id, capsule_digest
        self.observation, self._prepared = None, {}
        self.parent_job_name = None

    async def qualify(self):
        return self._accept_qualification(await self.client.call_async("health", {}))

    def qualify_sync(self):
        return self._accept_qualification(self.client.call("health", {}))

    def _accept_qualification(self, result):
        if (not isinstance(result, dict) or result.get("state") != "ready"
                or result.get("instance_id") != self.instance_id or result.get("runner_contract") != 2
                or (self.capsule_digest is not None and result.get("capsule_digest") != self.capsule_digest)
                or result.get("identity") != {"user_sid": self.runner_sid, "elevated": False,
                                              "administrator_member": False}
                or result.get("safety_sid") != self.safety_sid or result.get("worker_sid") != self.worker_sid
                or len({self.runner_sid, self.safety_sid, self.worker_sid}) != 3):
            raise PermissionDenied("independent Runner qualification is unavailable")
        self.observation = result
        return True

    def qualified_for(self, safety_sid, worker_sid):
        return (self.observation is not None and self.parent_job_name is not None and self.safety_sid == safety_sid and self.worker_sid == worker_sid)

    def scripts_qualified(self):
        return (self.qualified_for(self.safety_sid, self.worker_sid)
                and self.observation.get("script_contract") == 1)

    def script_helper_descriptor(self):
        import base64

        if not self.scripts_qualified():
            raise PermissionDenied("the offline script Runner is unavailable")
        return {"endpoint": self.client.endpoint,
                "public_key": base64.b64encode(self.client.public_key.public_bytes_raw()).decode("ascii"),
                "root_job_name": self.parent_job_name, "runner_sid": self.runner_sid}

    def retire_script_sync(self, binding, operation_id, descriptor):
        from dataclasses import asdict

        from orchestrator.permissions.ipc_rpc import PermissionPipeClient

        client = PermissionPipeClient(self.client.endpoint, self.client.public_key,
                                      methods={"script_result"}, idle_timeout_s=15)
        result = client.call("script_result", {"binding": binding, "operation_id": operation_id,
                                              "descriptor": asdict(descriptor)})
        return result

    async def stop_script(self, binding, operation_id):
        from orchestrator.permissions.ipc_rpc import PermissionPipeClient

        client = PermissionPipeClient(self.client.endpoint, self.client.public_key,
                                      methods={"stop_script"}, idle_timeout_s=15)
        result = await client.call_async("stop_script", {"binding": binding, "operation_id": operation_id})
        if not isinstance(result, dict) or result.get("stopped") is not True:
            raise OSError("script profile cleanup remains unverified")
        return True

    async def prepare(self, binding):
        if self.observation is None or binding.context.instance_id != self.instance_id:
            raise PermissionDenied("the Runner is not qualified for this instance")
        if self.parent_job_name is None:
            raise PermissionDenied("the owning Safety process has no parent Job")
        result = await self.client.call_async("prepare_host", {"binding": binding, "parent_job_name": self.parent_job_name})
        if (not isinstance(result, dict) or result.get("user_sid") != self.runner_sid
                or result.get("elevated") is not False or result.get("atomic_job") is not True
                or result.get("lpac") is not True or result.get("capability_count") != 0
                or result.get("parent_job_count") != 1
                or result.get("executable_digest") != self.observation["executable_digest"]
                or not isinstance(result.get("appcontainer_sid"), str)
                or type(result.get("process_id")) is not int):
            raise PermissionDenied("prepared host identity does not match its qualification")
        self._prepared[binding.context.execution_epoch] = result
        return PipePeerPolicy(self.runner_sid, result["appcontainer_sid"], result["process_id"], True)

    async def run_her(self, executor, command):
        if executor._inference is None:
            raise PermissionDenied("the host has no frozen selected model capability")
        initial = {
            **command, "profiles": executor._inference["profiles"],
            "privacy_level": executor._inference["privacy_level"],
            "tool_definitions": executor.channel.tool_definitions(),
        }
        from orchestrator.permissions.host_protocol import unpack

        config = unpack(command["config"])
        self.client.idle_timeout_s = float(config.user_idle_timeout_s)
        return await self.client.call_async("run_host", {
            "binding": executor.binding,
            "bootstrap": {"endpoint": executor.address, "public_key": executor.factory.public_key, "command": initial},
        }, on_event=emit_private_event)

    async def stop(self, binding):
        result = await self.client.call_async("stop_host", {"binding": binding})
        if not isinstance(result, dict) or result.get("stopped") is not True:
            raise PermissionDenied("host cleanup remains unverified")
        self._prepared.pop(binding.context.execution_epoch, None)
        return True
