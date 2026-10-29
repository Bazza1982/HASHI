"""Durable PAO notification publication with independent Connector receipts."""

from __future__ import annotations

from orchestrator import runtime_session
from orchestrator.frontend_connector_registry import endpoint_id_for
from orchestrator.frontend_projection import transport_text_component


async def deliver_notice(
    kernel, *, agent_id, origin, text, publication_id, send_notice
):
    """Retry publication/transport only, never the operation that produced it."""
    message = runtime_session.record_kernel_presentation_notice(
        kernel,
        agent_id=agent_id,
        text=text,
        idempotency_key=publication_id,
        origin=origin,
    )
    if not message:
        return {"sent": False}
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
        return {
            **result,
            "sent": any(
                r["endpoint_id"] == endpoint and r["status"] == "delivered"
                for r in receipts
            ),
        }
    claim = claims[0]
    try:
        delivered = await send_notice(
            kernel,
            source_agent=agent_id,
            operation_id=publication_id,
            notice_kind="final",
            chat_id=int(origin["chat_id"]),
            thread_id=origin.get("thread_id"),
            render_text=lambda *_: text,
        )
    except Exception:
        store.complete_delivery_outbox(
            outbox_id=claim["outbox_id"],
            lease_token=claim["lease_token"],
            status="unknown",
            error_code="notice_transport_unknown",
        )
        raise
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
