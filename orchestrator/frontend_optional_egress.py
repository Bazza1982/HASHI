"""Connector-neutral fan-out for optional owner-scoped FC subscribers."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import logging
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def configured_owner_subscribers(api: Any, owner_id: str) -> tuple[str, ...]:
    """Freeze enabled registry-declared subscribers for one call lifecycle."""

    from orchestrator.frontend_delivery import configured_owner_mirror_destinations

    state_root = getattr(getattr(api, "global_config", None), "bridge_home", None)
    return tuple(
        destination["surface"]
        for destination in configured_owner_mirror_destinations(
            request_metadata={"owner_id": str(owner_id)},
            state_root=state_root,
        )
    )


async def resolve_call_output(api, owner_id, binding, *, generation):
    """Resolve an opted-in external AV sink without naming it in CallService."""
    subscribers = configured_owner_subscribers(api, owner_id)
    if "agentpresence" not in subscribers:
        return None
    from orchestrator.agent_presence_notify import call_video_output

    config = getattr(api, "global_config", None)
    return await call_video_output(
        getattr(config, "bridge_home", None),
        owner_id,
        binding,
        instance_id=getattr(config, "instance_id", ""),
        generation=generation,
    )


def _event_id(binding: Mapping[str, Any], kind: str, segment: Any) -> str:
    material = "\n".join(
        (
            str(binding.get("call_id") or ""),
            str(kind),
            str(segment if segment is not None else ""),
        )
    )
    return "evt_call_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def _call_event(
    binding: Mapping[str, Any],
    kind: str,
    payload: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    from orchestrator.frontend_contracts import normalize_frontend_event

    normalized_kind = str(kind or "").strip().casefold()
    segment = payload.get("segment")
    event_id = _event_id(binding, normalized_kind, segment)
    resolved_media: dict[str, dict[str, Any]] = {}
    if normalized_kind == "call.speech":
        asset = payload.get("asset")
        if not isinstance(asset, Mapping):
            raise ValueError("call speech event requires an asset")
        encoded = asset.get("content_b64")
        if not isinstance(encoded, str) or not encoded:
            raise ValueError("call speech event has no media payload")
        try:
            audio = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError, TypeError) as exc:
            raise ValueError("call speech event media is invalid") from exc
        attachment_id = (
            "att_call_"
            + hashlib.sha256((event_id + "\nvoice").encode("utf-8")).hexdigest()[:32]
        )
        resolved_media[attachment_id] = {
            "data": audio,
            "mime_type": str(asset.get("media_type") or "audio/wav"),
            "size_bytes": len(audio),
        }
        interface_kind = "notification"
        blocks = [
            {
                "type": "media_ref",
                "group_id": "grp_" + event_id,
                "attachment_id": attachment_id,
                "role": "voice_message",
                "caption": None,
            }
        ]
    elif normalized_kind in {"call.started", "call.ended"}:
        interface_kind = "state"
        items = [
            {
                "key": "phase",
                "value": "started" if normalized_kind == "call.started" else "ended",
            }
        ]
        reason = str(payload.get("reason") or "").strip()
        if reason:
            items.append({"key": "reason", "value": reason})
        blocks = [{"type": "key_value", "items": items}]
    else:
        raise ValueError("unsupported call frontend event")

    event = normalize_frontend_event(
        {
            "type": "hashi.frontend-event",
            "version": 2,
            "event_id": event_id,
            "message_id": None,
            "session_id": str(binding.get("session_id") or ""),
            "sequence": None,
            "run_id": None,
            "request_id": None,
            "durability": "ephemeral",
            "epoch": 1,
            "ephemeral_sequence": max(0, int(segment or 0)),
            "audience": "user",
            "visibility": "ephemeral_preview",
            "interface_kind": interface_kind,
            "semantic_kind": normalized_kind,
            "presentation_channel": "call",
            "content_blocks": blocks,
            "delivery_intent_ref": None,
            "replaces_event_id": None,
            "superseded_by": None,
            "reply_to_event_id": None,
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
    )
    return event, resolved_media


async def dispatch_call_frontend_event(
    api: Any,
    *,
    owner_id: str,
    binding: Mapping[str, Any],
    kind: str,
    subscribers: Sequence[str],
    payload: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Dispatch one typed call event without coupling its producer to an FC."""

    event, resolved_media = _call_event(binding, kind, dict(payload or {}))
    state_root = getattr(getattr(api, "global_config", None), "bridge_home", None)
    receipts: list[dict[str, Any]] = []
    for connector_id in dict.fromkeys(
        str(item).strip().casefold() for item in subscribers
    ):
        if connector_id != "agentpresence":
            continue
        from orchestrator.agent_presence_notify import AgentPresenceConnectorAdapter
        from orchestrator.frontend_connector_registry import endpoint_id_for

        def resolve_media(
            _event: Mapping[str, Any], media_ref: Mapping[str, Any]
        ) -> Mapping[str, Any]:
            attachment_id = str(media_ref.get("attachment_id") or "")
            if attachment_id not in resolved_media:
                raise ValueError("call media reference is unavailable")
            return resolved_media[attachment_id]

        adapter = AgentPresenceConnectorAdapter(
            state_root,
            owner_id,
            enabled=True,
            media_resolver=resolve_media,
        )
        receipts.append(
            await adapter.dispatch(
                event,
                endpoint_id=endpoint_id_for(
                    connector_id,
                    ingress_transport="local",
                    channel_key=str(owner_id),
                ),
            )
        )
    return receipts


def schedule_call_frontend_event(
    api: Any,
    *,
    owner_id: str,
    binding: Mapping[str, Any],
    kind: str,
    subscribers: Sequence[str],
    payload: Mapping[str, Any] | None = None,
) -> asyncio.Task | None:
    """Schedule optional FC effects; failures never affect the call owner."""

    if not subscribers:
        return None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    task = loop.create_task(
        dispatch_call_frontend_event(
            api,
            owner_id=owner_id,
            binding=binding,
            kind=kind,
            subscribers=subscribers,
            payload=payload,
        )
    )
    pending = getattr(api, "_optional_frontend_tasks", None)
    if not isinstance(pending, set):
        pending = set()
        setattr(api, "_optional_frontend_tasks", pending)
    pending.add(task)

    def finished(completed: asyncio.Task) -> None:
        pending.discard(completed)
        try:
            completed.result()
        except Exception as exc:
            logger.warning(
                "Optional frontend event failed safely: kind=%s error_type=%s",
                kind,
                type(exc).__name__,
            )

    task.add_done_callback(finished)
    return task


def schedule_persisted_frontend_media_event(
    runtime: Any,
    *,
    target: Mapping[str, Any],
    owner_id: str,
    source_event_id: str,
) -> tuple[asyncio.Task, ...]:
    """Wake registered optional FC consumers for one durable media Event."""

    from orchestrator.frontend_connector_registry import owner_mirror_connector_ids

    tasks: list[asyncio.Task] = []
    for connector_id in owner_mirror_connector_ids():
        if connector_id == "agentpresence":
            from orchestrator.frontend_agent_presence import (
                schedule_agentpresence_audio_event,
            )

            task = schedule_agentpresence_audio_event(
                runtime,
                target=target,
                owner_id=owner_id,
                source_event_id=source_event_id,
            )
            if task is not None:
                tasks.append(task)
    return tuple(tasks)


__all__ = [
    "configured_owner_subscribers",
    "dispatch_call_frontend_event",
    "schedule_call_frontend_event",
    "schedule_persisted_frontend_media_event",
]
