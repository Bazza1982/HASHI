"""Independent nonadmin Runner for a fixed, sealed HERV3 host."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from hashlib import sha256
import json
from multiprocessing import Pipe
from pathlib import Path
import shutil

from orchestrator.permissions.authority import PermissionDenied
from orchestrator.permissions.ipc_rpc import emit_private_event
from orchestrator.permissions.models import RuntimeExecutionBinding
from orchestrator.permissions.windows_container import TaskContainerProfile, protect_task_scratch
from orchestrator.permissions.windows_ipc import JsonPipe, PipePeerPolicy
from orchestrator.permissions.windows_process import inheritable_handle_copy, launch_suspended
from orchestrator.permissions.windows_security import require_nonadmin_identity, require_private_path
from orchestrator.permissions.wire import decode


def _binding_digest(binding):
    if not isinstance(binding, RuntimeExecutionBinding):
        raise PermissionDenied("a fixed trusted Run binding is required")
    return sha256(json.dumps(asdict(binding), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class RestrictedHerRunner:
    METHODS = frozenset({"health", "prepare_host", "run_host", "stop_host"})

    def __init__(self, *, runner_sid, safety_sid, worker_sid, private_root, executable,
                 entry, executable_digest, entry_digest, environment, capsule_roots, instance_id, capsule=None):
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
        self._verify_images()

    def _verify_images(self):
        if (sha256(self.executable.read_bytes()).hexdigest() != self.executable_digest
                or sha256(self.entry.read_bytes()).hexdigest() != self.entry_digest):
            raise PermissionDenied("the sealed HER host image changed")

    def health(self):
        self._verify_images()
        return {"state": "ready", "identity": self.identity, "instance_id": self.instance_id, "safety_sid": self.safety_sid,
                "worker_sid": self.worker_sid, "executable_digest": self.executable_digest,
                "entry_digest": self.entry_digest}

    async def prepare_host(self, *, binding):
        identity = _binding_digest(binding)
        if binding.context.instance_id != self.instance_id:
            raise PermissionDenied("host belongs to another installed instance")
        async with self._locks.setdefault(identity, asyncio.Lock()):
            if self._closing:
                raise PermissionDenied("the Runner is closing")
            existing = self._owned.get(identity)
            if existing is not None:
                if existing["binding"] != binding or existing["started"]:
                    raise PermissionDenied("host was already started or rebound")
                return asdict(existing["process"].observation)
            self._verify_images()
            profile = TaskContainerProfile()
            scratch = self.private / ("host-" + identity)
            parent = child = owned = kernel = transfer = None
            try:
                scratch.mkdir(exist_ok=False)
                protect_task_scratch(scratch, runner_sid=self.runner_sid, appcontainer_sid=profile.sid)
                parent, child = Pipe(duplex=True)
                kernel, transfer = inheritable_handle_copy(child.fileno())
                owned = launch_suspended(
                    executable=self.executable, arguments=["-I", "-S", str(self.entry), str(transfer), str(scratch)],
                    cwd=scratch, environment={**self.environment, "TMP": str(scratch), "TEMP": str(scratch)},
                    expected_user_sid=self.runner_sid,
                    executable_digest=self.executable_digest, inherited_handles=(transfer,),
                    appcontainer_sid=profile.sid, require_lpac=True,
                )
                record = {"binding": binding, "profile": profile, "scratch": scratch,
                          "pipe": JsonPipe(parent), "process": owned, "started": False,
                          "lock": asyncio.Lock()}
                parent = None
                self._owned[identity] = record
                return asdict(owned.observation)
            except BaseException:
                if owned is not None:
                    await asyncio.to_thread(owned.stop, timeout_ms=4500)
                profile.close()
                if scratch.exists():
                    shutil.rmtree(scratch)
                raise
            finally:
                if transfer is not None and not kernel.CloseHandle(transfer):
                    raise OSError("host transfer handle cleanup is unverified")
                if child is not None:
                    child.close()
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

    async def stop_host(self, *, binding):
        identity = _binding_digest(binding)
        record = self._owned.get(identity)
        if record is None:
            return {"stopped": True, "already_stopped": True}
        if record["binding"] != binding:
            raise PermissionDenied("another execution owns this host")
        async with record["lock"]:
            await asyncio.to_thread(record["process"].stop, timeout_ms=4500)
            record["pipe"].close()
            await asyncio.to_thread(record["profile"].close)
            if record["scratch"].exists():
                await asyncio.to_thread(shutil.rmtree, record["scratch"])
            self._owned.pop(identity, None)
            return {"stopped": True, "process_id": record["process"].observation.process_id,
                    "zero_job": True}

    async def close(self):
        self._closing = True
        results = await asyncio.gather(
            *(self.stop_host(binding=record["binding"]) for record in tuple(self._owned.values())),
            return_exceptions=True,
        )
        if any(isinstance(result, BaseException) for result in results) or self._owned:
            raise OSError("Runner cleanup remains unverified")
        return True


class RunnerClient:
    """Safety's pinned private Runner client; no host receives this surface."""

    def __init__(self, client, *, runner_sid, safety_sid, worker_sid, instance_id):
        self.client, self.runner_sid, self.safety_sid, self.worker_sid = client, runner_sid, safety_sid, worker_sid
        self.instance_id = instance_id
        self.observation, self._prepared = None, {}

    async def qualify(self):
        return self._accept_qualification(await self.client.call_async("health", {}))

    def qualify_sync(self):
        return self._accept_qualification(self.client.call("health", {}))

    def _accept_qualification(self, result):
        if (not isinstance(result, dict) or result.get("state") != "ready"
                or result.get("instance_id") != self.instance_id
                or result.get("identity") != {"user_sid": self.runner_sid, "elevated": False,
                                              "administrator_member": False}
                or result.get("safety_sid") != self.safety_sid or result.get("worker_sid") != self.worker_sid
                or len({self.runner_sid, self.safety_sid, self.worker_sid}) != 3):
            raise PermissionDenied("independent Runner qualification is unavailable")
        self.observation = result
        return True

    def qualified_for(self, safety_sid, worker_sid):
        return (self.observation is not None and self.safety_sid == safety_sid and self.worker_sid == worker_sid)

    async def prepare(self, binding):
        if self.observation is None or binding.context.instance_id != self.instance_id:
            raise PermissionDenied("the Runner is not qualified for this instance")
        result = await self.client.call_async("prepare_host", {"binding": binding})
        if (not isinstance(result, dict) or result.get("user_sid") != self.runner_sid
                or result.get("elevated") is not False or result.get("atomic_job") is not True
                or result.get("lpac") is not True or result.get("capability_count") != 0
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
