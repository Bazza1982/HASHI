"""PAO Session execution leases; one Agent owner, isolated per-turn state.

The queue and durable writers remain Agent-owned. ContextVar propagation also
keeps callbacks, tools and detached completion in their admitting Session.
"""
from __future__ import annotations

import asyncio
import copy
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
import hashlib
import json
from typing import Any
from orchestrator.execution_resources import execution_budget, limit

_CURRENT = ContextVar("hashi_session_execution", default=None)
LOCAL_FIELDS = frozenset({
    "config", "backend_manager", "context_assembler", "current_request_meta",
    "is_generating", "last_prompt", "last_response", "_user_interrupt",
    "_workzone_state", "_workzone_dir", "_workzone_dirs", "_backend_workzone_topology",
    "_last_prompt_audit", "_thinking_chars_this_req", "_last_full_prompt_tokens",
    "_context_compaction_prompt_tokens",
    "_think_buffer", "_commentary_buffer", "_openrouter_think_chunk",
    "_last_openrouter_think_snippet", "_pending_session_primer",
    "_pending_session_primer_session_id", "_pending_auto_recall_context",
    "_pending_auto_recall_session_id",
})


@dataclass
class Execution:
    owner: Any
    session_id: str
    signature: str
    values: dict[str, Any] = field(default_factory=dict)
    active_request: str = ""


def scoped_execution(runtime):
    execution = _CURRENT.get()
    return execution if execution is not None and execution.owner is runtime else None


def read_field(runtime, name):
    execution = scoped_execution(runtime)
    if execution is not None and name in LOCAL_FIELDS:
        if name in execution.values:
            return True, execution.values[name]
        return False, None
    if name in {"is_generating", "current_request_meta"}:
        state = object.__getattribute__(runtime, "__dict__")
        active = [value for value in state.get("_session_executions", {}).values() if value.active_request]
        if name == "is_generating" and any(not task.done() for task in state.get("_session_execution_tasks", {}).values()):
            return True, True
        if active:
            if name == "is_generating":
                return True, True
            for value in active:
                meta = value.values.get(name)
                if meta:
                    return True, meta
    return False, None


def write_field(runtime, name, value):
    execution = scoped_execution(runtime)
    if execution is not None and name in LOCAL_FIELDS:
        execution.values[name] = value
        return True
    return False


@contextmanager
def bind(execution):
    token = _CURRENT.set(execution)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def session_control(function):
    """Bind a trusted command Session before touching a CLI or retry state."""
    @wraps(function)
    async def wrapped(runtime, update, *args, **kwargs):
        from orchestrator import runtime_session
        try:
            selected = runtime_session.current_session_for_update(runtime, update)
        except (AttributeError, runtime_session.SessionNotFound):
            selected = {}
        execution = getattr(runtime, "_session_executions", {}).get(str(selected.get("session_id") or ""))
        with bind(execution):
            return await function(runtime, update, *args, **kwargs)
    return wrapped


class SessionQueue(asyncio.Queue):
    """Keep every waiting item visible to existing recall/cancel projections."""
    def __init__(self):
        super().__init__()
        self.changed = asyncio.Event()

    def put_nowait(self, item):
        super().put_nowait(item)
        self.changed.set()

    def take_ready(self, blocked):
        for item in self._queue:
            session = str(getattr(item, "session_id", "") or "legacy")
            if session not in blocked:
                self._queue.remove(item)
                self._wakeup_next(self._putters)
                return item
        return None


def queue_reasons(runtime):
    """Derive waiting reasons from the same PAO queue and execution owners."""
    reasons = dict(getattr(runtime, '_execution_queue_reasons', {}))
    busy = getattr(runtime, '_executing_sessions', set())
    for item in getattr(getattr(runtime, 'queue', None), '_queue', ()):
        session = str(getattr(item, 'session_id', '') or 'legacy')
        reasons[item.request_id] = 'session_order' if session in busy else 'agent_capacity'
    return reasons


def snapshot(runtime):
    """Freeze execution choices at admission, never create another state writer."""
    manager = runtime.backend_manager
    return {
        "config": copy.deepcopy(runtime.config),
        "state": copy.deepcopy(manager._read_state_dict()),
        "settings": {key: copy.deepcopy(getattr(manager, key, None)) for key in (
            "agent_mode", "privacy_level", "_active_model_override",
            "_her_v3_configuration_override", "_her_v2_configuration_override",
            "_her_v2_configuration_draft", "_agents_json_global",
        )},
    }


