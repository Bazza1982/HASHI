"""Trusted PAO Run-to-task owner shared by Functions L1 and the safety service."""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
import inspect
from pathlib import Path
from uuid import uuid4

from orchestrator.permissions.authority import PermissionDenied
from orchestrator.permissions.models import ExecutionContext, PermissionLevel, PermissionSettings, RuntimeExecutionBinding
from orchestrator.permissions.pao_run_fence import PaoRunFence
from orchestrator.permissions.parking import PermissionParkingStore
from orchestrator.session_store import SessionStore, TERMINAL_RUN_STATES


@dataclass(frozen=True, slots=True)
class ManagedRunPolicy:
    settings: PermissionSettings
    revision: int
    cwd: Path
    requestable_scopes: tuple
    allowed_tools: tuple[str, ...]
    registry_options: dict

    def __post_init__(self):
        if (not isinstance(self.settings, PermissionSettings)
                or self.settings.requested_level is PermissionLevel.L0
                or type(self.revision) is not int or self.revision < 1
                or not isinstance(self.cwd, Path) or not self.cwd.is_absolute()
                or not isinstance(self.allowed_tools, tuple) or not isinstance(self.registry_options, dict)):
            raise ValueError("a validated managed Run policy is required")


def binding_from_route(value):
    if not isinstance(value, dict) or set(value) != {"context", "request_id", "fencing_token", "worker_id"}:
        raise ValueError("invalid private Run route")
    return RuntimeExecutionBinding(ExecutionContext(**value["context"]), value["request_id"],
                                   value["fencing_token"], value["worker_id"])


