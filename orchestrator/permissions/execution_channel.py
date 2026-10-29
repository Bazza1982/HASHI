"""An execution endpoint owns one immutable Run; it cannot approve or rebind."""
from __future__ import annotations

from dataclasses import asdict
import inspect

from orchestrator.permissions.authority import PermissionDenied
from orchestrator.permissions.continuation import PermissionPark
from orchestrator.permissions.models import RuntimeExecutionBinding
from orchestrator.permissions.pao_run_fence import PaoRunFence
from orchestrator.permissions.registry import ManagedToolRegistry

EXECUTION_METHODS = frozenset({"tool_execute", "provider_call", "provider_checkpoint", "audit_record"})


class PermissionExecutionChannel:
    def __init__(self, binding, *, authority, run_guard, registry, model_broker,
                 checkpoint_writer, audit_writer):
        if (not isinstance(binding, RuntimeExecutionBinding) or not isinstance(run_guard, PaoRunFence)
                or run_guard.binding != binding or not isinstance(registry, ManagedToolRegistry)
                or not registry.permission_durable_wait or not callable(checkpoint_writer)
                or not callable(audit_writer)):
            raise ValueError("a complete trusted Run execution channel is required")
        self.binding, self._guard, self._authority = binding, run_guard, authority
        self.registry, self._models = registry, model_broker
        self._checkpoint, self._audit = checkpoint_writer, audit_writer
        self._closed, self._parked = False, False
        self._completed_tool_calls = set()

    def _current(self):
        if self._closed or self._parked:
            raise PermissionDenied("execution channel is closed")
        with self._guard(self.binding.context):
            status = self._authority.execution_status(self.binding.context)
            if status.task_state != "active" or status.binding_state != "active":
                raise PermissionDenied("execution binding is inactive")

    def tool_definitions(self):
        self._current()
        return self.registry.get_tool_definitions()

    async def tool_execute(self, *, tool_name, arguments, tool_call_id):
        self._current()
        try:
            result = await self.registry.execute(tool_name, arguments, tool_call_id)
        except PermissionPark as park:
            result = park.result
        self._current()
        from tools.registry import ToolResult

        if isinstance(result, ToolResult):
            self._completed_tool_calls.add((tool_name, tool_call_id))
        return result

    async def provider_call(self, *, engine, model, payload, use_stream):
        self._current()
        return await self._models.provider_call(engine=engine, model=model, payload=payload, use_stream=use_stream)

    async def provider_checkpoint(self, *, checkpoint):
        # The producer is derived from the canonical Run, never from host input.
        self._current()
        if not isinstance(checkpoint, dict) or "producer_run_claim" in checkpoint:
            raise PermissionDenied("invalid host checkpoint")
        result = self._checkpoint(self.binding, checkpoint)
        if inspect.isawaitable(result):
            result = await result
        self._current()
        return result

    async def audit_record(self, *, record):
        self._current()
        if (not isinstance(record, dict) or record.get("turn_id") != self.binding.context.turn_id
                or record.get("request_ref") != "hashi-request:" + self.binding.request_id):
            raise PermissionDenied("audit record is outside this Run")
        result = self._audit(record)
        if inspect.isawaitable(result):
            result = await result
        self._current()
        return result

    async def park(self):
        # Human decisions still target this live canonical Run. Stop execution
        # resources without fencing its approval epoch until PAO adopts a new
        # executor. No further host calls are accepted on this parked channel.
        self._parked = True
        await self._models.close()
        supervisor = self.registry._permission_gate._supervisor
        closer = getattr(supervisor, "close", None)
        if not callable(closer):
            raise OSError("parked supervisor cleanup is unavailable")
        result = closer()
        if inspect.isawaitable(result):
            result = await result
        return result is True

    async def close(self):
        self._closed = True
        self._authority.fence_execution(self.binding.context)
        await self._models.close()
        supervisor = self.registry._permission_gate._supervisor
        retire = getattr(supervisor, "close", None)
        if not callable(retire):
            raise OSError("supervisor cleanup is unavailable")
        outcome = retire()
        if inspect.isawaitable(outcome):
            outcome = await outcome
        if outcome is not True:
            raise OSError("supervisor cleanup is unverified")
        return True

    def descriptor(self):
        self._current()
        return {"binding": asdict(self.binding), "tool_definitions": self.tool_definitions()}
