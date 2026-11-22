"""Worker-owned FC dispatch for durable incremental Telegram deliverables."""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any

from orchestrator.frontend_database import (
    DatabaseWorkBusy,
    is_sqlite_busy,
    record_loop_zero_progress,
    run_database_work,
)
from orchestrator.frontend_connector_registry import endpoint_id_for
from orchestrator.frontend_delivery import route_destination
from orchestrator.runtime_delivery import dispatch_claimed_telegram_event
from orchestrator.runtime_session import ensure_store

IDLE_SWEEP_BASE_SECONDS = 2.0
IDLE_SWEEP_MAX_SECONDS = 60.0
ACTIVE_RUN_SWEEP_SECONDS = 2.0


def _telegram_outbound_ready(runtime: Any) -> bool:
    return bool(
        runtime is not None
        and getattr(runtime, "telegram_connected", False)
        and getattr(getattr(runtime, "app", None), "bot", None) is not None
    )


def _has_active_tasks(mapping: Any) -> bool:
    if not isinstance(mapping, dict):
        return False
    for task in mapping.values():
        if isinstance(task, asyncio.Task) and not task.done():
            return True
    return False


def _runtime_has_active_run(runtime: Any) -> bool:
    if (
        getattr(runtime, "is_generating", False)
        or getattr(runtime, "is_processing", False)
        or getattr(runtime, "current_request_meta", None) is not None
    ):
        return True
    if getattr(runtime, "_background_request_ids", None):
        return True
    if getattr(runtime, "_outstanding_request_ids", None):
        return True
    if _has_active_tasks(
        getattr(runtime, "_background_completion_tasks_by_request", None)
    ) or _has_active_tasks(
        getattr(runtime, "_generation_tasks_by_request", None)
    ):
        return True
    queue = getattr(runtime, "queue", None)
    if queue is not None and callable(getattr(queue, "empty", None)):
        try:
            if not queue.empty():
                return True
        except Exception:
            pass
    return False


def _get_wake_event(runtime: Any) -> asyncio.Event:
    event = getattr(runtime, "_incremental_telegram_wake_event", None)
    if not isinstance(event, asyncio.Event):
        event = asyncio.Event()
        runtime._incremental_telegram_wake_event = event
    return event


def notify_incremental_telegram_delivery(runtime: Any) -> None:
    """Wake the incremental Telegram delivery loop immediately (non-blocking)."""
    if runtime is None:
        return
    event = getattr(runtime, "_incremental_telegram_wake_event", None)
    if isinstance(event, asyncio.Event):
        event.set()


def bind_telegram_delivery_wakeup(
    runtime: Any, *, peer_notifier: Any = None
) -> None:
    """Bind post-commit SessionStore notifications to wake Telegram delivery.

    Fires only AFTER SQLite commit of a genuine Telegram-deliverable task.
    Disconnected workers remain completely silent (zero wakeups/SQLite polls).
    Cross-agent writes from this Worker forward through ``peer_notifier`` if
    provided (reusing the existing Function Worker IPC channel).
    """
    if runtime is None:
        return
    if peer_notifier is not None:
        runtime._telegram_delivery_peer_notifier = peer_notifier
    try:
        store = ensure_store(runtime)
    except Exception:
        return
    register = getattr(store, "register_telegram_delivery_listener", None)
    if not callable(register):
        return
    unbind_telegram_delivery_wakeup(runtime)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    own_agent = str(getattr(runtime, "name", "") or "").lower()

    def _on_committed_telegram_task(
        agent_id: str, _event_id: str, _kind: str
    ) -> None:
        target_agent = str(agent_id or "").lower()
        if target_agent == own_agent:
            if not (
                getattr(runtime, "is_shutting_down", False)
                or _telegram_outbound_ready(runtime)
            ):
                return
            if loop is not None and not loop.is_closed():
                try:
                    loop.call_soon_threadsafe(
                        notify_incremental_telegram_delivery, runtime
                    )
                    return
                except RuntimeError:
                    pass
            notify_incremental_telegram_delivery(runtime)
            return
        notifier = getattr(runtime, "_telegram_delivery_peer_notifier", None)
        if callable(notifier) and target_agent:
            if loop is not None and not loop.is_closed():
                try:
                    loop.call_soon_threadsafe(notifier, target_agent)
                    return
                except RuntimeError:
                    pass
            notifier(target_agent)

    register(_on_committed_telegram_task)
    runtime._telegram_delivery_store_listener = (store, _on_committed_telegram_task)


