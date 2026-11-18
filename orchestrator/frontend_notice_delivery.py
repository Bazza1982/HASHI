"""Durable PAO notification publication with independent Connector receipts."""

from __future__ import annotations

import json

from orchestrator import runtime_session
from orchestrator.frontend_connector_registry import endpoint_id_for
from orchestrator.frontend_projection import transport_text_component


async def publish_run_capacity_notice(runtime, item, event):
    """Publish one internal recovery notice under the current Run fence.

    The outbox and presentation row commit together, using the Run's own
    immutable route.  A stopped or replaced Run cannot enqueue a later notice.
    """
    if bool(getattr(item, "silent", False)):
        return False
    run_id = str(getattr(item, "run_id", "") or "").strip()
    owner_id = str(getattr(item, "owner_id", "") or "").strip()
    session_id = str(getattr(item, "session_id", "") or "").strip()
    request_id = str(getattr(item, "request_id", "") or "").strip()
    event_id = str(getattr(event, "event_id", "") or "").strip()
    text = str(getattr(event, "summary", "") or "").strip()
    fence = getattr(item, "fencing_token", None)
    generation = getattr(item, "context_generation", None)
    if not all((run_id, owner_id, session_id, request_id, event_id, text)):
        return False  # Legacy non-Session callers retain their existing presentation.
    if type(fence) is not int or type(generation) is not int:
        return False
    store = runtime_session.ensure_store(runtime)
    worker_id = f"{getattr(runtime.global_config, 'instance_id', 'HASHI')}:{runtime.name}"
    binding = {
        "run_id": run_id, "owner_id": owner_id, "agent_id": runtime.name,
        "session_id": session_id, "request_id": request_id,
        "context_generation": generation, "fencing_token": fence, "worker_id": worker_id,
    }

    def commit():
        from orchestrator.frontend_delivery import normalize_run_delivery_route
        from orchestrator.frontend_projection import build_frontend_presentation_context

        with store.hold_active_run(**binding) as connection:
            row = connection.execute(
                "SELECT delivery_route_json FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            raw_route = json.loads(row["delivery_route_json"] or "{}")
            if not raw_route:
                return False  # Do not invent a destination for older Runs.
            route = normalize_run_delivery_route(raw_route)
            context = build_frontend_presentation_context(
                text=text, content_format="plain-text", presentation_channel="notification",
                message_context={"delivery_recovery": {
                    "category": "active_run_notice", "run_id": run_id,
                }},
            )
            return store.append_presentation_message(
                session_id=session_id, owner_id=owner_id, agent_id=runtime.name,
                role="assistant", text=text, source="frontend.capacity_recovery_notice",
                idempotency_key=f"capacity-recovery:{request_id}:{event_id}",
                presentation_channel="notification", history_eligible=False,
                message_context=context, outbox=True, delivery_route=route,
                expected_context_generation=generation, _connection=connection, _run_id=run_id,
            )

    from orchestrator.frontend_database import run_database_work
    return await run_database_work(
        commit, _db_role="worker", _db_instance=store.instance_id,
        _db_operation="capacity.notice.publish",
    )


async def deliver_notice(
    kernel, *, agent_id, origin, text, publication_id, send_notice,
    recovery_category="command_result", recovery=False, original_at=None,
):
    """Retry publication/transport only, never the operation that produced it."""
    message = runtime_session.record_kernel_presentation_notice(
        kernel,
        agent_id=agent_id,
        text=text,
        idempotency_key=publication_id,
        origin=origin,
        recovery_category=recovery_category,
        original_at=original_at,
    )
    if not message:
        return {"sent": False, "retry_safe": True}
    presentation = {
        "message_id": message["message_id"],
        "session_id": message["session_id"],
        "text": transport_text_component(text, "telegram-html")["text"],
    }
    result = {
        "sent": True,
        "sender": origin["surface"],
        "message_id": message["message_id"],
        "presentation_message": presentation,
        "delivery_event_id": message["delivery_event_id"],
    }
    # Non-Telegram destinations consume the canonical FC event. Their tasks
    # remain independent; publication is not a claim that a browser displayed it.
    if origin["surface"] != "telegram":
        return result
    config = getattr(kernel, "global_cfg", None) or kernel.global_config
    store = getattr(
        kernel, "session_store", None
    ) or runtime_session.SessionStore.from_global_config(config)
    owner = str(
        origin.get("owner_id") or runtime_session.SessionStore.owner_id_for(config)
    )
    channel = str(origin.get("session_channel_key") or origin["chat_id"])
    endpoint = endpoint_id_for(
        "telegram", ingress_transport="telegram", channel_key=channel
    )
    runtime_map = kernel._runtime_map()
    if runtime_map and not any(getattr(handle, "telegram_connected", False)
                               for handle in runtime_map.values()):
        # Durable publication remains pending; offline time is not an attempt.
        return {**result, "sent": False, "deferred": True, "retry_safe": True}
    claims = store.claim_delivery_outbox(
        session_id=message["session_id"],
        owner_id=owner,
        worker_id="runtime-notice:" + publication_id,
        event_id=message["delivery_event_id"],
        connector_id="telegram",
        endpoint_id=endpoint,
        limit=1,
    )
    if not claims:
        receipts = store.frontend_delivery_receipts(
            session_id=message["session_id"],
            owner_id=owner,
            event_id=message["delivery_event_id"],
        )
        if any(r["endpoint_id"] == endpoint and r["status"] == "delivered" for r in receipts):
            return result
        task = store.frontend_delivery_task_result(
            session_id=message["session_id"], owner_id=owner,
            event_id=message["delivery_event_id"], connector_id="telegram", endpoint_id=endpoint,
        ) or {}
        state = str(task.get("state") or "unknown")
        if state in {"pending", "retry", "claimed"}:
            return {**result, "sent": False, "deferred": True, "retry_safe": True}
        return {**result, "sent": False, "delivery_state": state if state in {
            "unknown", "failed", "expired", "suppressed"
        } else "unknown"}
    claim = claims[0]
    from orchestrator.frontend_dispatch import project_claimed_frontend_event
    from orchestrator.frontend_telegram_replay import (
        finish_without_telegram, telegram_replay_decision,
    )
    event = project_claimed_frontend_event(
        store, claim, session_id=message["session_id"], owner_id=owner,
    )
    decision = telegram_replay_decision(
        store, claim, event, owner_id=owner, agent_id=agent_id, chat_id=int(origin["chat_id"]),
        recovery=recovery or claim["attempt_count"] > 1,
    )
    if decision.state != "send":
        finish_without_telegram(store, claim, owner_id=owner, decision=decision)
        return {**result, "sent": False, "delivery_state": decision.state}
    rendered_text = text
    if recovery or claim["attempt_count"] > 1:
        from types import SimpleNamespace
        from orchestrator.ui_language import preferred_locale
        locale = preferred_locale(SimpleNamespace(global_config=config), actor_id=int(origin["chat_id"]))
        rendered_text = decision.label(locale=locale) + "\n\n" + text
    try:
        delivered = await send_notice(
            kernel,
            source_agent=agent_id,
            operation_id=publication_id,
            notice_kind="final",
            chat_id=int(origin["chat_id"]),
            thread_id=origin.get("thread_id"),
            render_text=lambda *_: rendered_text,
        )
    except Exception:
        store.complete_delivery_outbox(
            outbox_id=claim["outbox_id"],
            lease_token=claim["lease_token"],
            status="unknown",
            error_code="notice_transport_unknown",
        )
        return {**result, "sent": False, "delivery_state": "unknown"}
    if not delivered.get("sent"):
        if delivered.get("retry_safe") is True:
            store.complete_delivery_outbox(
                outbox_id=claim["outbox_id"], lease_token=claim["lease_token"],
                status="retry", retry_safe=True, error_code="notice_transport_retryable",
            )
            return {**result, **delivered}
        if delivered.get("delivery_state") != "failed":
            store.complete_delivery_outbox(
                outbox_id=claim["outbox_id"], lease_token=claim["lease_token"],
                status="unknown", error_code="notice_transport_unknown",
            )
            return {**result, **delivered, "sent": False, "delivery_state": "unknown"}
    sent_id = delivered.get("message_id")
    status = (
        "delivered"
        if delivered.get("sent") and sent_id is not None
        else "accepted"
        if delivered.get("sent")
        else "failed"
    )
    store.record_frontend_delivery_receipt(
        session_id=message["session_id"],
        owner_id=owner,
        receipt={
            "type": "hashi.delivery-receipt",
            "version": 1,
            "event_id": message["delivery_event_id"],
            "endpoint_id": endpoint,
            "status": status,
            "proof": {"type": "telegram-message-id", "value": str(sent_id)}
            if status == "delivered"
            else None,
        },
    )
    store.complete_delivery_outbox(
        outbox_id=claim["outbox_id"],
        lease_token=claim["lease_token"],
        status="completed" if delivered.get("sent") else "failed",
    )
    return {**result, **delivered}


async def publish_runtime_notice(runtime, *, origin, text, publication_id):
    """An active Worker uses its registered canonical Telegram dispatcher."""
    from orchestrator import runtime_delivery

    message = runtime_session.record_kernel_presentation_notice(
        runtime,
        agent_id=runtime.name,
        text=text,
        idempotency_key=publication_id,
        content_format="plain-text",
        origin=origin,
    )
    if message is None:
        raise RuntimeError(
            "notice publication is pending; original Session was not changed"
        )
    if origin["surface"] == "telegram":
        await runtime_delivery.send_long_message(
            runtime,
            chat_id=int(origin["chat_id"]),
            text=text,
            request_id=publication_id,
            purpose="notification",
            frontend_event_id=message["delivery_event_id"],
            frontend_session_id=message["session_id"],
            frontend_owner_id=message["owner_id"],
            frontend_outbox=True,
        )
    return message
# HASHI-Fingerprint: hfp_36e0621805784fe7ace28b8919e57000
