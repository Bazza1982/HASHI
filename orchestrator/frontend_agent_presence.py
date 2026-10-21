"""Durable AgentPresence delivery through the standard FC outbox."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any


async def deliver_agentpresence_audio_event(
    runtime: Any,
    *,
    target: Mapping[str, Any],
    owner_id: str,
    source_event_id: str,
) -> dict[str, Any] | None:
    """Claim one AgentPresence media task independently of every other FC."""

    from orchestrator.agent_presence_notify import (
        AgentPresenceConnectorAdapter,
        VOICE_MAX_BYTES,
    )
    from orchestrator.frontend_connector_registry import endpoint_id_for
    from orchestrator.frontend_dispatch import FrontendDispatcher

    store = getattr(runtime, "session_store", None)
    if store is None:
        return None
    endpoint_id = endpoint_id_for(
        "agentpresence",
        ingress_transport="agentpresence",
        channel_key=str(owner_id),
    )
    claims = await asyncio.to_thread(
        store.claim_delivery_outbox,
        session_id=str(target["session_id"]),
        owner_id=str(owner_id),
        worker_id=(
            f"fc-agentpresence-audio-{getattr(runtime, 'name', 'agent')}-"
            f"{str(source_event_id)[:48]}"
        ),
        event_id=str(target["event_id"]),
        connector_id="agentpresence",
        endpoint_id=endpoint_id,
        limit=1,
    )
    if not claims:
        return None

    def resolve_media(
        event: Mapping[str, Any], media_ref: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        message_id = str(event.get("message_id") or "").strip()
        attachment_id = str(media_ref.get("attachment_id") or "").strip()
        if not message_id or not attachment_id:
            raise ValueError("canonical AgentPresence media identity is missing")
        attachment = store.visible_message_attachment(
            str(target["session_id"]),
            owner_id=str(owner_id),
            message_id=message_id,
            attachment_id=attachment_id,
        )
        local_ref = str(attachment.get("local_ref") or "").strip()
        if not local_ref:
            raise ValueError("canonical AgentPresence media has no local reference")
        path = Path(local_ref)
        size_bytes = path.stat().st_size
        return {
            "data": path.read_bytes() if size_bytes <= VOICE_MAX_BYTES else None,
            "mime_type": str(attachment.get("mime_type") or "audio/ogg"),
            "size_bytes": size_bytes,
        }

    adapter = AgentPresenceConnectorAdapter(
        getattr(getattr(runtime, "global_config", None), "bridge_home", None),
        owner_id,
        enabled=True,
        media_resolver=resolve_media,
    )
    dispatcher = FrontendDispatcher(
        store,
        worker_id=f"fc-agentpresence-audio-{str(source_event_id)[:48]}",
        adapters={"agentpresence": adapter},
    )
    return await dispatcher.dispatch_claimed_task(
        claims[0],
        session_id=str(target["session_id"]),
        owner_id=str(owner_id),
    )


def schedule_agentpresence_audio_event(
    runtime: Any,
    *,
    target: Mapping[str, Any],
    owner_id: str,
    source_event_id: str,
) -> asyncio.Task | None:
    """Retain one fail-open local FC task until it reaches a typed outcome."""

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    task = loop.create_task(
        deliver_agentpresence_audio_event(
            runtime,
            target=target,
            owner_id=owner_id,
            source_event_id=source_event_id,
        )
    )
    pending = getattr(runtime, "_optional_frontend_tasks", None)
    if not isinstance(pending, set):
        pending = set()
        setattr(runtime, "_optional_frontend_tasks", pending)
    pending.add(task)

    def finished(completed: asyncio.Task) -> None:
        pending.discard(completed)
        try:
            completed.result()
        except Exception as exc:
            logger = getattr(runtime, "logger", None)
            warning = getattr(logger, "warning", None)
            if callable(warning):
                warning(
                    "AgentPresence audio delivery failed safely for %s (%s)",
                    source_event_id,
                    type(exc).__name__,
                )

    task.add_done_callback(finished)
    return task


__all__ = [
    "deliver_agentpresence_audio_event",
    "schedule_agentpresence_audio_event",
]
