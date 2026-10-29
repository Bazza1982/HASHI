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
from orchestrator.session_store import TERMINAL_RUN_STATES


@dataclass
class ManagedHerTurn:
    owner: object
    binding: RuntimeExecutionBinding
    executor: object
    recovery: dict | None = None

    def bind_provider(self, provider):
        provider.tool_registry = getattr(self.executor, "registry", self.executor)

    async def run(self, runtime, prompt, request_id, **arguments):
        delegated = getattr(self.executor, "run_her", None)
        if callable(delegated):
            return await delegated(runtime, prompt, request_id, binding=self.binding, **arguments)
        return await runtime.run_turn(prompt, request_id, turn_id=self.binding.context.turn_id, **arguments)


class PermissionAgentController:
    def __init__(self, runtime, service):
        if not all(callable(getattr(service, method, None)) for method in (
            "open_execution", "execution_registry", "settle_run", "close_execution",
        )):
            raise ValueError("a qualified PAO permission service is required")
        self.runtime, self.service = runtime, service
        self.parking = PermissionParkingStore(service.store)
        self._turns, self._signals, self._wake_task = {}, {}, None
        self._queued, self._closing = set(), False
        self._owned_bindings = {}

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
        binding = await self.service.open_execution(
            request_id=request_id, run_id=run_id, owner_id=owner, agent_id=self.runtime.name,
            session_id=run["session_id"], context_generation=run["context_generation"],
            fencing_token=run["fencing_token"], worker_id=run["worker_id"],
            worker_generation=generation, turn_id=accepted.turn_id, backend_id="her-v2",
        )
        execution = ManagedHerTurn(self, binding, self.service.execution_registry(binding))
        if accepted.duplicate and not accepted.duplicate_text:
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
        return isinstance(signal, PermissionPark)

    def capture(self, request_id, signal, coordinator, accepted):
        execution = self._turns.get(request_id)
        if not isinstance(signal, PermissionPark) or execution is None:
            raise PermissionDenied("a current managed continuation is required")
        recovery = coordinator.store.active_turn_recovery(accepted.session_id, accepted.turn_id)
        checkpoint = coordinator.store.provider_context_for_turn(accepted.session_id, accepted.turn_id)
        if (not recovery or not checkpoint or checkpoint.get("boundary") != "pending_tool_batch"
                or not recovery.get("provider_context_digest") or not recovery.get("provider_context_producer_claim")):
            raise PermissionDenied("no durable pending Tool boundary was committed")
        reference = {
            "her_session_id": accepted.session_id, "turn_id": accepted.turn_id,
            "digest": recovery["provider_context_digest"],
            "producer_run_claim": recovery["provider_context_producer_claim"],
        }
        self._signals[request_id] = (signal, execution.binding, reference, coordinator, accepted)

    async def park(self, item, signal):
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

    async def start(self):
        if self._wake_task is not None or self._closing:
            raise RuntimeError("permission wakeup owner is already started")
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
                ready = status.binding_state == "fenced"
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

    async def close(self):
        self._closing = True
        for binding in tuple(self._owned_bindings.values()):
            self.service.authority.fence_execution(binding.context)
        if self._wake_task is not None:
            self._wake_task.cancel()
            await asyncio.gather(self._wake_task, return_exceptions=True)
            self._wake_task = None
        for binding in tuple(self._owned_bindings.values()):
            await self.service.close_execution(binding=binding)
        self._turns.clear()
        self._owned_bindings.clear()
