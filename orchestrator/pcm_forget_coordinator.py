"""Resume one confirmed PCM forget operation to a fresh Agent Session."""
from __future__ import annotations

import asyncio
from typing import Any

from orchestrator import runtime_session
from orchestrator.pcm_forget_artifacts import forget_local_memories, quarantine_unproven_continuity
from orchestrator.session_store import SessionConflict


def background_work_active(runtime: Any) -> bool:
    for name in ("_background_tasks", "_context_compaction_tasks"):
        tasks = getattr(runtime, name, None)
        if isinstance(tasks, set) and any(not task.done() for task in tuple(tasks)):
            return True
    return False


async def finish_pending(runtime: Any, update: Any, *, owner_id: str) -> dict[str, Any]:
    """Finish confirmed work; failures retain the durable Agent admission fence."""
    if background_work_active(runtime):
        raise SessionConflict("Agent has active background work")
    store = runtime_session.ensure_store(runtime)
    owner, agent = str(owner_id), str(runtime.name).lower()
    pending = store.pending_pcm_forget(owner_id=owner, agent_id=agent)
    if pending is None:
        raise SessionConflict("no pending forget operation")
    operation = pending["operation_id"]
    steps = set(pending["steps"])
    report: dict[str, Any] = {"operation_id": operation}
    if "local_memory" not in steps:
        report["local_memory"] = await asyncio.to_thread(
            forget_local_memories, store, operation_id=operation, owner_id=owner,
            agent_id=agent, agent_home=runtime.workspace_dir,
        )
    if "continuity" not in steps:
        session_ids = pending["result"].get("all_session_ids", pending["result"]["session_ids"])
        report["quarantined_artifacts"] = await asyncio.to_thread(
            quarantine_unproven_continuity, store, operation_id=operation,
            owner_id=owner, agent_id=agent, agent_home=runtime.workspace_dir,
            session_ids=session_ids,
        )
        store.record_pcm_forget_step(
            owner_id=owner, agent_id=agent, operation_id=operation, step="continuity",
        )
    pending = store.pending_pcm_forget(owner_id=owner, agent_id=agent)
    if pending is None or pending["operation_id"] != operation:
        raise SessionConflict("forget operation changed")
    new_session_id = pending["result"].get("new_session_id")
    newly_created = "backend" not in pending["steps"]
    if newly_created:
        session = store.create_session(owner_id=owner, agent_id=agent, title="New session")
        new_session_id = str(session["session_id"])
    if not new_session_id:
        raise SessionConflict("forget new session unavailable")
    try:
        # A pending operation may outlive the Worker that created its Session.
        # Re-establish the backend every time before allowing new Agent work.
        reset = await runtime_session._reset_cli_backend(runtime, reason="pcm_forget")
        if reset == "unavailable":
            raise SessionConflict("backend could not start a clean session")
        await runtime_session._bind_session(runtime, update, new_session_id)
        # The primary conversation is shared by interactive frontends even if
        # this command arrived through an explicit Session API route.
        store.bind_primary_session(owner_id=owner, agent_id=agent, session_id=new_session_id)
        runtime_session._discard_session_transients(runtime)
        runtime_session._prepare_clean_context(
            runtime, disable_saved_memory=False, clear_session_primer=True,
        )
        if newly_created:
            store.record_pcm_forget_new_session(
                owner_id=owner, agent_id=agent, operation_id=operation, session_id=new_session_id,
            )
    except Exception:
        if newly_created:
            try:
                store.archive_session(new_session_id, deleted=True)
            except Exception:
                pass
        raise
    store.complete_pcm_forget(owner_id=owner, agent_id=agent, operation_id=operation)
    report["new_session_id"] = new_session_id
    report["central_memory_quarantined"] = True
    return report
