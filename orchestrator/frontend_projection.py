"""Versioned event projection and feed service for HASHI Frontend Connectors.

This module projects authoritative SessionStore events and RequestActivity
ephemeral items into unified, versioned FrontendEvent envelopes. Connectors
consume this single projection rather than guessing between JSONL, Session
messages, and memory activities.
"""
from __future__ import annotations

import html
import logging
from collections.abc import Mapping
from typing import Any

from orchestrator.frontend_contracts import (
    FRONTEND_EVENT_TYPE,
    FRONTEND_EVENT_VERSION,
    normalize_frontend_event,
)

logger = logging.getLogger(__name__)


def project_frontend_event(
    raw: Mapping[str, Any],
    *,
    message_map: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Convert an internal SessionStore event row or dict into a standard FrontendEvent."""
    event_id = str(raw.get("event_id") or "")
    session_id = str(raw.get("session_id") or "")
    run_id = str(raw.get("run_id") or "") or None
    sequence = int(raw.get("sequence", 0))
    kind = str(raw.get("kind") or "status").strip().casefold()
    summary = str(raw.get("summary") or "")

    detail = raw.get("detail")
    if not isinstance(detail, Mapping):
        detail_raw = raw.get("detail_json")
        if isinstance(detail_raw, str) and detail_raw.strip():
            import json
            try:
                detail = json.loads(detail_raw)
            except Exception:
                detail = {}
        else:
            detail = {}

    msg_obj = None
    if message_map and detail.get("message_id") and str(detail.get("message_id")) in message_map:
        msg_obj = message_map[str(detail.get("message_id"))]

    chan = str(
        detail.get("presentation_channel")
        or (
            (msg_obj.get("message_context") or {}).get("presentation_channel")
            if msg_obj
            else ""
        )
        or ""
    ).strip().casefold()

    # Map semantic kind and presentation channel
    if chan in {
        "meter",
        "herv2",
        "command",
        "approval",
        "commentary",
        "reasoning",
        "technical",
        "status",
        "final",
        "error",
    }:
        semantic_kind = chan
    elif kind in {"message.created", "message", "run.completed"}:
        semantic_kind = "final"
    elif kind in {"run.failed", "error"}:
        semantic_kind = "error"
    elif kind in {"run.cancelled", "cancelled"}:
        semantic_kind = "status"
    elif kind in {"meter.cost", "meter"}:
        semantic_kind = "meter"
    elif kind in {"herv2", "strategy"}:
        semantic_kind = "herv2"
    elif kind in {"command.completed", "command"}:
        semantic_kind = "command"
    elif kind in {"approval", "run.approval"}:
        semantic_kind = "approval"
    elif kind in {"commentary", "user_commentary"}:
        semantic_kind = "commentary"
    elif kind in {"reasoning", "thinking"}:
        semantic_kind = "reasoning"
    elif kind in {"technical", "tool_progress"}:
        semantic_kind = "technical"
    else:
        semantic_kind = "status"

    presentation_channel = semantic_kind

    # Build typed content blocks
    blocks: list[dict[str, Any]] = []
    text_content = str(
        detail.get("text")
        or detail.get("assistant_text")
        or (msg_obj.get("text") if msg_obj else "")
        or summary
        or ""
    )
    if text_content:
        fmt = "html" if "<b" in text_content or "<code" in text_content else "markdown"
        blocks.append({"type": "text", "text": text_content, "format": fmt})

    # Structured metrics / key-values
    metrics = detail.get("metrics") or detail.get("items")
    if isinstance(metrics, Mapping):
        items = [{"key": str(k), "value": str(v)} for k, v in metrics.items()]
        blocks.append({"type": "key_value", "items": items})
    elif isinstance(metrics, (list, tuple)):
        items = []
        for m in metrics:
            if isinstance(m, Mapping):
                items.append({"key": str(m.get("key") or m.get("name")), "value": str(m.get("value"))})
        if items:
            blocks.append({"type": "key_value", "items": items})

    # Actions / Buttons
    actions = detail.get("actions") or detail.get("buttons")
    if isinstance(actions, (list, tuple)):
        for act in actions:
            if isinstance(act, Mapping):
                blocks.append(
                    {
                        "type": "action",
                        "action_id": str(act.get("action_id") or act.get("id") or "action"),
                        "label": str(act.get("label") or act.get("text") or "Action"),
                        "style": str(act.get("style") or "primary").casefold(),
                        "payload": dict(act.get("payload") or {}),
                    }
                )

    # Attachments
    attachments = detail.get("attachments")
    if isinstance(attachments, (list, tuple)):
        for att in attachments:
            if isinstance(att, Mapping):
                blocks.append(
                    {
                        "type": "media_ref",
                        "group_id": str(att.get("group_id") or "default"),
                        "attachment_id": str(att.get("attachment_id") or ""),
                        "role": str(att.get("semantic_role") or "attachment"),
                        "caption": str(att.get("caption") or "") or None,
                    }
                )

    if not blocks:
        blocks.append({"type": "text", "text": summary or "Event", "format": "plain"})

    created_at = str(raw.get("created_at") or "") or "2026-09-25T00:00:00Z"

    return normalize_frontend_event(
        {
            "type": FRONTEND_EVENT_TYPE,
            "version": FRONTEND_EVENT_VERSION,
            "event_id": event_id,
            "session_id": session_id,
            "sequence": sequence,
            "run_id": run_id,
            "request_id": str(detail.get("request_id") or "") or None,
            "durability": "durable",
            "epoch": None,
            "ephemeral_sequence": None,
            "audience": "user",
            "visibility": "public",
            "semantic_kind": semantic_kind,
            "presentation_channel": presentation_channel,
            "content_blocks": blocks,
            "delivery_intent_ref": None,
            "replaces_event_id": None,
            "superseded_by": None,
            "reply_to_event_id": None,
            "created_at": created_at,
        }
    )


def project_ephemeral_event(
    session_id: str,
    raw_activity: Mapping[str, Any],
    *,
    epoch: int = 1,
) -> dict[str, Any]:
    """Convert a volatile RequestActivity item into an ephemeral FrontendEvent."""
    activity_id = str(raw_activity.get("id") or raw_activity.get("event_id") or "eph-1")
    seq = int(raw_activity.get("sequence", 0))
    kind = str(raw_activity.get("kind") or "commentary").strip().casefold()
    text = str(raw_activity.get("text") or raw_activity.get("summary") or "")

    return normalize_frontend_event(
        {
            "type": FRONTEND_EVENT_TYPE,
            "version": FRONTEND_EVENT_VERSION,
            "event_id": f"eph_{activity_id}",
            "session_id": session_id,
            "sequence": None,
            "run_id": str(raw_activity.get("run_id") or "") or None,
            "request_id": str(raw_activity.get("request_id") or "") or None,
            "durability": "ephemeral",
            "epoch": epoch,
            "ephemeral_sequence": seq,
            "audience": "user",
            "visibility": "ephemeral_preview",
            "semantic_kind": kind if kind in {"commentary", "reasoning", "technical"} else "commentary",
            "presentation_channel": kind if kind in {"commentary", "reasoning", "technical"} else "commentary",
            "content_blocks": [{"type": "text", "text": text, "format": "plain"}],
            "delivery_intent_ref": None,
            "replaces_event_id": str(raw_activity.get("replaces_id") or "") or None,
            "superseded_by": None,
            "reply_to_event_id": None,
            "created_at": str(raw_activity.get("created_at") or "") or "2026-09-25T00:00:00Z",
        }
    )


def render_event_to_plain_text(event: Mapping[str, Any]) -> str:
    """Render a FrontendEvent's content blocks into clean, readable text."""
    blocks = event.get("content_blocks") or []
    lines: list[str] = []
    for block in blocks:
        btype = block.get("type")
        if btype == "text":
            lines.append(str(block.get("text") or ""))
        elif btype in {"key_value", "kv"}:
            for item in block.get("items", []):
                lines.append(f"{item.get('key')}: {item.get('value')}")
        elif btype == "table":
            headers = block.get("headers", [])
            if headers:
                lines.append(" | ".join(headers))
            for row in block.get("rows", []):
                lines.append(" | ".join(row))
        elif btype == "action":
            lines.append(f"[{block.get('label')}]")
        elif btype == "hint":
            lines.append(f"[{block.get('level').upper()}]: {block.get('text')}")
        elif btype == "media_ref":
            caption = f" ({block.get('caption')})" if block.get("caption") else ""
            lines.append(f"[File: {block.get('attachment_id')}{caption}]")
    return "\n".join(lines).strip()


def render_event_to_html(event: Mapping[str, Any]) -> str:
    """Render a FrontendEvent's content blocks into safe HTML suitable for Telegram or Web."""
    blocks = event.get("content_blocks") or []
    parts: list[str] = []
    for block in blocks:
        btype = block.get("type")
        if btype == "text":
            fmt = block.get("format", "plain")
            text = str(block.get("text") or "")
            if fmt == "html":
                parts.append(text)
            else:
                parts.append(html.escape(text))
        elif btype in {"key_value", "kv"}:
            kv_lines = [
                f"<b>{html.escape(str(item.get('key')))}</b>: <code>{html.escape(str(item.get('value')))}</code>"
                for item in block.get("items", [])
            ]
            parts.append("\n".join(kv_lines))
        elif btype == "action":
            parts.append(f"<i>[{html.escape(str(block.get('label')))}]</i>")
        elif btype == "hint":
            parts.append(f"⚠️ <i>{html.escape(str(block.get('text')))}</i>")
        elif btype == "media_ref":
            parts.append(f"📎 <code>{html.escape(str(block.get('attachment_id')))}</code>")
    return "\n".join(parts).strip()


def poll_frontend_feed(
    store: Any,
    session_id: str,
    *,
    owner_id: str | None = None,
    after_durable_sequence: int = 0,
    limit: int = 200,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Retrieve versioned durable events and latest watermark for a Session feed."""
    raw_events = store.events(
        session_id,
        owner_id=owner_id,
        after_sequence=after_durable_sequence,
        limit=limit,
        run_id=run_id,
    )
    msgs: dict[str, Any] = {}
    try:
        for m in store.messages(session_id, owner_id=owner_id):
            if isinstance(m, dict) and m.get("message_id"):
                msgs[str(m["message_id"])] = m
    except Exception:
        pass
    projected = [project_frontend_event(e, message_map=msgs) for e in raw_events]
    watermark = (
        max(e["sequence"] for e in projected)
        if projected
        else after_durable_sequence
    )
    return {
        "session_id": session_id,
        "durable_events": projected,
        "durable_watermark": watermark,
        "has_more": len(raw_events) >= limit,
    }


__all__ = [
    "poll_frontend_feed",
    "project_ephemeral_event",
    "project_frontend_event",
    "render_event_to_html",
    "render_event_to_plain_text",
]
