"""Telegram Connector adapter usable by runtime and trusted local producers.

Local utilities must publish a standard FC Event and claim its durable endpoint
task before invoking Telegram.  This module gives those utilities the same
Session/Event/outbox boundary used by an Agent Worker; it is not a second send
API and it never accepts a caller-owned delivery status.
"""
from __future__ import annotations

import hashlib
import logging
import mimetypes
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from telegram import Bot

from orchestrator.frontend_connector_registry import endpoint_id_for
from orchestrator.runtime_delivery import dispatch_claimed_telegram_event
from orchestrator.runtime_session import (
    publish_frontend_media_notification,
    publish_frontend_message,
)
from orchestrator.session_store import SessionStore

logger = logging.getLogger("HASHI.FrontendConnector.Telegram")


def _runtime_adapter(
    *,
    root: Path,
    instance_id: str,
    agent_id: str,
    token: str,
    store: SessionStore,
    bot: Any,
    authorized_id: int,
    agent_lifecycle_id: str,
) -> Any:
    runtime_logger = logger
    return SimpleNamespace(
        app=SimpleNamespace(bot=bot),
        canonical_audit=None,
        config=SimpleNamespace(
            active_backend="frontend-connector",
            telegram_token_key=agent_id,
            extra={"agent_lifecycle_id": str(agent_lifecycle_id or "")},
        ),
        error_logger=runtime_logger,
        global_config=SimpleNamespace(
            authorized_id=int(authorized_id),
            instance_id=str(instance_id),
            project_root=Path(root),
        ),
        logger=runtime_logger,
        media_dir=Path(root) / "media",
        name=str(agent_id).strip().casefold(),
        session_store=store,
        telegram_connected=True,
        telegram_logger=runtime_logger,
        token=str(token),
        voice_manager=SimpleNamespace(ffmpeg_cmd="ffmpeg"),
        workspace_dir=Path(root) / "workspaces" / str(agent_id),
        _notify_enabled=False,
    )


async def _claim_and_dispatch(
    runtime: Any,
    publication: dict[str, Any],
    *,
    chat_id: int,
    publication_id: str,
) -> dict[str, Any]:
    endpoint_id = endpoint_id_for(
        "telegram",
        ingress_transport="telegram",
        channel_key=str(chat_id),
    )
    claims = runtime.session_store.claim_delivery_outbox(
        session_id=str(publication["session_id"]),
        owner_id=str(publication["owner_id"]),
        worker_id=f"fc-telegram-local-{runtime.name}-{uuid4().hex}",
        event_id=str(publication["delivery_event_id"]),
        connector_id="telegram",
        endpoint_id=endpoint_id,
        limit=1,
    )
    if claims:
        _elapsed, effects = await dispatch_claimed_telegram_event(
            runtime,
            chat_id=int(chat_id),
            store=runtime.session_store,
            claim=claims[0],
            frontend_owner_id=str(publication["owner_id"]),
            request_id=str(publication_id),
            purpose="explicit-notification",
        )
    else:
        effects = 0
    receipts = runtime.session_store.frontend_delivery_receipts(
        session_id=str(publication["session_id"]),
        owner_id=str(publication["owner_id"]),
        event_id=str(publication["delivery_event_id"]),
    )
    receipt = next(
        (
            item
            for item in receipts
            if str(item.get("endpoint_id") or "") == endpoint_id
        ),
        None,
    )
    state = str((receipt or {}).get("status") or "not_attempted")
    return {
        "event_id": str(publication["delivery_event_id"]),
        "session_id": str(publication["session_id"]),
        "endpoint_id": endpoint_id,
        "state": state,
        "accepted": state in {"accepted", "delivered"},
        "effects": int(effects),
    }


async def publish_explicit_telegram_notification(
    *,
    root: str | Path,
    instance_id: str,
    agent_id: str,
    owner_id: str,
    authorized_id: int,
    chat_id: int,
    token: str,
    publication_id: str,
    text: str = "",
    file_path: str | Path | None = None,
    caption: str = "",
    semantic_role: str = "",
    presentation_role: str = "",
    session_db_path: str | Path | None = None,
    attachment_root: str | Path | None = None,
    agent_lifecycle_id: str = "",
    bot: Any | None = None,
) -> dict[str, Any]:
    """Publish one trusted local notification through standard FC delivery."""

    resolved_root = Path(root).resolve()
    stable_id = str(publication_id or "").strip()
    if not stable_id:
        raise ValueError("publication_id is required")
    if bool(str(text or "").strip()) == bool(file_path):
        raise ValueError("provide exactly one of text or file_path")
    store = SessionStore(
        str(session_db_path or (resolved_root / "state" / "sessions.sqlite3")),
        instance_id=str(instance_id),
        attachment_root=(
            str(attachment_root) if attachment_root is not None else None
        ),
    )

    async def perform(active_bot: Any) -> dict[str, Any]:
        runtime = _runtime_adapter(
            root=resolved_root,
            instance_id=str(instance_id),
            agent_id=str(agent_id),
            token=str(token),
            store=store,
            bot=active_bot,
            authorized_id=int(authorized_id),
            agent_lifecycle_id=str(agent_lifecycle_id),
        )
        if file_path is None:
            publication = publish_frontend_message(
                runtime,
                role="assistant",
                text=str(text),
                source="telegram.explicit-notification",
                publication_id=stable_id,
                surface="telegram",
                channel_key=str(chat_id),
                explicit_owner_id=str(owner_id),
                content_format="plain-text",
                presentation_channel="notification",
            )
        else:
            path = Path(file_path).resolve(strict=True)
            payload = path.read_bytes()
            media_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
            publication = publish_frontend_media_notification(
                runtime,
                filename=path.name,
                media_type=media_type,
                payload=payload,
                sha256=hashlib.sha256(payload).hexdigest(),
                caption=str(caption or ""),
                publication_id=stable_id,
                surface="telegram",
                channel_key=str(chat_id),
                semantic_role=str(semantic_role or ""),
                presentation_role=str(presentation_role or ""),
                explicit_owner_id=str(owner_id),
            )
        return await _claim_and_dispatch(
            runtime,
            publication,
            chat_id=int(chat_id),
            publication_id=stable_id,
        )

    if bot is not None:
        return await perform(bot)
    async with Bot(str(token)) as active_bot:
        return await perform(active_bot)


__all__ = ["publish_explicit_telegram_notification"]
