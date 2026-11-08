"""Owner/scoped-Run admission for the bounded Agent management API family."""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
import json

from aiohttp import web

from orchestrator.session_store import StaleFencingToken


_current_check = ContextVar("agent_mutation_run_check", default=None)


def _reject(status, **payload):
    error = {401: web.HTTPUnauthorized, 403: web.HTTPForbidden, 404: web.HTTPNotFound}[status]
    raise error(text=json.dumps({"ok": False, **payload}), content_type="application/json")


def mutation_authority(server, request, agent, tool):
    """Body markers never authenticate; a supplied scoped bearer cannot fall back to owner."""
    if getattr(request, "headers", {}).get("X-Hashi-Scheduler-Token"):
        binding = server._scheduler_tool_run(request, agent, tool)
        if binding is None:
            _reject(401, error_code="agent_run_not_active")
        return binding
    denied = server._guard_agent_management_access(request, agent)
    if denied is not None:
        _reject(denied.status, error="management authentication required")
    return None


def invoke_mutation(server, binding, callback, *, scope=None):
    """Fence a short local effect, or admission of an independently scheduled operation."""
    try:
        if binding is None:
            guard = nullcontext()
        elif scope is not None:
            guard = server.session_store.hold_operation_approval(binding=binding, scope=scope)
        else:
            guard = server.session_store.hold_active_run(**binding)
        with guard as approval:
            if binding is not None and scope is not None and not approval["authorized"]:
                _reject(403, error_code="approval_required", approval=approval,
                        decision_path=f"/api/v1/approvals/{approval['approval_id']}",
                        message="The exact operation requires a trusted PAO owner decision.")
            return callback()
    except StaleFencingToken:
        _reject(401, error_code="agent_run_not_active")


async def invoke_async_mutation(server, binding, callback, *, scope=None):
    # Never hold SQLite across Worker RPC, queue admission or a service await.
    # A scheduled child has its own lifecycle once admitted. Recovery rechecks
    # this authority after its lock wait and before each additional dispatch.
    invoke_mutation(server, binding, lambda: None, scope=scope)
    guard = lambda: server.session_store.hold_active_run(**binding)
    token = _current_check.set(guard if binding is not None else None)
    try:
        check_current_mutation()
        return await callback()
    except StaleFencingToken:
        _reject(401, error_code="agent_run_not_active")
    finally:
        _current_check.reset(token)


@contextmanager
def current_mutation_guard():
    guard = _current_check.get()
    with guard() if guard is not None else nullcontext():
        yield


def check_current_mutation():
    with current_mutation_guard():
        pass
# HASHI-Fingerprint: hfp_045490f26b324ab29a338db67c8243e5
