"""Installed native execution assembly; paths and keys are never caller input."""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from cryptography.hazmat.primitives import serialization

from orchestrator.permissions.authority import PermissionDenied
from orchestrator.permissions.execution_channel import PermissionExecutionChannel, EXECUTION_METHODS
from orchestrator.permissions.file_process_supervisor import WindowsFileProcessSupervisor
from orchestrator.permissions.ipc_server import PermissionEndpoint
from orchestrator.permissions.models import PermissionLevel
from orchestrator.permissions.pao_run_fence import PaoRunFence
from orchestrator.permissions.registry import ManagedToolRegistry
from orchestrator.permissions.windows_ipc import WindowsPipeListener, PipePeerPolicy, current_process_user_sid
from orchestrator.permissions.wire import PermissionServiceRouter


@dataclass(frozen=True)
class InstalledFileHelper:
    executable: Path
    entry: Path
    executable_digest: str
    entry_digest: str
    private_root: Path
    environment: dict

    def verify(self):
        if (not all(isinstance(path, Path) and path.is_absolute()
                    for path in (self.executable, self.entry, self.private_root))
                or not self.private_root.is_dir()
                or sha256(self.executable.read_bytes()).hexdigest() != self.executable_digest
                or sha256(self.entry.read_bytes()).hexdigest() != self.entry_digest):
            raise PermissionDenied("installed file helper qualification changed")


class _NoModelTransport:
    async def provider_call(self, **arguments):
        raise PermissionDenied("this channel has no selected model capability")

    async def close(self):
        return True


class InstalledExecution:
    def __init__(self, factory, binding, policy, channel, endpoint, address):
        self.factory, self.binding, self.policy = factory, binding, policy
        self.channel, self.endpoint, self.address = channel, endpoint, address
        self.registry = channel.registry
        self._closed = False
        self._cleanup_lock = asyncio.Lock()
        self._inference_digest, self._inference = None, None

    def attach_callbacks(self, descriptor):
        from orchestrator.permissions.her_callbacks import CallbackClient

        callbacks = CallbackClient(descriptor)
        self.channel._checkpoint = callbacks.checkpoint
        self.channel._audit = lambda record: callbacks.audit(self.binding, record)

    async def configure_inference(self, config):
        from orchestrator.permissions.host_protocol import pack
        import json

        self.channel._current()
        digest = sha256(json.dumps(pack(config), separators=(",", ":")).encode()).hexdigest()
        if self._inference_digest is not None:
            if digest != self._inference_digest:
                raise PermissionDenied("the Turn model capability is already frozen")
            return self._inference
        if self.factory.model_factory is None:
            raise PermissionDenied("the selected model owner is unavailable")
        models, public = await self.factory.model_factory.create(self.binding, config, self.channel._guard)
        try:
            self.channel._current()
        except BaseException:
            await models.close()
            raise
        self.channel._models = models
        self._inference_digest, self._inference = digest, public
        return public

    def descriptor(self):
        self.channel._current()
        return {"binding": self.binding, "level": self.policy.settings.requested_level.value,
                "endpoint": self.address, "public_key": self.factory.public_key,
                "tool_definitions": self.channel.tool_definitions()}

    async def park(self):
        # A parked epoch can still receive a human decision, but it cannot
        # retain an executable host or in-flight provider/file resources.
        await self._stop_host()
        return await self.channel.park()

    async def _stop_host(self):
        if self.policy.settings.requested_level is PermissionLevel.L2:
            if self.factory.runner is None:
                raise OSError("the Runner owner is unavailable")
            if await self.factory.runner.stop(self.binding) is not True:
                raise OSError("restricted HER host cleanup is unverified")

    async def close(self):
        async with self._cleanup_lock:
            if self._closed:
                return True
            owners = [self._stop_host(), self.channel.close()]
            if self.endpoint is not None:
                owners.append(self.endpoint.close())
            # Retain ownership until every child owner confirms cleanup. One
            # failed owner must not prevent the others from being attempted.
            results = await asyncio.gather(*owners, return_exceptions=True)
            errors = [result for result in results if isinstance(result, BaseException)]
            if errors:
                raise OSError("installed executor cleanup is unverified") from errors[0]
            self._closed = True
            return True

    async def run_restricted_her(self, command):
        if self.policy.settings.requested_level is not PermissionLevel.L2 or self.factory.runner is None:
            raise PermissionDenied("this Run has no qualified restricted HER host")
        result = await self.factory.runner.run_her(self, command)
        evidence = self.channel._models.usage_receipts()
        return {**result, "provider_evidence": evidence,
                 "accounting_epoch": self.binding.context.execution_epoch,
                "tool_call_count": len(self.channel._completed_tool_calls),
                "tool_loop_count": sum(1 for receipt in evidence if receipt["tool_batch"])}