def unbind_telegram_delivery_wakeup(runtime: Any) -> None:
    if runtime is None:
        return
    binding = getattr(runtime, "_telegram_delivery_store_listener", None)
    if isinstance(binding, tuple) and len(binding) == 2:
        store, listener = binding
        unregister = getattr(store, "unregister_telegram_delivery_listener", None)
        if callable(unregister):
            try:
                unregister(listener)
            except Exception:
                pass
    runtime._telegram_delivery_store_listener = None


async def dispatch_incremental_telegram_event(
    store: Any,
    runtime: Any,
    *,
    session_id: str,
    owner_id: str,
    request_id: str,
    event_id: str,
    delivery_route: Any = None,
    recovery: bool = False,
) -> bool:
    """Claim one frozen endpoint and use the canonical Telegram FC renderer."""
    if (
        runtime is None
        or not getattr(runtime, "telegram_connected", False)
        or getattr(getattr(runtime, "app", None), "bot", None) is None
    ):
        return False
    def claim():
        route = delivery_route
        if route is None:
            run = store.get_run_by_request(request_id, owner_id=owner_id)
            route = run.get("delivery_route")
        destination = route_destination(route, "telegram")
        if destination is None:
            return [], None
        channel_key = str(destination["channel_key"])
        endpoint_id = endpoint_id_for(
            "telegram", ingress_transport="telegram", channel_key=channel_key
        )
        claims = store.claim_delivery_outbox(
            session_id=session_id,
            owner_id=owner_id,
            worker_id=f"fc-telegram-deliverable-{event_id[:48]}",
            event_id=event_id,
            connector_id="telegram",
            endpoint_id=endpoint_id,
            limit=1,
            lease_seconds=3600,
        )
        return claims, channel_key
    claims, channel_key = await run_database_work(claim, _db_role='worker', _db_instance=store.instance_id,
                                                       _db_operation='telegram.claim')
    if not claims:
        return False
    try:
        await dispatch_claimed_telegram_event(
            runtime,
            chat_id=int(channel_key),
            store=store,
            claim=claims[0],
            frontend_owner_id=owner_id,
            request_id=request_id,
            purpose="incremental-deliverable",
            recovery=recovery,
        )
    except Exception as exc:
        # The claim is now unknown or failed. An uncertain transport effect
        # must be reconciled from evidence, never replayed by this loop.
        runtime.logger.warning(
            "Incremental Telegram FC dispatch failed for %s (%s)",
            event_id, type(exc).__name__,
        )
    return True


async def dispatch_pending_telegram_deliverables(
    runtime: Any, *, limit: int = 20
) -> int:
    """Drain this Agent's persisted publication tasks, including after restart."""
    if (
        not getattr(runtime, "telegram_connected", False)
        or getattr(getattr(runtime, "app", None), "bot", None) is None
    ):
        return 0
    def scan():
        store = ensure_store(runtime)
        from orchestrator.run_questions import RunQuestions
        questions = RunQuestions(store)
        questions.refresh_pending(agent_id=runtime.name, busy_timeout_ms=100)
        candidates = questions.pending_telegram_deliveries(
            agent_id=runtime.name, limit=limit, busy_timeout_ms=100)
        candidates.extend(store.pending_incremental_telegram_deliveries(
            agent_id=runtime.name, limit=limit))
        return store, candidates
    store, candidates = await run_database_work(scan, _db_role='worker',
                                                 _db_instance=getattr(runtime.global_config, 'instance_id', 'unknown'),
                                                 _db_operation='telegram.scan')
    from orchestrator.frontend_run_questions import refresh_telegram_question_renditions
    dispatched = 0
    seen = set()
    for candidate in candidates:
        if candidate["event_id"] in seen:
            continue
        seen.add(candidate["event_id"])
        if await dispatch_incremental_telegram_event(
            store,
            runtime,
            session_id=str(candidate["session_id"]),
            owner_id=str(candidate["owner_id"]),
            request_id=str(candidate["request_id"]),
            event_id=str(candidate["event_id"]),
            delivery_route=candidate.get("delivery_route"),
            recovery=True,
        ):
            dispatched += 1
    await refresh_telegram_question_renditions(runtime, store)
    return dispatched


