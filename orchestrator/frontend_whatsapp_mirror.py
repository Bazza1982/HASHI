"""Deliver a frozen WhatsApp mirror through the standard FC outbox."""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any


async def deliver_whatsapp_mirror(runtime: Any, request_id: str) -> dict[str, Any] | None:
    """Send only an owner-approved mirror; never replay a WhatsApp primary."""

    from orchestrator import runtime_session
    from orchestrator.frontend_delivery import normalize_run_delivery_route
    from orchestrator.frontend_dispatch import FrontendDispatcher, OutcomeConnectorAdapter
    from orchestrator.frontend_projection import render_event_to_plain_text

    store = runtime_session.ensure_store(runtime)
    try:
        run = await asyncio.to_thread(store.get_run_by_request, str(request_id))
    except Exception:
        return None
    try:
        route = normalize_run_delivery_route(run.get("delivery_route"))
    except ValueError:
        return None
    mirror = next(
        (item for item in route["mirrors"] if item["surface"] == "whatsapp"),
        None,
    )
    if mirror is None:
        return None
    session = await asyncio.to_thread(store.get_session, str(run["session_id"]))
    owner_id = str(session["owner_id"])
    if owner_id != runtime_session.owner_id(runtime):
        return None
    worker_id = f"fc-whatsapp-mirror-{request_id}"
    claim = await asyncio.to_thread(
        store.claim_run_delivery_outbox,
        request_id=str(request_id),
        owner_id=owner_id,
        surface="whatsapp",
        channel_key=str(mirror["channel_key"]),
        worker_id=worker_id,
    )
    if not isinstance(claim, Mapping) or claim.get("state") != "claimed":
        return dict(claim) if isinstance(claim, Mapping) else None

    async def send_event(event: Mapping[str, Any], *, endpoint_id: str) -> dict[str, Any]:
        del endpoint_id
        text = render_event_to_plain_text(event).strip()
        if not text:
            return {"attempted": False, "state": "skipped"}
        sender = getattr(getattr(runtime, "orchestrator", None), "send_whatsapp_text", None)
        if not callable(sender):
            return {"attempted": True, "delivered": False, "state": "failed"}
        phone = str(mirror["channel_key"]).split("@", 1)[0]
        ok, _message = await sender(phone, f"[{runtime.name}]: {text}")
        return {
            "attempted": True,
            "delivered": bool(ok),
            "state": "accepted" if ok else "failed",
        }

    dispatcher = FrontendDispatcher(
        store,
        worker_id=worker_id,
        adapters={"whatsapp": OutcomeConnectorAdapter("whatsapp", send_event)},
    )
    return await dispatcher.dispatch_claimed_task(
        claim["claim"],
        session_id=str(run["session_id"]),
        owner_id=owner_id,
    )
