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
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any

from orchestrator.frontend_contracts import (
    FRONTEND_EVENT_TYPE,
    FRONTEND_EVENT_VERSION,
    normalize_frontend_event,
)

logger = logging.getLogger(__name__)


class _TransportHTMLToText(HTMLParser):
    """Discard transport markup while retaining readable semantic text."""

    _BREAK_TAGS = frozenset({"br", "p", "div", "li", "tr"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in self._BREAK_TAGS - {"br"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def transport_text_component(text: str, content_format: str) -> dict[str, str]:
    """Convert connector-authored text into the shared text representation."""

    value = str(text or "")
    normalized_format = str(content_format or "plain-text").strip().casefold()
    if normalized_format in {"telegram-html", "html"}:
        parser = _TransportHTMLToText()
        parser.feed(value)
        parser.close()
        lines = [line.rstrip() for line in "".join(parser.parts).splitlines()]
        while lines and not lines[0]:
            lines.pop(0)
        while lines and not lines[-1]:
            lines.pop()
        value = "\n".join(lines)
        block_format = "plain"
    elif normalized_format in {"markdown", "markdownv2"}:
        block_format = "markdown"
    else:
        block_format = "plain"
    return {"type": "text", "text": value, "format": block_format}


def build_frontend_presentation_context(
    *,
    text: str,
    content_format: str,
    presentation_channel: str,
    message_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Attach a canonical display projection without changing local styling."""

    context = dict(message_context or {})
    if isinstance(context.get("frontend_presentation"), Mapping):
        return context
    channel = str(presentation_channel or "command").strip().casefold()
    semantic_kind = channel or "status"
    interface_kind = (
        "notification"
        if semantic_kind == "notification"
        else "message"
        if semantic_kind in {"final", "message", "commentary"}
        else "state"
        if semantic_kind in {"reasoning", "technical", "answer_preview", "progress"}
        else "display"
    )
    context["frontend_presentation"] = {
        "interface_kind": interface_kind,
        "semantic_kind": semantic_kind,
        "presentation_channel": channel,
        "content_blocks": (
            [transport_text_component(text, content_format)] if text else []
        ),
    }
    return context


def project_frontend_event(
    raw: Mapping[str, Any],
    *,
    message_map: Mapping[str, Any] | None = None,
    request_id: str | None = None,
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

    canonical_presentation = None
    if msg_obj and isinstance(msg_obj.get("message_context"), Mapping):
        candidate = msg_obj["message_context"].get("frontend_presentation")
        if isinstance(candidate, Mapping):
            canonical_presentation = candidate

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
        "notification",
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
    interface_kind = None
    if kind == 'run.question.created':
        from orchestrator.frontend_run_questions import question_blocks
        blocks = question_blocks(detail)
        semantic_kind = presentation_channel = 'command'
        interface_kind = 'display'
    if canonical_presentation is not None:
        candidate_kind = str(
            canonical_presentation.get("semantic_kind") or ""
        ).strip().casefold()
        candidate_channel = str(
            canonical_presentation.get("presentation_channel") or ""
        ).strip().casefold()
        candidate_interface = str(
            canonical_presentation.get("interface_kind") or ""
        ).strip().casefold()
        candidate_blocks = canonical_presentation.get("content_blocks")
        if candidate_kind:
            semantic_kind = candidate_kind
        if candidate_channel:
            presentation_channel = candidate_channel
        if candidate_interface:
            interface_kind = candidate_interface
        if isinstance(candidate_blocks, (list, tuple)):
            blocks = [dict(item) for item in candidate_blocks if isinstance(item, Mapping)]
    text_content = str(
        detail.get("text")
        or detail.get("assistant_text")
        or (msg_obj.get("text") if msg_obj else "")
        or summary
        or ""
    )
    if text_content and not blocks:
        inferred_format = (
            "telegram-html"
            if "<b" in text_content or "<code" in text_content
            else "markdown"
        )
        blocks.append(transport_text_component(text_content, inferred_format))

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

    # Media references may be carried by the event itself or by its canonical
    # Message.  Only opaque asset identities cross the FC boundary; managed
    # paths and inline bytes are never projected.
    media_sources: list[Any] = []
    for candidate in (
        detail.get("attachments"),
        detail.get("content"),
        msg_obj.get("content") if msg_obj else None,
    ):
        if isinstance(candidate, (list, tuple)):
            media_sources.extend(candidate)
    seen_media: set[str] = set()
    for att in media_sources:
        if not isinstance(att, Mapping):
            continue
        part_type = str(att.get("type") or "").strip().casefold()
        attachment_id = str(
            att.get("attachment_id")
            or (att.get("asset_id") if part_type == "audio" else "")
            or ""
        ).strip()
        if not attachment_id or attachment_id in seen_media:
            continue
        if part_type and part_type not in {"attachment", "media", "audio"}:
            continue
        seen_media.add(attachment_id)
        blocks.append(
            {
                "type": "media_ref",
                "group_id": str(
                    att.get("group_id") or f"event:{event_id or 'media'}"
                ),
                "attachment_id": attachment_id,
                "role": str(
                    att.get("presentation_role")
                    or att.get("semantic_role")
                    or att.get("kind")
                    or (
                        "voice_message"
                        if part_type == "audio"
                        else "attachment"
                    )
                ),
                "caption": str(att.get("caption") or "") or None,
            }
        )

    if not blocks:
        blocks.append({"type": "text", "text": summary or "Event", "format": "plain"})

    created_at = str(raw.get("created_at") or "") or "2026-09-25T00:00:00Z"
    error_context = detail.get("error_context")
    public_failure = None
    if semantic_kind == 'error':
        from orchestrator.runtime_delivery import build_public_failure
        public_failure = error_context.get('public_failure') if isinstance(error_context, Mapping) else None
        if not isinstance(public_failure, Mapping):
            public_failure = build_public_failure(
                str(error_context.get('backend') or '') if isinstance(error_context, Mapping) else '',
                summary, error_context=error_context,
            )
        blocks = [{'type':'text', 'text':public_failure['text'], 'format':'plain'}]
    typed_error_code = (
        str(error_context.get("error_code") or "").strip()
        if isinstance(error_context, Mapping)
        else ""
    )

    return normalize_frontend_event(
        {
            "type": FRONTEND_EVENT_TYPE,
            "version": FRONTEND_EVENT_VERSION,
            "event_id": event_id,
            "message_id": str(detail.get("message_id") or "") or None,
            "session_id": session_id,
            "sequence": sequence,
            "run_id": run_id,
            "request_id": str(detail.get("request_id") or request_id or "") or None,
            "durability": "durable",
            "epoch": None,
            "ephemeral_sequence": None,
            "audience": "user",
            "visibility": "public",
            "interface_kind": interface_kind,
            "semantic_kind": semantic_kind,
            "presentation_channel": presentation_channel,
            "content_blocks": blocks,
            **({"error_code": typed_error_code} if typed_error_code else {}),
            **({'public_failure':public_failure} if public_failure else {}),
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
    request_id: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Convert a volatile RequestActivity item into an ephemeral FrontendEvent."""
    seq = int(raw_activity.get("sequence", 0))
    channel = str(
        raw_activity.get("presentation_channel")
        or raw_activity.get("kind")
        or "commentary"
    ).strip().casefold()
    semantic_kind, presentation_channel = {
        "commentary": ("commentary", "commentary"),
        "thinking": ("reasoning", "reasoning"),
        "reasoning": ("reasoning", "reasoning"),
        "verbose": ("technical", "technical"),
        "technical": ("technical", "technical"),
        "answer": ("answer_preview", "answer"),
        "answer_preview": ("answer_preview", "answer"),
    }.get(channel, ("commentary", "commentary"))
    text = str(
        (
            raw_activity.get("raw_delta")
            if presentation_channel == "reasoning"
            else None
        )
        or raw_activity.get("text")
        or raw_activity.get("summary")
        or raw_activity.get("detail")
        or ""
    )
    created_at = raw_activity.get("created_at") or raw_activity.get("timestamp")
    if isinstance(created_at, (int, float)) and not isinstance(created_at, bool):
        created_at = datetime.fromtimestamp(
            float(created_at), tz=timezone.utc
        ).isoformat().replace("+00:00", "Z")

    return normalize_frontend_event(
        {
            "type": FRONTEND_EVENT_TYPE,
            "version": FRONTEND_EVENT_VERSION,
            "event_id": f"eph:{epoch}:{seq}",
            "message_id": None,
            "session_id": session_id,
            "sequence": None,
            "run_id": str(raw_activity.get("run_id") or run_id or "") or None,
            "request_id": str(
                raw_activity.get("request_id") or request_id or ""
            ) or None,
            "durability": "ephemeral",
            "epoch": epoch,
            "ephemeral_sequence": seq,
            "audience": "user",
            "visibility": "ephemeral_preview",
            "semantic_kind": semantic_kind,
            "presentation_channel": presentation_channel,
            "content_blocks": [{
                "type": "text",
                "text": text,
                "format": (
                    "markdown" if presentation_channel == "answer" else "plain"
                ),
            }],
            "delivery_intent_ref": None,
            "replaces_event_id": str(raw_activity.get("replaces_id") or "") or None,
            "superseded_by": None,
            "reply_to_event_id": None,
            "created_at": str(created_at or "") or "2026-09-25T00:00:00Z",
        }
    )


def project_ephemeral_feed(
    session_id: str,
    activity: Mapping[str, Any],
    *,
    after_ephemeral_sequence: int = 0,
    epoch_reset: bool = False,
    request_id: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Project only visibility-checked RequestActivity events for one feed.

    RequestActivity is already a sanitised presentation projection.  This
    second gate deliberately excludes internal/control/provider events and the
    final answer replacement, which must come from the durable Session event.
    """

    epoch = int(activity.get("ephemeral_epoch") or 0)
    projected: list[dict[str, Any]] = []
    raw_events = activity.get("events")
    if not isinstance(raw_events, (list, tuple)):
        raw_events = []
    seen_watermark = max(0, int(after_ephemeral_sequence))
    for raw in raw_events:
        if not isinstance(raw, Mapping):
            continue
        sequence = int(raw.get("sequence") or 0)
        seen_watermark = max(seen_watermark, sequence)
        channel = str(raw.get("presentation_channel") or "").strip().casefold()
        if not bool(raw.get("presentation_enabled")):
            continue
        if channel not in {"commentary", "thinking", "reasoning", "verbose", "technical", "answer"}:
            continue
        if channel == "answer" and not bool(raw.get("answer_ephemeral")):
            continue
        event = project_ephemeral_event(
            session_id,
            raw,
            epoch=epoch,
            request_id=request_id,
            run_id=run_id,
        )
        if not event["content_blocks"][0].get("text"):
            continue
        projected.append(event)
    latest = int(activity.get("latest_sequence") or seen_watermark)
    replay_complete = bool(activity.get("replay_complete", False))
    return {
        "ephemeral_events": projected,
        "ephemeral_epoch": epoch,
        "ephemeral_watermark": seen_watermark,
        "ephemeral_latest_sequence": latest,
        "ephemeral_has_more": seen_watermark < latest,
        "ephemeral_replay_complete": replay_complete,
        "ephemeral_gap": not replay_complete,
        "ephemeral_reset": bool(epoch_reset),
    }


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
    request_ids: dict[str, str] = {}
    for raw_event in raw_events:
        detail = raw_event.get("detail")
        if not isinstance(detail, Mapping):
            detail = {}
        message_id = str(detail.get("message_id") or "").strip()
        if not message_id or message_id in msgs:
            continue
        try:
            msgs[message_id] = store.get_message(
                message_id,
                session_id=session_id,
                owner_id=owner_id,
            )
        except Exception:
            continue
    for raw_event in raw_events:
        raw_run_id = str(raw_event.get("run_id") or "").strip()
        if not raw_run_id or raw_run_id in request_ids:
            continue
        try:
            run = store.get_run(raw_run_id, owner_id=owner_id)
            resolved_request_id = str(run.get("request_id") or "").strip()
            if resolved_request_id:
                request_ids[raw_run_id] = resolved_request_id
        except Exception:
            continue
    projected = [
        project_frontend_event(
            event,
            message_map=msgs,
            request_id=request_ids.get(str(event.get("run_id") or "")),
        )
        for event in raw_events
    ]
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
    "build_frontend_presentation_context",
    "poll_frontend_feed",
    "project_ephemeral_event",
    "project_ephemeral_feed",
    "project_frontend_event",
    "render_event_to_html",
    "render_event_to_plain_text",
    "transport_text_component",
]