async def incremental_telegram_delivery_loop(runtime: Any) -> None:
    wake_event = _get_wake_event(runtime)
    busy_count = 0
    idle_streak = 0
    while not getattr(runtime, "is_shutting_down", False):
        # Complete Telegram outbound decoupling: disconnected or local-mode
        # workers must sleep on the wake event with zero SQLite polling.
        if not _telegram_outbound_ready(runtime):
            wake_event.clear()
            if getattr(runtime, "is_shutting_down", False) or _telegram_outbound_ready(runtime):
                continue
            await wake_event.wait()
            wake_event.clear()
            busy_count = 0
            idle_streak = 0
            continue

        wake_event.clear()
        delay = IDLE_SWEEP_BASE_SECONDS
        busy_backoff = False
        try:
            started = time.monotonic()
            dispatched = int(await dispatch_pending_telegram_deliverables(runtime) or 0)
            elapsed = time.monotonic() - started
            record_loop_zero_progress(
                operation="telegram.scan",
                instance=str(getattr(runtime.global_config, "instance_id", "unknown")),
                role="worker",
                duration_s=elapsed,
                items_processed=dispatched,
            )
            busy_count = 0
            if dispatched > 0:
                idle_streak = 0
                delay = IDLE_SWEEP_BASE_SECONDS
            elif _runtime_has_active_run(runtime):
                idle_streak = 0
                delay = ACTIVE_RUN_SWEEP_SECONDS
            else:
                delay = min(
                    IDLE_SWEEP_MAX_SECONDS,
                    IDLE_SWEEP_BASE_SECONDS * (2 ** idle_streak),
                )
                idle_streak = min(idle_streak + 1, 5)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if isinstance(exc, DatabaseWorkBusy) or is_sqlite_busy(exc):
                busy_backoff = True
                busy_count = min(busy_count + 1, 4)
                delay = random.uniform(2.0, min(30.0, 2.0 * (2 ** busy_count)))
                runtime.logger.warning(
                    "Incremental Telegram database busy: operation=sweep instance=%s "
                    "role=worker retry_in=%.1fs",
                    getattr(runtime.global_config, "instance_id", "unknown"), delay)
            else:
                runtime.logger.exception(
                    "Incremental Telegram FC sweep failed: %s", exc)
        if getattr(runtime, "is_shutting_down", False):
            break
        if busy_backoff:
            # Retry discovery after bounded jitter, never a transport effect or answer.
            await asyncio.sleep(delay)
            continue
        if wake_event.is_set():
            wake_event.clear()
            idle_streak = 0
            continue
        try:
            await asyncio.wait_for(wake_event.wait(), timeout=delay)
            wake_event.clear()
            idle_streak = 0
        except asyncio.TimeoutError:
            pass


def start_incremental_telegram_delivery(runtime: Any) -> None:
    _get_wake_event(runtime)
    bind_telegram_delivery_wakeup(runtime)
    task = getattr(runtime, "_incremental_telegram_delivery_task", None)
    if isinstance(task, asyncio.Task) and not task.done():
        return
    runtime._incremental_telegram_delivery_task = asyncio.create_task(
        incremental_telegram_delivery_loop(runtime),
        name=f"incremental-telegram-delivery-{runtime.name}",
    )
# HASHI-Fingerprint: hfp_e304672c47da4171bc6240e6c3ed2182
