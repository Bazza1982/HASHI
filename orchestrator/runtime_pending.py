from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class PendingRemoval:
    ready: int = 0
    delayed: int = 0

    @property
    def total(self) -> int:
        return self.ready + self.delayed


def pending_lock(runtime: Any) -> asyncio.Lock:
    """Return the per-runtime lock shared by queue recall and delay dispatch."""

    lock = getattr(runtime, "_pending_queue_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        runtime._pending_queue_lock = lock
    return lock


def scheduler_for(runtime: Any) -> Any | None:
    orchestrator = getattr(runtime, "orchestrator", None)
    return (
        getattr(orchestrator, "scheduler", None) if orchestrator is not None else None
    )


def _value_session_id(value: Any) -> str:
    if isinstance(value, dict):
        direct = value.get("session_id")
        metadata = value.get("request_metadata")
        nested = metadata.get("session_id") if isinstance(metadata, dict) else None
        return str(direct or nested or "")
    return str(getattr(value, "session_id", "") or "")


def _in_runtime_session(runtime: Any, value: Any, session_id: str | None) -> bool:
    if not session_id:
        return True
    value_session_id = _value_session_id(value)
    if value_session_id:
        return value_session_id == str(session_id)
    return str(getattr(runtime, "default_session_id", "") or "") == str(session_id)


def ready_items(runtime: Any, *, session_id: str | None = None) -> list[Any]:
    queue = getattr(runtime, "queue", None)
    raw = getattr(queue, "_queue", None)
    items = list(raw) if raw is not None else []
    return [item for item in items if _in_runtime_session(runtime, item, session_id)]


def delayed_count_now(
    runtime: Any,
    *,
    agent_name: str | None = None,
    session_id: str | None = None,
) -> int:
    if session_id:
        return len(
            delayed_messages_now(
                runtime, agent_name=agent_name, session_id=session_id
            )
        )
    scheduler = scheduler_for(runtime)
    counter = getattr(scheduler, "count_delayed_messages", None)
    if not callable(counter):
        return 0
    return int(counter(agent_name or getattr(runtime, "name", "")) or 0)


def delayed_messages_now(
    runtime: Any,
    *,
    agent_name: str | None = None,
    session_id: str | None = None,
) -> list[dict[str, Any]]:
    scheduler = scheduler_for(runtime)
    reader = getattr(scheduler, "list_delayed_messages_now", None)
    if not callable(reader):
        return []
    records = list(reader(agent_name or getattr(runtime, "name", "")) or [])
    return [
        record
        for record in records
        if _in_runtime_session(runtime, record, session_id)
    ]


async def delayed_messages(
    runtime: Any,
    *,
    agent_name: str | None = None,
    session_id: str | None = None,
) -> list[dict[str, Any]]:
    scheduler = scheduler_for(runtime)
    reader = getattr(scheduler, "list_delayed_messages", None)
    if not callable(reader):
        return []
    records = list(await reader(agent_name or getattr(runtime, "name", "")) or [])
    return [
        record
        for record in records
        if _in_runtime_session(runtime, record, session_id)
    ]


async def delayed_count(
    runtime: Any,
    *,
    agent_name: str | None = None,
    session_id: str | None = None,
) -> int:
    return len(
        await delayed_messages(
            runtime, agent_name=agent_name, session_id=session_id
        )
    )


def _drain_ready_queue(runtime: Any) -> list[Any]:
    queue = getattr(runtime, "queue", None)
    if queue is None:
        return []
    items: list[Any] = []
    while True:
        try:
            items.append(queue.get_nowait())
            queue.task_done()
        except asyncio.QueueEmpty:
            break
    return items


def _restore_ready_queue(runtime: Any, items: list[Any]) -> None:
    queue = getattr(runtime, "queue", None)
    if queue is None:
        return
    for item in items:
        queue.put_nowait(item)


def _created_timestamp(value: Any, *, fallback: float) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return fallback


def _request_id(item: Any) -> str:
    return str(getattr(item, "request_id", "") or "")


def _id_matches(candidate: str, requested: str) -> bool:
    wanted = str(requested or "").strip()
    return bool(wanted and (candidate == wanted or candidate.endswith(wanted)))


def _finish_removed_runs(runtime: Any, items: list[Any]) -> None:
    store = getattr(runtime, "session_store", None)
    finish = getattr(store, "finish_request", None)
    if not callable(finish):
        return
    for item in items:
        request_id = _request_id(item)
        if not request_id:
            continue
        try:
            finish(
                request_id,
                success=False,
                error_text="request removed from the ready queue",
                failure_state="superseded",
            )
        except Exception:
            logger = getattr(runtime, "logger", None)
            if logger is not None:
                logger.warning("Could not terminalize removed Session Run %s", request_id)


async def clear_ready(runtime: Any, *, session_id: str | None = None) -> int:
    async with pending_lock(runtime):
        drained = _drain_ready_queue(runtime)
        if session_id:
            removed = [
                item
                for item in drained
                if _in_runtime_session(runtime, item, session_id)
            ]
            removed_ids = {id(item) for item in removed}
            kept = [item for item in drained if id(item) not in removed_ids]
            _restore_ready_queue(runtime, kept)
        else:
            removed = drained
        _finish_removed_runs(runtime, removed)
        return len(removed)


async def recall_pending(
    runtime: Any,
    count: int | None = None,
    *,
    session_id: str | None = None,
) -> PendingRemoval:
    """Recall the newest READY+FUTURE requests in one Session."""

    async with pending_lock(runtime):
        ready = ready_items(runtime, session_id=session_id)
        delayed = await delayed_messages(runtime, session_id=session_id)

        candidates: list[tuple[float, int, str, Any]] = []
        for index, item in enumerate(ready):
            candidates.append(
                (
                    _created_timestamp(
                        getattr(item, "created_at", None), fallback=float(index)
                    ),
                    index,
                    "ready",
                    item,
                )
            )
        offset = len(candidates)
        for index, record in enumerate(delayed):
            candidates.append(
                (
                    _created_timestamp(
                        record.get("created_at"), fallback=float(offset + index)
                    ),
                    offset + index,
                    "delayed",
                    record,
                )
            )

        if count is None:
            selected = candidates
        else:
            selected = sorted(
                candidates, key=lambda entry: (entry[0], entry[1]), reverse=True
            )[: max(0, int(count))]

        selected_ready = {id(entry[3]) for entry in selected if entry[2] == "ready"}
        selected_delay_ids = {
            str(entry[3].get("id") or "")
            for entry in selected
            if entry[2] == "delayed" and entry[3].get("id")
        }

        removed_delayed = 0
        if selected_delay_ids:
            scheduler = scheduler_for(runtime)
            cancel = getattr(scheduler, "cancel_delayed_messages", None)
            if callable(cancel):
                removed_delayed = len(
                    await cancel(
                        getattr(runtime, "name", ""),
                        delay_ids=selected_delay_ids,
                    )
                )

        drained = _drain_ready_queue(runtime)
        kept = [item for item in drained if id(item) not in selected_ready]
        _restore_ready_queue(runtime, kept)
        removed_ready = [item for item in drained if id(item) in selected_ready]
        _finish_removed_runs(runtime, removed_ready)
        return PendingRemoval(
            ready=len(drained) - len(kept),
            delayed=removed_delayed,
        )


async def cancel_pending_by_id(
    runtime: Any,
    request_id: str,
    *,
    session_id: str | None = None,
) -> PendingRemoval:
    """Cancel one matching READY request or FUTURE delayed message."""

    async with pending_lock(runtime):
        drained = _drain_ready_queue(runtime)
        matched_ready: list[Any] = []
        kept: list[Any] = []
        for item in drained:
            if (
                not matched_ready
                and _in_runtime_session(runtime, item, session_id)
                and _id_matches(_request_id(item), request_id)
            ):
                matched_ready.append(item)
            else:
                kept.append(item)

        if matched_ready:
            _restore_ready_queue(runtime, kept)
            _finish_removed_runs(runtime, matched_ready)
            return PendingRemoval(ready=1)

        _restore_ready_queue(runtime, drained)
        records = await delayed_messages(runtime, session_id=session_id)
        matches = [
            record
            for record in records
            if _id_matches(str(record.get("id") or ""), request_id)
        ]
        if len(matches) != 1:
            return PendingRemoval()

        scheduler = scheduler_for(runtime)
        cancel = getattr(scheduler, "cancel_delayed_messages", None)
        if not callable(cancel):
            return PendingRemoval()
        removed = await cancel(
            getattr(runtime, "name", ""),
            delay_ids={str(matches[0]["id"])},
        )
        return PendingRemoval(delayed=len(removed))