class InstalledExecutorFactory:
    """One central service owns all Run channels and supervisor signing keys."""

    def __init__(self, authority, run_store, *, helper, results, stop_issuer, signing_key,
                 worker_sid, validate_resource, checkpoint_writer, audit_writer, runner=None,
                 policy_observer=None, model_factory=None):
        helper.verify()
        if (not callable(validate_resource) or not callable(checkpoint_writer) or not callable(audit_writer)
                or not callable(getattr(stop_issuer, "issue_stop", None))):
            raise ValueError("complete trusted service ports are required")
        self.authority, self.store, self.helper = authority, run_store, helper
        self.results, self.issuer, self.key = results, stop_issuer, signing_key
        self.worker_sid, self.sid = worker_sid, current_process_user_sid()
        self.validate_resource, self.checkpoint_writer, self.audit_writer = (
            validate_resource, checkpoint_writer, audit_writer,
        )
        self.runner, self._owned = runner, {}
        self._policy_observer = policy_observer
        self.model_factory = model_factory
        self.public_key = base64.b64encode(signing_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw,
        )).decode("ascii")

    def executor_id(self, policy):
        if policy.settings.requested_level is PermissionLevel.L2:
            if (self.runner is None or self.sid == self.worker_sid
                    or not self.runner.qualified_for(self.sid, self.worker_sid)):
                raise PermissionDenied("independent L2 service and runner qualification is unavailable")
        return self.issuer.key_id

    async def create(self, binding, policy):
        self.helper.verify()
        self.executor_id(policy)
        if self._policy_observer is not None:
            self._policy_observer(binding, policy)
        guard = PaoRunFence(
            self.store, context=binding.context, request_id=binding.request_id,
            fencing_token=binding.fencing_token, worker_id=binding.worker_id,
        )
        options = dict(policy.registry_options)
        roots = tuple(Path(path) for path in options.get("access_roots", (policy.cwd,)))
        supervisor = WindowsFileProcessSupervisor(
            self.authority, cwd=policy.cwd, read_roots=roots, result_store=self.results,
            issue_stop=self.issuer.issue_stop, run_guard=guard,
            helper_executable=self.helper.executable, helper_entry=self.helper.entry,
            executable_digest=self.helper.executable_digest, entry_digest=self.helper.entry_digest,
            private_root=self.helper.private_root, environment=self.helper.environment,
            expected_user_sid=self.sid, validate_resource=self.validate_resource,
        )
        registry = ManagedToolRegistry(
            allowed_tools=list(policy.allowed_tools), access_root=roots[0], workspace_dir=policy.cwd,
            secrets={}, permission_authority=self.authority, permission_context=binding.context,
            permission_supervisor=supervisor, requestable_scopes=policy.requestable_scopes,
            durable_wait=True, **options,
        )
        channel = PermissionExecutionChannel(
            binding, authority=self.authority, run_guard=guard, registry=registry,
            model_broker=_NoModelTransport(), checkpoint_writer=self.checkpoint_writer,
            audit_writer=self.audit_writer,
        )
        address = r"\\.\pipe\LOCAL\HASHI.Permissions.execution." + uuid4().hex
        executor = InstalledExecution(self, binding, policy, channel, None, address)
        self._owned[binding.context.execution_epoch] = executor
        try:
            if policy.settings.requested_level is PermissionLevel.L2:
                # Register ownership before preparing the suspended host: even
                # a failed prepare can have native resources to reconcile.
                peer = await self.runner.prepare(binding)
            else:
                peer = PipePeerPolicy(self.worker_sid)
            executor.endpoint = PermissionEndpoint(
                WindowsPipeListener(address, service_sid=self.sid, peer_policy=peer),
                PermissionServiceRouter(channel, methods=EXECUTION_METHODS),
                signing_key=self.key, service_generation=self.authority._service_generation,
            )
            await executor.endpoint.start()
            return executor
        except BaseException:
            await executor.close()
            self._owned.pop(binding.context.execution_epoch, None)
            raise

    async def retire(self, binding):
        executor = self._owned.get(binding.context.execution_epoch)
        if executor is None:
            # Service recovery may not invent a clean stop. A separate native
            # resource reconciliation must settle any persisted outstanding Job.
            if self.runner is not None:
                await self.runner.stop(binding)
            return not self.authority.unsettled_resources(binding.context)
        if executor.binding != binding:
            raise PermissionDenied("retirement belongs to another binding")
        await executor.close()
        self._owned.pop(binding.context.execution_epoch, None)
        return True

    async def close(self):
        results = await asyncio.gather(
            *(self.retire(executor.binding) for executor in tuple(self._owned.values())),
            return_exceptions=True,
        )
        errors = [result for result in results if isinstance(result, BaseException)]
        if errors or self._owned:
            raise OSError("installed execution resources remain unverified") from (errors[0] if errors else None)
        return True
