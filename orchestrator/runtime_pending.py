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


def ready_items(runtime: Any) -> list[Any]:
    queue = getattr(runtime, "queue", None)
    raw = getattr(queue, "_queue", None)
    return list(raw) if raw is not None else []


def delayed_count_now(runtime: Any, *, agent_name: str | None = None) -> int:
    scheduler = scheduler_for(runtime)
    counter = getattr(scheduler, "count_delayed_messages", None)
    if not callable(counter):
        return 0
    return int(counter(agent_name or getattr(runtime, "name", "")) or 0)


def delayed_messages_now(
    runtime: Any,
    *,
    agent_name: str | None = None,
) -> list[dict[str, Any]]:
    scheduler = scheduler_for(runtime)
    reader = getattr(scheduler, "list_delayed_messages_now", None)
    if not callable(reader):
        return []
    return list(reader(agent_name or getattr(runtime, "name", "")) or [])


async def delayed_messages(
    runtime: Any,
    *,
    agent_name: str | None = None,
) -> list[dict[str, Any]]:
    scheduler = scheduler_for(runtime)
    reader = getattr(scheduler, "list_delayed_messages", None)
    if not callable(reader):
        return []
    return list(await reader(agent_name or getattr(runtime, "name", "")) or [])


async def delayed_count(runtime: Any, *, agent_name: str | None = None) -> int:
    return len(await delayed_messages(runtime, agent_name=agent_name))


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


async def clear_ready(runtime: Any) -> int:
    async with pending_lock(runtime):
        removed = _drain_ready_queue(runtime)
        return len(removed)


async def recall_pending(runtime: Any, count: int | None = None) -> PendingRemoval:
    """Recall all or the newest N READY+FUTURE requests for one runtime."""

    async with pending_lock(runtime):
        ready = ready_items(runtime)
        delayed = await delayed_messages(runtime)

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
        return PendingRemoval(
            ready=len(drained) - len(kept),
            delayed=removed_delayed,
        )


async def cancel_pending_by_id(runtime: Any, request_id: str) -> PendingRemoval:
    """Cancel one matching READY request or FUTURE delayed message."""

    async with pending_lock(runtime):
        drained = _drain_ready_queue(runtime)
        matched_ready: list[Any] = []
        kept: list[Any] = []
        for item in drained:
            if not matched_ready and _id_matches(_request_id(item), request_id):
                matched_ready.append(item)
            else:
                kept.append(item)

        if matched_ready:
            _restore_ready_queue(runtime, kept)
            return PendingRemoval(ready=1)

        _restore_ready_queue(runtime, drained)
        records = await delayed_messages(runtime)
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
