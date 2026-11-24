"""An execution endpoint owns one immutable Run; it cannot approve or rebind."""
from __future__ import annotations

from dataclasses import asdict
import inspect
import json

from orchestrator.permissions.authority import PermissionDenied
from orchestrator.permissions.continuation import PermissionPark
from orchestrator.permissions.models import RuntimeExecutionBinding
from orchestrator.permissions.pao_run_fence import PaoRunFence
from orchestrator.permissions.registry import ManagedToolRegistry

EXECUTION_METHODS = frozenset({"tool_execute", "provider_call", "provider_checkpoint", "audit_record", "runtime_port"})


class PermissionExecutionChannel:
    def __init__(self, binding, *, authority, run_guard, registry, model_broker,
                 checkpoint_writer, audit_writer, require_checkpoints=False):
        if (not isinstance(binding, RuntimeExecutionBinding) or not isinstance(run_guard, PaoRunFence)
                or run_guard.binding != binding or not isinstance(registry, ManagedToolRegistry)
                or not registry.permission_durable_wait or not callable(checkpoint_writer)
                or not callable(audit_writer)):
            raise ValueError("a complete trusted Run execution channel is required")
        self.binding, self._guard, self._authority = binding, run_guard, authority
        self.registry, self._models = registry, model_broker
        self._checkpoint, self._audit = checkpoint_writer, audit_writer
        self._runtime_port = None
        self._evidence_callback = None
        self._evidence_policy_validator = None
        self._closed, self._parked = False, False
        self._completed_tool_calls = set()
        self._require_checkpoints = bool(require_checkpoints)
        self._pending_calls, self._ready_provider = {}, None

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
        if self._require_checkpoints:
            saved = self._pending_calls.get(tool_call_id)
            if saved != (tool_name, json.dumps(arguments, sort_keys=True, separators=(",", ":"), allow_nan=False)):
                raise PermissionDenied("the Tool call has no exact durable pending boundary")
        from orchestrator.permissions.activity import TOOL_CALL

        token = TOOL_CALL.set(tool_call_id)
        try:
            if tool_name == "request_diagnostics" and isinstance(arguments, dict) and arguments.get("recovery_evidence_ref"):
                result = await self._read_recovery_evidence(arguments, tool_call_id)
            else:
                result = await self.registry.execute(tool_name, arguments, tool_call_id)
        except PermissionPark as park:
            result = park.result
        finally:
            TOOL_CALL.reset(token)
        self._current()
        from tools.registry import ToolResult

        if isinstance(result, ToolResult):
            self._completed_tool_calls.add((tool_name, tool_call_id))
            self._pending_calls.pop(tool_call_id, None)
        return result

    async def _read_recovery_evidence(self, arguments, tool_call_id):
        """A single Run-bound recovery item, under the existing PAO policy."""
        import asyncio
        import time
        from pathlib import Path
        from orchestrator.permissions.descriptors import describe_tool
        from tools.registry import ToolResult

        started = time.monotonic()
        admission = self.registry.evaluate_admission("request_diagnostics", arguments, tool_call_id)
        if admission is not None:
            return self.registry._finish_result("request_diagnostics", arguments, admission, started)
        unavailable = {"status": "not available"}
        page = unavailable
        try:
            if not callable(self._evidence_callback) or not callable(self._evidence_policy_validator):
                raise PermissionDenied("scoped recovery evidence owner is unavailable")
            evidence = await self._evidence_callback(binding=self.binding, arguments=arguments)
            self._current()
            if not isinstance(evidence, dict) or not isinstance(evidence.get("page"), dict):
                raise PermissionDenied("invalid scoped recovery evidence")
            proposed = evidence["page"]
            if proposed.get("status") == "available":
                name, source_args = evidence.get("tool_name"), evidence.get("tool_arguments")
                if not isinstance(name, str) or not isinstance(source_args, dict):
                    raise PermissionDenied("the source Tool identity is unavailable")
                if self.registry.evaluate_admission(name, source_args, tool_call_id) is not None:
                    raise PermissionDenied("the source Tool is not currently admitted")
                cwd, roots = self.registry._permission_cwd, self.registry._permission_roots
                # A default search root cannot be reconstructed from an old
                # checkpoint without evidence of the original roots.
                if name == "file_search" and not source_args.get("roots"):
                    raise PermissionDenied("the source search roots are unavailable")
                if any(isinstance(value, str) and not Path(value).is_absolute() for value in (
                    [source_args.get("path")] + list(source_args.get("roots") or [])
                )):
                    if Path(str(evidence.get("source_workzone") or "")).resolve(strict=False) != cwd:
                        raise PermissionDenied("the source working directory cannot be rebound")
                descriptor = describe_tool(name, source_args, cwd=cwd, read_roots=roots)
                if not descriptor.scopes:
                    raise PermissionDenied("the source action has no reviewed scope")
                for scope in descriptor.scopes:
                    if self._evidence_policy_validator(self.binding.context, scope) is not True:
                        raise PermissionDenied("the source scope is outside the current policy")
                if await asyncio.to_thread(self._authority.evaluate, self.binding.context, descriptor.scopes) is not True:
                    raise PermissionDenied("the source scope is not currently authorized")
                # Check again after the owner read and permission lookup, before
                # releasing any content to the restricted execution host.
                self._current()
                if any(self._evidence_policy_validator(self.binding.context, scope) is not True
                       for scope in descriptor.scopes):
                    raise PermissionDenied("the source policy changed")
                if await asyncio.to_thread(self._authority.evaluate, self.binding.context, descriptor.scopes) is not True:
                    raise PermissionDenied("the source authorization changed")
                if any(self._evidence_policy_validator(self.binding.context, scope) is not True
                       for scope in descriptor.scopes):
                    raise PermissionDenied("the source policy changed")
                self._current()
                page = proposed
        except (PermissionDenied, OSError, TypeError, ValueError, AttributeError):
            page = unavailable
        result = ToolResult(
            tool_call_id=tool_call_id,
            output=json.dumps({"recovery_evidence": page}, ensure_ascii=False),
            details={"control_disposition": "allowed" if page is not unavailable else "denied"},
        )
        return self.registry._finish_result("request_diagnostics", arguments, result, started)

    async def provider_call(self, *, engine, model, payload, use_stream):
        self._current()
        if self._require_checkpoints:
            ready = (engine, model, json.dumps(payload.get("messages"), sort_keys=True,
                                              separators=(",", ":"), allow_nan=False))
            if self._ready_provider != ready:
                raise PermissionDenied("model transport has no exact durable ready boundary")
            self._ready_provider = None
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
        self._accept_checkpoint(checkpoint)
        return result

    def seed_trusted_recovery(self, checkpoint):
        """Private assembly method; deliberately absent from execution RPC."""
        self._current()
        if checkpoint is None:
            self._pending_calls, self._ready_provider = {}, None
        else:
            self._accept_checkpoint(checkpoint)

    def _accept_checkpoint(self, checkpoint):
        if self._require_checkpoints:
            pending, ready = {}, None
            if checkpoint.get("boundary") == "pending_tool_batch":
                from orchestrator.her_v2.provider_continuation import pending_tool_calls

                for call in pending_tool_calls(checkpoint["messages"]):
                    function = call["function"]
                    arguments = function.get("arguments") or {}
                    if isinstance(arguments, str):
                        arguments = json.loads(arguments)
                    pending[call["id"]] = (function["name"], json.dumps(
                        arguments, sort_keys=True, separators=(",", ":"), allow_nan=False,
                    ))
            elif checkpoint.get("boundary") == "ready_provider_request":
                ready = (checkpoint["provider"], checkpoint["model"], json.dumps(
                    checkpoint["messages"], sort_keys=True, separators=(",", ":"), allow_nan=False,
                ))
            self._pending_calls, self._ready_provider = pending, ready

    async def runtime_port(self, *, operation, arguments):
        self._current()
        if operation == "question_availability":
            if arguments != {"args": [], "kwargs": {}}:
                raise PermissionDenied("question availability takes no arguments")
            from orchestrator.permissions.host_protocol import pack
            from orchestrator.permissions.questions import ScopedRunQuestions
            import asyncio

            questions = self.registry._ordinary_questions
            if questions is None:
                return pack(False)
            if not isinstance(questions, ScopedRunQuestions) or questions.binding != self.binding:
                raise PermissionDenied("question availability is outside this Run")
            policy = await asyncio.to_thread(
                questions.questions.question_policy, self.binding.context.agent_id,
            )
            self._current()
            return pack(policy.get("enabled") is True)
        if self._runtime_port is None:
            raise PermissionDenied("the actual HER runtime ports are unavailable")
        result = await self._runtime_port(self.binding, operation, arguments)
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
        return await self._stop_resources()

    async def close(self):
        self._closed = True
        failure = None
        try:
            self._authority.fence_execution(self.binding.context)
        except Exception as error:
            failure = error
        await self._stop_resources()
        if failure is not None:
            raise OSError("execution epoch fencing remains unverified") from failure
        return True

    async def _stop_resources(self):
        import asyncio

        supervisor = self.registry._permission_gate._supervisor
        retire = getattr(supervisor, "close", None)
        async def stop_files():
            if not callable(retire):
                raise OSError("supervisor cleanup is unavailable")
            result = retire()
            if inspect.isawaitable(result):
                result = await result
            if result is not True:
                raise OSError("supervisor cleanup is unverified")
        results = await asyncio.gather(self._models.close(), stop_files(), return_exceptions=True)
        errors = [result for result in results if isinstance(result, BaseException)]
        if errors:
            raise OSError("execution resource cleanup is unverified") from errors[0]
        return True

    def descriptor(self):
        self._current()
        return {"binding": asdict(self.binding), "tool_definitions": self.tool_definitions()}
# HASHI-Fingerprint: hfp_4773f9ce379f4cb78fb68dd45c3fa44d