class PermissionRunService:
    """Own lifecycle calls; execution channels never receive this interface."""

    def __init__(self, authority, run_store, *, policy_resolver, executor_factory):
        if (not isinstance(run_store, SessionStore) or not callable(policy_resolver)
                or not all(callable(getattr(executor_factory, name, None))
                           for name in ("retire", "create", "executor_id"))):
            raise ValueError("trusted PAO, policy and executor owners are required")
        self.authority, self.store = authority, run_store
        self.parking = PermissionParkingStore(run_store)
        self._policy, self._executors = policy_resolver, executor_factory
        self._locks, self._active, self._registries = {}, {}, {}

    async def _retire(self, binding):
        # Fence first, then independently stop resources. An old Worker exit
        # cannot revoke a replacement merely because the Agent name matches.
        self.authority.fence_execution(binding.context)
        result = self._executors.retire(binding)
        if inspect.isawaitable(result):
            result = await result
        if result is not True:
            raise PermissionDenied("previous executor cleanup is unverified")

    async def open_execution(self, *, request_id, run_id, owner_id, agent_id, session_id,
                             context_generation, fencing_token, worker_id, worker_generation,
                             turn_id, backend_id):
        lock = self._locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            run = self.store.get_run(run_id, owner_id=owner_id)
            if (run["request_id"] != request_id or run["agent_id"] != agent_id
                    or run["session_id"] != session_id or run["context_generation"] != context_generation
                    or run["fencing_token"] != fencing_token or run["worker_id"] != worker_id
                    or run["state"] != "running" or backend_id not in {"her-v2", "her-v3"}
                    or not isinstance(worker_generation, str) or not worker_generation
                    or not isinstance(turn_id, str) or not turn_id):
                raise PermissionDenied("canonical PAO Run binding is unavailable")
            policy = self._policy(agent_id, owner_id, session_id)
            if not isinstance(policy, ManagedRunPolicy):
                raise PermissionDenied("managed execution policy is unavailable")
            current = self._active.get(run_id)
            if current is not None and all((
                current.request_id == request_id, current.fencing_token == fencing_token,
                current.worker_id == worker_id, current.context.worker_generation == worker_generation,
                current.context.turn_id == turn_id, current.context.backend_id == backend_id,
                current.context.policy_revision == policy.revision,
            )):
                return current
            recorded = self.parking.route(run_id, owner_id=owner_id)
            prior = binding_from_route(recorded) if recorded is not None else None
            if prior is not None:
                if prior.fencing_token > fencing_token:
                    raise PermissionDenied("a newer PAO attempt already owns this Run")
                await self._retire(prior)
                task_id = prior.context.task_id
            else:
                task_id = "task_" + uuid4().hex
                parent = run.get("parent_run_id")
                if parent:
                    route = self.parking.route(parent, owner_id=owner_id)
                    if route is not None:
                        candidate = binding_from_route(route)
                        status = self.authority.execution_status(candidate.context)
                        if (candidate.context.agent_id == agent_id and candidate.context.session_id == session_id
                                and candidate.context.context_generation == str(context_generation)
                                and status.task_state in {"active", "paused"}):
                            task_id = candidate.context.task_id
            context = ExecutionContext(
                instance_id=self.store.instance_id, owner_id=owner_id, agent_id=agent_id,
                task_id=task_id, run_id=run_id, session_id=session_id, turn_id=turn_id,
                worker_generation=worker_generation, context_generation=str(context_generation),
                execution_epoch="epoch_" + uuid4().hex, backend_id=backend_id,
                executor_id=self._executors.executor_id(policy), policy_revision=policy.revision,
            )
            binding = RuntimeExecutionBinding(context, request_id, fencing_token, worker_id)
            guard = PaoRunFence(self.store, context=context, request_id=request_id,
                                fencing_token=fencing_token, worker_id=worker_id)
            self.authority.open_task(context)
            try:
                self.authority.register_runtime_context(context, run_guard=guard)
                if prior is not None:
                    self.authority.rebind_parked_requests(context, run_guard=guard)
                self.parking.record_binding(binding)
                executor = self._executors.create(binding, policy)
                if inspect.isawaitable(executor):
                    executor = await executor
                self._registries[context.execution_epoch] = executor
                self._active[run_id] = binding
                return binding
            except BaseException:
                self.authority.fence_execution(context)
                raise

    def execution_registry(self, binding):
        if (not isinstance(binding, RuntimeExecutionBinding)
                or self._active.get(binding.context.run_id) != binding):
            raise PermissionDenied("execution channel was superseded")
        registry = self._registries.get(binding.context.execution_epoch)
        if registry is None:
            raise PermissionDenied("execution channel is unavailable")
        return registry

    async def close_execution(self, *, binding):
        if not isinstance(binding, RuntimeExecutionBinding):
            raise PermissionDenied("an exact execution binding is required")
        await self._retire(binding)
        self._registries.pop(binding.context.execution_epoch, None)
        if self._active.get(binding.context.run_id) == binding:
            self._active.pop(binding.context.run_id, None)

    async def settle_run(self, *, binding):
        if not isinstance(binding, RuntimeExecutionBinding):
            raise PermissionDenied("an exact execution binding is required")
        run = self.store.get_run(binding.context.run_id, owner_id=binding.context.owner_id)
        if run["fencing_token"] != binding.fencing_token:
            route = self.parking.route(binding.context.run_id, owner_id=binding.context.owner_id)
            # User stop/terminal resolution also advances the PAO fence. Only
            # the last recorded executor may settle that terminal Run. A late
            # older Worker must never settle a replacement execution.
            if run["state"] not in TERMINAL_RUN_STATES or route != asdict(binding):
                await self.close_execution(binding=binding)
                return "superseded"
        if run["state"] not in TERMINAL_RUN_STATES:
            raise PermissionDenied("the PAO Run is not terminal")
        with self.store._connection() as connection:
            active = connection.execute(
                "SELECT 1 FROM permission_run_routes p JOIN runs r ON r.run_id=p.run_id "
                "WHERE p.task_id=? AND r.state IN ('queued','running') "
                "UNION ALL SELECT 1 FROM runs child WHERE child.parent_run_id IN "
                "(SELECT run_id FROM permission_run_routes WHERE task_id=?) "
                "AND child.state IN ('queued','running') AND child.session_id=? "
                "AND child.agent_id=? AND child.context_generation=? LIMIT 1",
                (binding.context.task_id, binding.context.task_id, binding.context.session_id,
                 binding.context.agent_id, int(binding.context.context_generation)),
            ).fetchone()
        if active is None:
            terminal = "completed" if run["state"] == "completed" else (
                "cancelled" if run["state"] in {"cancelled", "stopped"} else "failed_terminal"
            )
            self.authority.end_task(binding.context, outcome=terminal)
        else:
            self.authority.end_run(binding.context)
        self.parking.finish(binding)
        await self.close_execution(binding=binding)
        return "settled"
