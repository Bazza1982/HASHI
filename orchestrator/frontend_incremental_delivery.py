"""Worker-owned FC dispatch for durable incremental Telegram deliverables."""

from __future__ import annotations

import asyncio
from typing import Any

from orchestrator.frontend_connector_registry import endpoint_id_for
from orchestrator.frontend_delivery import route_destination
from orchestrator.runtime_delivery import dispatch_claimed_telegram_event
from orchestrator.runtime_session import ensure_store


async def dispatch_incremental_telegram_event(
    store: Any,
    runtime: Any,
    *,
    session_id: str,
    owner_id: str,
    request_id: str,
    event_id: str,
) -> bool:
    """Claim one frozen endpoint and use the canonical Telegram FC renderer."""
    if (
        runtime is None
        or not getattr(runtime, "telegram_connected", False)
        or getattr(getattr(runtime, "app", None), "bot", None) is None
    ):
        return False
    run = store.get_run_by_request(request_id, owner_id=owner_id)
    destination = route_destination(run.get("delivery_route"), "telegram")
    if destination is None:
        return False
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
    store = ensure_store(runtime)
    candidates = store.pending_incremental_telegram_deliveries(
        agent_id=runtime.name, limit=limit
    )
    dispatched = 0
    for candidate in candidates:
        if await dispatch_incremental_telegram_event(
            store,
            runtime,
            session_id=str(candidate["session_id"]),
            owner_id=str(candidate["owner_id"]),
            request_id=str(candidate["request_id"]),
            event_id=str(candidate["event_id"]),
        ):
            dispatched += 1
    return dispatched


async def incremental_telegram_delivery_loop(runtime: Any) -> None:
    while not getattr(runtime, "is_shutting_down", False):
        try:
            await dispatch_pending_telegram_deliverables(runtime)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            runtime.logger.exception(
                "Incremental Telegram FC sweep failed: %s", exc
            )
        await asyncio.sleep(2.0)


def start_incremental_telegram_delivery(runtime: Any) -> None:
    task = getattr(runtime, "_incremental_telegram_delivery_task", None)
    if isinstance(task, asyncio.Task) and not task.done():
        return
    runtime._incremental_telegram_delivery_task = asyncio.create_task(
        incremental_telegram_delivery_loop(runtime),
        name=f"incremental-telegram-delivery-{runtime.name}",
    )
