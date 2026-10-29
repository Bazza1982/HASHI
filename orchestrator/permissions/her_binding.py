"""Functions PAO binding for permission-managed HERV3 turns.

This owner is installed only for an explicitly configured managed Agent. Public
request metadata cannot install it or provide a resume capability.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from orchestrator.permissions.authority import PermissionDenied
from orchestrator.permissions.continuation import PermissionPark
from orchestrator.permissions.models import RuntimeExecutionBinding
from orchestrator.permissions.parking import PermissionParkingStore
from orchestrator.permissions.run_service import binding_from_route
from orchestrator.permissions.start_wait import PermissionStartWait, PermissionStartStore
from orchestrator.session_store import TERMINAL_RUN_STATES


@dataclass
class ManagedHerTurn:
    owner: object
    binding: RuntimeExecutionBinding
    executor: object
    recovery: dict | None = None
    adapter: object | None = None
    accepted: object | None = None
    resumed_unstarted: bool = False
    runtime: object | None = None
    media: object | None = None

    def bind_provider(self, provider):
        binder = getattr(self.executor, "bind_provider", None)
        if callable(binder):
            binder(provider)
        else:
            provider.tool_registry = getattr(self.executor, "registry", self.executor)

    async def run(self, runtime, prompt, request_id, **arguments):
        self.runtime = runtime
        from orchestrator.permissions.media_bridge import RunMedia

        self.media = RunMedia(arguments.get("request_content"), prompt)
        delegated = getattr(self.executor, "run_her", None)
        if callable(delegated):
            return await delegated(runtime, prompt, request_id, binding=self.binding,
                                   permission_recovery=self.recovery, **arguments)
        return await runtime.run_turn(prompt, request_id, turn_id=self.binding.context.turn_id, **arguments)


class PermissionAgentController:
    def __init__(self, runtime, service):
        if not all(callable(getattr(service, method, None)) for method in (
            "open_execution", "execution_registry", "settle_run", "close_execution",
        )):
            raise ValueError("a qualified PAO permission service is required")
        self.runtime, self.service = runtime, service
        self.parking = PermissionParkingStore(service.store)
        self.start_waits = PermissionStartStore(service.store)
        self._unstarted = {}
        self._turns, self._signals, self._wake_task = {}, {}, None
        self._queued, self._closing = set(), False
        self._owned_bindings = {}
        self._callback_endpoint, self._callback_descriptor = None, None

    async def open_turn(self, adapter, accepted, request_id):
        meta = adapter._runtime_request_meta(request_id)
        run_id, owner = meta.get("hashi_run_id"), meta.get("owner_id")
        if not run_id or not owner:
            raise PermissionDenied("managed HERV3 requires a canonical PAO Run")
        run = self.service.store.get_run(run_id, owner_id=owner)
        generation = getattr(self.runtime, "_worker_version_metadata", {}).get("generation_id")
        if (not generation or run["request_id"] != request_id or run["agent_id"] != self.runtime.name
                or run["session_id"] != meta.get("hashi_session_id")
                or run["fencing_token"] != meta.get("hashi_fencing_token")):
            raise PermissionDenied("the managed Worker claim is unavailable")
        self._queued.discard(request_id)
        try:
            binding = await self.service.open_execution(
                request_id=request_id, run_id=run_id, owner_id=owner, agent_id=self.runtime.name,
                session_id=run["session_id"], context_generation=run["context_generation"],
                fencing_token=run["fencing_token"], worker_id=run["worker_id"],
                worker_generation=generation, turn_id=accepted.turn_id, backend_id="her-v2",
                callbacks=self._callback_descriptor,
            )
        except OSError as error:
            from orchestrator.permissions.wire import PermissionRemoteError

            if isinstance(error, PermissionRemoteError) and error.code != "permission_service_unavailable":
                raise
            claim = dict(request_id=request_id, run_id=run_id, owner_id=owner, agent_id=self.runtime.name,
                         session_id=run["session_id"], context_generation=run["context_generation"],
                         fencing_token=run["fencing_token"], worker_id=run["worker_id"])
            signal = PermissionStartWait(request_id)
            self._unstarted[request_id] = (claim, adapter._session_coordinator, accepted, None, signal)
            raise signal from error
        execution = ManagedHerTurn(self, binding, self.service.execution_registry(binding),
                                   adapter=adapter, accepted=accepted)
        if accepted.duplicate and not accepted.duplicate_text:
            pending = [payload for payload in self.start_waits.list_waiting(self.runtime.name)
                       if payload["claim"]["run_id"] == run_id and payload["_state"] == "resuming"]
            if pending:
                ticket = self.start_waits.ticket(binding)
                adapter._session_coordinator.store.adopt_unstarted_permission_turn(
                    ticket, execution_owner=adapter._session_coordinator.execution_owner,
                )
                ticket.finish()
                execution.resumed_unstarted = True
            else:
                ticket = self.parking.resume_ticket(binding)
                execution.recovery = adapter._session_coordinator.store.adopt_permission_turn(
                    ticket, execution_owner=adapter._session_coordinator.execution_owner,
                )
        self._turns[request_id] = execution
        self._owned_bindings[binding.context.execution_epoch] = binding
        self._queued.discard(request_id)
        return execution

    @staticmethod
    def is_park(signal):
        return isinstance(signal, (PermissionPark, PermissionStartWait))

    def capture(self, request_id, signal, coordinator, accepted):
        execution = self._turns.get(request_id)
        if not isinstance(signal, PermissionPark) or execution is None:
            raise PermissionDenied("a current managed continuation is required")
        recovery = coordinator.store.active_turn_recovery(accepted.session_id, accepted.turn_id)
        checkpoint = coordinator.store.provider_context_for_turn(accepted.session_id, accepted.turn_id)
        if checkpoint is None and getattr(execution.executor, "_permission_prestart_failure", False):
            from orchestrator.permissions.parking import _claims

            self._unstarted[request_id] = (_claims(execution.binding), coordinator, accepted, execution.binding, signal)
            return
        if (not recovery or not checkpoint or checkpoint.get("boundary") not in {
                    "pending_tool_batch", "ready_provider_request", "completed_tool_batch",
                    "completed_provider_response",
                }
                or not recovery.get("provider_context_digest") or not recovery.get("provider_context_producer_claim")):
            raise PermissionDenied("no durable pending Tool boundary was committed")
        reference = {
            "her_session_id": accepted.session_id, "turn_id": accepted.turn_id,
            "digest": recovery["provider_context_digest"],
            "producer_run_claim": recovery["provider_context_producer_claim"],
        }
        self._signals[request_id] = (signal, execution.binding, reference, coordinator, accepted)

    async def park(self, item, signal):
        if isinstance(signal, PermissionStartWait) or item.request_id in self._unstarted:
            saved_start = self._unstarted.pop(item.request_id, None)
            if saved_start is None or saved_start[4] is not signal:
                return False
            claim, coordinator, accepted, binding, _ = saved_start
            self.start_waits.park(claim, item, {
                "her_session_id": accepted.session_id, "turn_id": accepted.turn_id,
                "execution_owner_id": coordinator.execution_owner["owner_id"],
            })
            if binding is not None:
                execution = self._turns.pop(item.request_id)
                try:
                    await execution.executor.park()
                except OSError:
                    # The durable wait exists. A new epoch still has to prove
                    # old-resource retirement; this is not a clean-stop claim.
                    self.runtime.error_logger.warning("Prestart executor cleanup remains unavailable")
            return True
        saved = self._signals.pop(item.request_id, None)
        if saved is None or saved[0] is not signal:
            return False
        _, binding, reference, coordinator, accepted = saved
        self.parking.park(
            binding, item, checkpoint_reference=reference, request_id=signal.result.request_id,
            operation_id=signal.result.operation_id, state=signal.result.state,
        )
        coordinator.store.mark_permission_waiting(
            session_id=accepted.session_id, turn_id=accepted.turn_id,
            execution_owner_id=coordinator.execution_owner["owner_id"],
            expected_digest=reference["digest"],
        )
        execution = self._turns[item.request_id].executor
        park_resources = getattr(execution, "park", None)
        if callable(park_resources):
            if await park_resources() is not True:
                raise PermissionDenied("parked executor cleanup is unverified")
        self._turns.pop(item.request_id, None)
        return True

    async def settle(self, item):
        execution = self._turns.pop(item.request_id, None)
        if execution is None:
            return
        run = self.service.store.get_run(execution.binding.context.run_id,
                                         owner_id=execution.binding.context.owner_id)
        if run["state"] in TERMINAL_RUN_STATES:
            await self.service.settle_run(binding=execution.binding)
        else:
            await self.service.close_execution(binding=execution.binding)
        self._owned_bindings.pop(execution.binding.context.execution_epoch, None)

    async def start(self):
        if self._wake_task is not None or self._closing:
            raise RuntimeError("permission wakeup owner is already started")
        installation = getattr(self.runtime.global_config, "permission_service", None) or {}
        safety_sid = installation.get("service_sid")
        if safety_sid:
            from orchestrator.permissions.her_callbacks import start_callbacks

            self._callback_endpoint, self._callback_descriptor = await start_callbacks(self, safety_sid)
        self._wake_task = asyncio.create_task(self._watch())

    async def _watch(self):
        # Notifications are hints; authoritative state is rechecked after
        # missed events and adoption. This loop never invokes a model.
        while not self._closing:
            try:
                await self.wake_ready()
            except (PermissionDenied, OSError, ValueError):
                self.runtime.error_logger.warning("Permission continuation remains unavailable")
            await asyncio.sleep(.5)

    async def wake_ready(self):
        from orchestrator.flexible_backend_registry import canonical_backend_engine

        if canonical_backend_engine(self.runtime.config.active_backend) != "her-v2":
            # Selection never wakes a checkpoint into a different engine. The
            # same Task and completed facts remain available for explicit PAO
            # continuation; the old executor is not silently resurrected.
            return
        pending_starts = self.start_waits.list_waiting(self.runtime.name)
        if pending_starts:
            qualify = getattr(self.service, "qualify", None)
            if callable(qualify):
                await qualify()
            for payload in pending_starts:
                request_id = payload["claim"]["request_id"]
                if request_id in self._queued:
                    continue
                run = self.service.store.get_run(payload["claim"]["run_id"], owner_id=payload["claim"]["owner_id"])
                if run["state"] == "queued":
                    item = self.start_waits.recover_queued(payload)
                elif payload["_state"] == "parked" and run["state"] == "running":
                    item = self.start_waits.claim_resume(payload)
                else:
                    continue
                self._queued.add(request_id)
                try:
                    await self.runtime.queue.put(item)
                except BaseException:
                    self._queued.discard(request_id)
                    raise
        for payload in self.parking.list_parked(agent_id=self.runtime.name):
            binding = binding_from_route(payload["binding"])
            if binding.request_id in self._queued:
                continue
            status = self.service.authority.execution_status(binding.context)
            if status.task_state != "active":
                continue
            run = self.service.store.get_run(binding.context.run_id, owner_id=binding.context.owner_id)
            if run["state"] == "queued":
                item = self.parking.recover_queued(binding)
            else:
                if run["state"] != "running" or payload.get("_park_state") == "resuming":
                    continue
                ready = (status.binding_state == "fenced"
                         or payload["reason"] in {"unavailable", "permission_changed", "fenced"})
                if not ready and payload["operation_id"]:
                    read_operation = getattr(self.service.authority, "operation_status_for_task", None)
                    if callable(read_operation):
                        operation = read_operation(binding.context, payload["operation_id"])
                        ready = (operation is not None and operation.state in {"succeeded", "failed"}
                                 and operation.resource_state == "stopped")
                if not ready and payload["request_id"]:
                    request = self.service.authority.get_request(binding.context, payload["request_id"])
                    ready = request.state in {"denied", "withdrawn", "expired", "decided", "granted_existing"}
                if not ready:
                    continue
                item, _ = self.parking.claim_resume(binding)
            self._queued.add(binding.request_id)
            try:
                await self.runtime.queue.put(item)
            except BaseException:
                self._queued.discard(binding.request_id)
                raise

    async def before_backend_switch(self, target_engine):
        from orchestrator.flexible_backend_registry import canonical_backend_engine

        if canonical_backend_engine(target_engine) == "her-v2":
            return
        bindings = dict(self._owned_bindings)
        for payload in self.parking.list_parked(agent_id=self.runtime.name):
            from orchestrator.permissions.run_service import binding_from_route
            binding = binding_from_route(payload["binding"])
            bindings.setdefault(binding.context.execution_epoch, binding)
        # Stop the canonical attempts first. Even an offline Safety service
        # cannot use its old PaoRunFence to dispatch a Tool or Provider call.
        for binding in bindings.values():
            self.service.store.cancel_run(binding.context.run_id, owner_id=binding.context.owner_id,
                                          reason="backend_changed_to_unmanaged")
        async def retire(binding):
            closer = getattr(self.service, "fence_for_backend_switch", None)
            if callable(closer):
                return await closer(binding)
            await asyncio.to_thread(self.service.authority.fence_execution, binding.context)
            return await self.service.close_execution(binding=binding)
        results = await asyncio.gather(*(retire(binding) for binding in bindings.values()), return_exceptions=True)
        for binding, result in zip(bindings.values(), results):
            if not isinstance(result, BaseException):
                self._owned_bindings.pop(binding.context.execution_epoch, None)
        if any(isinstance(value, BaseException) for value in results):
            self.runtime.error_logger.warning("Old backend remains fenced; resource settlement is unverified")

    async def close(self):
        self._closing = True
        bindings = tuple(self._owned_bindings.values())
        errors = await asyncio.gather(*(asyncio.to_thread(
            self.service.authority.fence_execution, binding.context,
        ) for binding in bindings), return_exceptions=True)
        if self._wake_task is not None:
            self._wake_task.cancel()
            await asyncio.gather(self._wake_task, return_exceptions=True)
            self._wake_task = None
        errors.extend(await asyncio.gather(*(self.service.close_execution(binding=binding)
                                           for binding in bindings), return_exceptions=True))
        if self._callback_endpoint is not None:
            result = await asyncio.gather(self._callback_endpoint.close(), return_exceptions=True)
            errors.extend(result)
            if not isinstance(result[0], BaseException):
                self._callback_endpoint = None
        if any(isinstance(error, BaseException) for error in errors):
            raise OSError("permission Worker cleanup remains unverified")
        self._turns.clear()
        self._owned_bindings.clear()
