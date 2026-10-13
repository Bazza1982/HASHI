"""PAO cancellation of one accepted Session Run inside its Agent Worker."""

from __future__ import annotations

import asyncio
from typing import Any

from orchestrator import runtime_control, runtime_pending, runtime_session
from orchestrator.session_store import SessionNotFound, TERMINAL_RUN_STATES


def transition_lock(runtime: Any) -> asyncio.Lock:
    lock = getattr(runtime, "_run_cancel_transition_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        runtime._run_cancel_transition_lock = lock
    return lock


def requested_ids(runtime: Any) -> set[str]:
    ids = getattr(runtime, "_targeted_cancel_request_ids", None)
    if not isinstance(ids, set):
        ids = set()
        runtime._targeted_cancel_request_ids = ids
    return ids


def finalizing_ids(runtime: Any) -> set[str]:
    ids = getattr(runtime, "_finalizing_request_ids", None)
    if not isinstance(ids, set):
        ids = set()
        runtime._finalizing_request_ids = ids
    return ids


async def claim_final_result(runtime: Any, item: Any) -> bool:
    """Linearize final-result publication against an accepted exact stop.

    Once claimed, publication may already have reached a listener or transport;
    cancellation must report that boundary rather than promise a false stop.
    No network I/O is performed while holding the transition lock.
    """
    async with transition_lock(runtime):
        run_id = getattr(item, "run_id", None)
        store = getattr(runtime, "session_store", None)
        run = store.get_run(run_id) if run_id and store is not None else None
        stopped = bool(run and run["request_id"] == item.request_id
                       and run["agent_id"] == runtime.name and run["state"] == "stopped")
        if item.request_id in requested_ids(runtime) or stopped:
            return False
        finalizing_ids(runtime).add(item.request_id)
        return True


def queued_run_is_terminal(runtime: Any, item: Any) -> bool:
    """Fence a queued item whose durable Run was cancelled before dequeue."""
    run_id = getattr(item, "run_id", None)
    store = getattr(runtime, "session_store", None)
    if not run_id or store is None:
        return False
    run = store.get_run(run_id)
    return (run["request_id"] == item.request_id
            and run["agent_id"] == runtime.name
            and run["state"] in TERMINAL_RUN_STATES)


async def finish_queued_request(runtime: Any, item: Any, *, error: str = "Cancelled before execution") -> None:
    """Settle an exact queued request through the normal Session/listener path."""
    await runtime._notify_request_listeners(item.request_id, {
        "request_id": item.request_id,
        "success": False,
        "text": None,
        "error": error,
        "source": item.source,
        "summary": item.summary,
        "interrupted": True,
        "interrupt_reason": "user_stop",
    })


async def cancel_session_run(
    runtime: Any, *, owner_id: str, session_id: str, run_id: str,
    request_id: str, reason: str = "cancelled_by_user",
) -> dict[str, Any]:
    """Target one Run; never stop the Agent or a different pending request."""
    store = runtime_session.ensure_store(runtime)
    run = store.get_run(run_id, owner_id=owner_id)
    if (run["session_id"] != session_id or run["agent_id"] != runtime.name
            or run["request_id"] != request_id):
        raise SessionNotFound("run not found in selected Session and Agent")

    async with transition_lock(runtime):
        run = store.get_run(run_id, owner_id=owner_id)
        if run["state"] in TERMINAL_RUN_STATES:
            return {"ok": True, "request_id": request_id, "run_id": run_id,
                    "session_id": session_id, "status": run["state"], "terminal": True,
                    "already_terminal": True}
        if request_id in finalizing_ids(runtime):
            return {"ok": False, "request_id": request_id, "run_id": run_id,
                    "session_id": session_id, "status": "finalizing", "terminal": False,
                    "error_code": "final_result_committed",
                    "error": "The final result is already being published; cancellation was not accepted."}

        removed = await runtime_pending.take_ready_exact(runtime, request_id, session_id=session_id)
        if removed is not None:
            await finish_queued_request(runtime, removed)
            await runtime_pending.complete_removed_turn(runtime, request_id)
            return {"ok": True, "request_id": request_id, "run_id": run_id,
                    "session_id": session_id, "status": "stopped", "terminal": True}

        active = getattr(runtime, "current_request_meta", None)
        execution = next((value for value in getattr(runtime, "_session_executions", {}).values()
                          if value.active_request == request_id and value.session_id == session_id), None)
        if execution is not None:
            active = execution.values.get("current_request_meta")
        active_matches = (isinstance(active, dict)
                          and str(active.get("request_id") or "") == request_id
                          and runtime_control._meta_session_id(active) == session_id
                          and str(active.get("hashi_run_id") or "") == run_id)
        tasks = getattr(runtime, "_generation_tasks_by_request", None)
        task = tasks.get(request_id) if isinstance(tasks, dict) else None
        completion_tasks = getattr(runtime, "_background_completion_tasks_by_request", None)
        completion = (completion_tasks.get(request_id)
                      if isinstance(completion_tasks, dict) else None)
        background_ids = getattr(runtime, "_background_request_ids", set())
        if (active_matches or isinstance(task, asyncio.Task)
                or isinstance(completion, asyncio.Task) or request_id in background_ids):
            requested_ids(runtime).add(request_id)
            if active_matches:
                from orchestrator.runtime_execution import bind
                with bind(execution):
                    runtime_control.mark_user_interrupt(runtime, "user_stop", request_meta=active)
            if isinstance(task, asyncio.Task) and not task.done():
                task.cancel()
            elif (isinstance(completion, asyncio.Task) and not completion.done()
                  and request_id in getattr(runtime, "_background_completion_started_request_ids", set())):
                completion.cancel()
            return {"ok": True, "request_id": request_id, "run_id": run_id,
                    "session_id": session_id, "status": "cancellation_requested",
                    "terminal": False}

        # An admitted Run can still be between the durable write and ready-queue
        # insertion. Fence it durably, then let the queue processor settle any
        # later-arriving item without starting the provider.
        if run["state"] == "queued":
            stopped = store.cancel_run(run_id, owner_id=owner_id, reason=reason)
            # A quota waiter has left the ready queue but still owns no backend.
            task = getattr(runtime,"_session_execution_tasks",{}).get(request_id)
            if isinstance(task,asyncio.Task) and not task.done():
                task.cancel()
            return {"ok": True, "request_id": request_id, "run_id": run_id,
                    "session_id": session_id, "status": stopped["state"],
                    "terminal": stopped["state"] in TERMINAL_RUN_STATES}

        if run["state"] == "running":
            # A Worker recovered with a durable running Run but no matching
            # local execution. Fence the orphan so it cannot later commit a
            # result under this Run; keep the inconsistency visible in logs.
            logger = getattr(runtime, "logger", None)
            if logger is not None:
                logger.warning("Fencing running Run without local execution: %s", run_id)
            stopped = store.cancel_run(run_id, owner_id=owner_id, reason=reason)
            await runtime_pending.complete_removed_turn(runtime, request_id)
            return {"ok": True, "request_id": request_id, "run_id": run_id,
                    "session_id": session_id, "status": stopped["state"],
                    "terminal": stopped["state"] in TERMINAL_RUN_STATES,
                    "execution_missing": True}

        return {"ok": False, "request_id": request_id, "run_id": run_id,
                "session_id": session_id, "status": "cancellation_unconfirmed",
                "terminal": False, "error_code": "unexpected_run_state",
                "error": f"Cannot cancel Run in state {run['state']!r}."}