async def create_execution(runtime, item, frozen):
    from orchestrator.session_backend_manager import SessionBackendManager
    session = str(getattr(item, "session_id", "") or "legacy")
    signature = hashlib.sha256(json.dumps({
        "config": vars(frozen["config"]), "settings": frozen["settings"],
        "generation": getattr(item, "context_generation", None),
    }, sort_keys=True, default=str).encode()).hexdigest()
    executions = runtime._session_executions
    previous = executions.get(session)
    if previous is not None and previous.signature == signature and previous.values["backend_manager"].current_backend is not None:
        return previous
    if previous is not None:
        await previous.values["backend_manager"].shutdown()
    execution = Execution(runtime, session, signature)
    root = object.__getattribute__(runtime, "__dict__")
    for name in LOCAL_FIELDS - {"backend_manager", "config", "context_assembler"}:
        if name in root:
            value = root[name]
            execution.values[name] = copy.deepcopy(value) if isinstance(value, (dict, list, set)) else value
    execution.values.update(config=frozen["config"], current_request_meta=None,
                            is_generating=False, last_prompt=None, last_response=None, _user_interrupt=None)
    if root.get("context_assembler") is not None:
        assembler = copy.copy(root["context_assembler"])
        for key, value in vars(assembler).items():
            if isinstance(value, (dict, list, set)):
                setattr(assembler, key, copy.deepcopy(value))
        execution.values["context_assembler"] = assembler
    manager = SessionBackendManager(runtime.backend_manager, runtime, frozen)
    execution.values["backend_manager"] = manager
    with bind(execution):
        if not await manager.initialize_active_backend():
            raise RuntimeError("Session execution backend initialization failed")
        backend = manager.current_backend
        session_mode = manager.agent_mode == "fixed" and bool(backend.capabilities.supports_sessions)
        if callable(getattr(backend, "set_session_mode", None)):
            backend.set_session_mode(session_mode)
    executions[session] = execution
    # Bound idle processes without resetting their durable conversation.
    idle = [value for value in executions.values() if not value.active_request and value is not execution]
    while len(executions) > 8 and idle:
        expired = idle.pop(0)
        await expired.values["backend_manager"].shutdown()
        executions.pop(expired.session_id, None)
    return execution


async def process_sessions(runtime, process_item):
    queue = runtime.queue
    capacity = limit((runtime.config.extra or {}).get("max_concurrent_sessions"), "max_concurrent_sessions", 2, 8)
    runtime._session_executions = {}
    runtime._session_execution_tasks = {}
    busy = set()
    runtime._executing_sessions = busy
    tasks = set()

    async def execute(item, session):
        execution = None
        pipeline_owns_queue = False
        try:
            frozen = runtime._execution_admissions.pop(item.request_id, None) or snapshot(runtime)
            from orchestrator import runtime_cancel
            async with runtime_cancel.transition_lock(runtime):
                if item.request_id in runtime_cancel.requested_ids(runtime) or runtime_cancel.queued_run_is_terminal(runtime,item):
                    await runtime_cancel.finish_queued_request(runtime,item)
                    return
            async with execution_budget(runtime, item, frozen):
                async with runtime_cancel.transition_lock(runtime):
                    if item.request_id in runtime_cancel.requested_ids(runtime) or runtime_cancel.queued_run_is_terminal(runtime,item):
                        await runtime_cancel.finish_queued_request(runtime,item)
                        return
                execution = await create_execution(runtime, item, frozen)
                execution.active_request = item.request_id
                with bind(execution):
                    pipeline_owns_queue = True
                    await process_item(runtime, item)
                    # Detachment must not release a Session/order or execution slot.
                    completion = getattr(runtime, "_background_completion_tasks_by_request", {}).get(item.request_id)
                    if isinstance(completion, asyncio.Task):
                        await asyncio.shield(completion)
                    elif item.request_id in getattr(runtime, "_background_request_ids", set()):
                        generation = getattr(runtime, "_generation_tasks_by_request", {}).get(item.request_id)
                        if isinstance(generation, asyncio.Task):
                            await asyncio.shield(generation)
                        while item.request_id in getattr(runtime, "_background_request_ids", set()):
                            await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            if not pipeline_owns_queue:
                from orchestrator import runtime_cancel
                await runtime_cancel.finish_queued_request(runtime,item)
            raise
        except Exception as exc:
            runtime.error_logger.exception("Session execution failed: %s", exc)
            if execution is None:
                await runtime._notify_request_listeners(item.request_id, {
                    "request_id": item.request_id, "success": False, "text": None,
                    "error": str(exc), "error_code": "session_execution_unavailable",
                    "error_retryable": False, "source": item.source, "summary": item.summary,
                })
        finally:
            if not pipeline_owns_queue:
                queue.task_done()
            if execution is not None:
                execution.active_request = ""
            busy.discard(session)
            runtime._session_execution_tasks.pop(item.request_id, None)
            queue.changed.set()
            from orchestrator.runtime_lifecycle import _publish_worker_metadata
            with bind(None):
                await _publish_worker_metadata(runtime, transition="Session execution released")

    try:
        while True:
            queue.changed.clear()
            while len(busy) < capacity:
                item = queue.take_ready(busy)
                if item is None:
                    break
                session = str(getattr(item, "session_id", "") or "legacy")
                busy.add(session)
                task = asyncio.create_task(execute(item, session), name=f"session-execution-{item.request_id}")
                runtime._session_execution_tasks[item.request_id] = task
                tasks.add(task)
                task.add_done_callback(tasks.discard)
            await queue.changed.wait()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for execution in runtime._session_executions.values():
            with bind(execution):
                await execution.values["backend_manager"].shutdown()
        runtime._session_executions.clear()
