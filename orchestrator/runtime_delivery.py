from __future__ import annotations

import asyncio
import hashlib
import html as html_lib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import uuid4

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, constants
from telegram.error import BadRequest, RetryAfter

from orchestrator import (
    runtime_delivery_order,
    telegram_delivery_failover,
    telegram_notifications,
    ui_language,
)
from orchestrator.runtime_common import _md_to_html


def _retry_after_seconds(exc: Exception) -> int | None:
    if isinstance(exc, RetryAfter):
        return telegram_delivery_failover.retry_after_seconds(exc)
    return None


def _safe_disable_notification(
    runtime: Any,
    *,
    purpose: str,
    delivery_mode: str = "",
    final_chunk: bool = True,
) -> bool:
    """Keep notification policy failures from blocking message delivery."""
    resolver = telegram_notifications.disable_notification
    try:
        return bool(
            resolver(
                runtime,
                purpose=purpose,
                delivery_mode=delivery_mode,
                final_chunk=final_chunk,
            )
        )
    except TypeError as exc:
        # During the first hot reboot across a signature change, a partially
        # refreshed process may briefly retain the legacy one-argument helper.
        try:
            result = bool(resolver(runtime))
        except Exception:
            result = False
        logger = getattr(runtime, "error_logger", None) or getattr(
            runtime, "telegram_logger", None
        )
        if logger is not None:
            logger.warning(
                f"Notification policy compatibility fallback for purpose={purpose}: {exc}"
            )
        return result
    except Exception as exc:
        logger = getattr(runtime, "error_logger", None) or getattr(
            runtime, "telegram_logger", None
        )
        if logger is not None:
            logger.warning(
                f"Notification policy failed for purpose={purpose}; "
                f"sending audibly: {exc}"
            )
        return False


def _extract_backend_error_message(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    try:
        payload = json.loads(raw)
    except Exception:
        payload = None
    if isinstance(payload, dict):
        nested = payload.get("error")
        if isinstance(nested, dict):
            message = str(nested.get("message") or "").strip()
            if message:
                return message
        message = str(payload.get("message") or "").strip()
        if message:
            return message
    return raw


def _backend_runtime_name(engine: str) -> str:
    names = {
        "codex-cli": "Codex",
        "claude-cli": "Claude CLI",
        "gemini-cli": "Gemini CLI",  # Historical failures remain readable.
        "antigravity-cli": "Antigravity CLI",
        "grok-cli": "Grok CLI",
        "her": "HASHI Engine Runtime (HER)",
        "her-v2": "HASHI Engine Runtime (HER)",
    }
    return names.get(str(engine or "").strip().lower(), str(engine or "").strip() or "backend")


def _render_standard_event_for_telegram(
    event: Mapping[str, Any],
) -> tuple[str, str | None]:
    """Render canonical FC content without trusting a caller-owned text copy."""

    blocks = [
        block
        for block in event.get("content_blocks") or ()
        if isinstance(block, Mapping)
        and block.get("type") not in {"media_ref", "action"}
    ]
    if blocks and all(
        isinstance(block, Mapping) and block.get("type") == "text"
        for block in blocks
    ):
        text = "\n".join(str(block.get("text") or "") for block in blocks).strip()
        formats = {
            str(block.get("format") or "plain").strip().casefold()
            for block in blocks
        }
        if formats == {"markdown"}:
            return text, None
        return html_lib.escape(text), "HTML"
    if not blocks:
        return "", "HTML"
    from orchestrator.frontend_projection import render_event_to_plain_text

    projected = dict(event)
    projected["content_blocks"] = blocks
    return html_lib.escape(render_event_to_plain_text(projected)), "HTML"


def telegram_presentation_context(
    *,
    text: str,
    content_format: str,
    presentation_channel: str,
    reply_markup: Any = None,
) -> dict[str, Any]:
    """Convert Telegram styling/widgets into standard content plus a local rendition.

    The transport HTML is retained only as a Connector-local rendition.  Button
    meaning is projected as standard action blocks so another frontend can
    render the same actions without importing Telegram classes.
    """

    from orchestrator.frontend_projection import transport_text_component

    blocks: list[dict[str, Any]] = []
    if text:
        blocks.append(transport_text_component(text, content_format))
    context: dict[str, Any] = {
        "frontend_presentation": {
            "interface_kind": "display",
            "semantic_kind": str(presentation_channel or "command")
            .strip()
            .casefold(),
            "presentation_channel": str(presentation_channel or "command")
            .strip()
            .casefold(),
            "content_blocks": blocks,
        },
        "frontend_connector_renditions": {
            "telegram": {
                "format": str(content_format or "plain-text")
                .strip()
                .casefold(),
                "text": str(text or ""),
            }
        },
    }
    if reply_markup is None:
        return context

    raw_markup = reply_markup.to_dict() if hasattr(reply_markup, "to_dict") else reply_markup
    if isinstance(raw_markup, Mapping) and isinstance(
        raw_markup.get("inline_keyboard"), (list, tuple)
    ):
        from orchestrator.frontend_connector_registry import (
            get_compatibility_adapter,
        )
        from orchestrator.frontend_contracts import normalize_content_blocks

        adapter = get_compatibility_adapter("telegram.inline_keyboard")
        if adapter.get("route") != "standard_fc":
            raise ValueError("Telegram inline keyboard adapter is not standard FC")
        rows = raw_markup["inline_keyboard"]
        if len(rows) > 100:
            raise ValueError("Telegram inline keyboard exceeds FC button limit")
        button_count = 0
        for row_index, row in enumerate(rows):
            if not isinstance(row, (list, tuple)):
                raise ValueError("Telegram inline keyboard row is invalid")
            for column_index, raw_button in enumerate(row):
                button = (
                    raw_button.to_dict()
                    if hasattr(raw_button, "to_dict")
                    else raw_button
                )
                if not isinstance(button, Mapping):
                    raise ValueError("Telegram inline keyboard button is invalid")
                button_count += 1
                if button_count > 100:
                    raise ValueError("Telegram inline keyboard exceeds FC button limit")
                safe_button = json.loads(
                    json.dumps(dict(button), ensure_ascii=False, allow_nan=False)
                )
                label = str(safe_button.get("text") or "").strip()
                if not label:
                    raise ValueError("Telegram inline keyboard button has no label")
                material = json.dumps(
                    {
                        "row": row_index,
                        "column": column_index,
                        "button": safe_button,
                    },
                    sort_keys=True,
                    ensure_ascii=False,
                    allow_nan=False,
                )
                payload: dict[str, Any] = {
                    "connector_id": "telegram",
                    "row": row_index,
                    "column": column_index,
                    "telegram_button": safe_button,
                }
                if safe_button.get("url"):
                    payload["url"] = str(safe_button["url"])
                if safe_button.get("callback_data"):
                    payload["callback_data"] = str(
                        safe_button["callback_data"]
                    )
                blocks.append(
                    {
                        "type": "action",
                        "action_id": "tgact_"
                        + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24],
                        "label": label,
                        "style": "primary",
                        "payload": payload,
                    }
                )
        context["frontend_presentation"]["content_blocks"] = (
            normalize_content_blocks(blocks)
        )
        return context

    class_name = type(reply_markup).__name__.casefold()
    override_key = (
        "reply_prompt" if "forcereply" in class_name else "reply_keyboard"
    )
    from orchestrator.frontend_connector_registry import (
        require_connector_presentation_override,
    )

    require_connector_presentation_override("telegram", override_key)
    context["frontend_connector_renditions"]["telegram"][
        "presentation_override"
    ] = override_key
    return context


def telegram_reply_markup_for_event(
    runtime: Any,
    event: Mapping[str, Any],
) -> InlineKeyboardMarkup | None:
    """Render standard action blocks with Telegram's local button classes."""

    rows: dict[int, list[tuple[int, InlineKeyboardButton]]] = {}
    for block in event.get("content_blocks") or ():
        if not isinstance(block, Mapping) or block.get("type") != "action":
            continue
        payload = block.get("payload")
        payload = dict(payload) if isinstance(payload, Mapping) else {}
        raw_button = payload.get("telegram_button")
        if isinstance(raw_button, Mapping):
            button_data = dict(raw_button)
            button_data["text"] = str(block.get("label") or button_data.get("text") or "Action")
            button = InlineKeyboardButton.de_json(button_data, runtime.app.bot)
        elif payload.get('kind') in {'run_question_answer', 'run_question_text'}:
            from orchestrator.frontend_run_questions import telegram_button
            button = telegram_button(block, ui_language.preferred_locale(runtime))
            payload = {**payload, 'row': len(rows), 'column': 0}
        elif payload.get("url"):
            button = InlineKeyboardButton(
                str(block.get("label") or "Action"),
                url=str(payload["url"]),
            )
        elif payload.get("callback_data"):
            button = InlineKeyboardButton(
                str(block.get("label") or "Action"),
                callback_data=str(payload["callback_data"]),
            )
        else:
            # An action without a Telegram-compatible transport instruction is
            # still visible in text-degrading Connectors, but Telegram must not
            # invent a callback route that FC did not register.
            continue
        row_index = int(payload.get("row") or 0)
        column_index = int(payload.get("column") or 0)
        rows.setdefault(row_index, []).append((column_index, button))
    if not rows:
        return None
    ordered_rows = [
        [button for _column, button in sorted(rows[row], key=lambda item: item[0])]
        for row in sorted(rows)
    ]
    return InlineKeyboardMarkup(ordered_rows)


def require_matching_telegram_rendition(
    event: Mapping[str, Any],
    *,
    text: str,
    content_format: str,
) -> None:
    """Allow local Telegram markup only when its semantic text matches FC."""

    from orchestrator.frontend_projection import transport_text_component

    expected = "\n".join(
        str(block.get("text") or "")
        for block in event.get("content_blocks") or ()
        if isinstance(block, Mapping) and block.get("type") == "text"
    )
    observed = transport_text_component(text, content_format)["text"]
    if observed != expected:
        raise ValueError("Telegram rendition differs from canonical FC text")


def _standard_event_media_refs(
    event: Mapping[str, Any],
) -> tuple[dict[str, Any], ...]:
    """Return the ordered, opaque media references from one FC Event."""

    return tuple(
        dict(block)
        for block in event.get("content_blocks") or ()
        if isinstance(block, Mapping) and block.get("type") == "media_ref"
    )


def _record_standard_telegram_receipt(
    store: Any,
    *,
    session_id: str,
    owner_id: str,
    event_id: str,
    endpoint_id: str,
    status: str,
    message_ids: list[str],
) -> dict[str, Any]:
    """Persist one combined receipt for all text/media effects of an Event."""

    proof = None
    if status == "delivered":
        material = "\n".join(message_ids)
        proof = {
            "type": "telegram-api-accepted",
            "value": "tgmsg_"
            + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32],
        }
    return store.record_frontend_delivery_receipt(
        session_id=str(session_id),
        owner_id=str(owner_id),
        receipt={
            "type": "hashi.delivery-receipt",
            "version": 1,
            "event_id": str(event_id),
            "endpoint_id": str(endpoint_id),
            "status": str(status),
            "proof": proof,
        },
    )


async def _send_standard_event_media_to_telegram(
    runtime: Any,
    *,
    chat_id: int,
    store: Any,
    claim: Mapping[str, Any],
    owner_id: str,
    event: Mapping[str, Any],
    request_id: str,
    purpose: str,
    delivery_mode: str,
    outbox_outcome: dict[str, Any] | None = None,
) -> tuple[list[str], int]:
    """Render canonical media refs with Telegram's local presentation rules."""

    media_refs = _standard_event_media_refs(event)
    if not media_refs:
        return [], 0
    detail = claim.get("detail")
    message_id = (
        str(detail.get("message_id") or "").strip()
        if isinstance(detail, Mapping)
        else ""
    )
    if not message_id:
        raise ValueError("canonical media event has no Message identity")

    accepted_ids: list[str] = []
    accepted_count = 0
    for index, media_ref in enumerate(media_refs, start=1):
        attachment_id = str(media_ref.get("attachment_id") or "").strip()
        if not attachment_id:
            raise ValueError("canonical media reference has no attachment identity")
        attachment = store.visible_message_attachment(
            str(claim["session_id"]),
            owner_id=str(owner_id),
            message_id=message_id,
            attachment_id=attachment_id,
        )
        local_ref = str(attachment.get("local_ref") or "").strip()
        if not local_ref:
            raise ValueError("canonical media attachment has no authorized local reference")
        upload_path = Path(local_ref)
        mime_type = str(attachment.get("mime_type") or "application/octet-stream")
        role = str(media_ref.get("role") or attachment.get("semantic_role") or "")
        caption = str(media_ref.get("caption") or attachment.get("caption") or "")
        # Telegram captions are a local rendering concern and are bounded by
        # the transport. The canonical caption remains intact in the Event.
        telegram_caption = caption[:1024] or None
        kwargs: dict[str, Any] = {
            "chat_id": chat_id,
            "caption": telegram_caption,
            "disable_notification": _safe_disable_notification(
                runtime,
                purpose=purpose,
                delivery_mode=delivery_mode,
                final_chunk=index == len(media_refs),
            ),
            "read_timeout": 30,
            "write_timeout": 30,
            "connect_timeout": 15,
        }
        derivative: Path | None = None
        normalized_role = role.casefold()
        if normalized_role in {"voice", "voice_message"}:
            method = runtime.app.bot.send_voice
            field = "voice"
            if mime_type.casefold() not in {"audio/ogg", "audio/opus"}:
                from orchestrator.voice_synthesizer import convert_audio_to_ogg

                media_dir = Path(
                    getattr(runtime, "media_dir", upload_path.parent)
                )
                media_dir.mkdir(parents=True, exist_ok=True)
                derivative = media_dir / f"fc_voice_{uuid4().hex}.ogg"
                await convert_audio_to_ogg(
                    str(
                        getattr(
                            getattr(runtime, "voice_manager", None),
                            "ffmpeg_cmd",
                            "ffmpeg",
                        )
                    ),
                    upload_path,
                    derivative,
                )
                upload_path = derivative
        elif normalized_role == "document":
            method = runtime.app.bot.send_document
            field = "document"
        elif normalized_role in {"image", "photo"}:
            method = runtime.app.bot.send_photo
            field = "photo"
        elif normalized_role == "video":
            method = runtime.app.bot.send_video
            field = "video"
        elif normalized_role == "audio":
            method = runtime.app.bot.send_audio
            field = "audio"
        elif mime_type.casefold().startswith("image/"):
            method = runtime.app.bot.send_photo
            field = "photo"
        elif mime_type.casefold().startswith("video/"):
            method = runtime.app.bot.send_video
            field = "video"
        elif mime_type.casefold().startswith("audio/"):
            method = runtime.app.bot.send_audio
            field = "audio"
        else:
            method = runtime.app.bot.send_document
            field = "document"
        try:
            with upload_path.open("rb") as handle:
                kwargs[field] = handle
                sent_message = await method(**kwargs)
        finally:
            if derivative is not None:
                derivative.unlink(missing_ok=True)
        accepted_count += 1
        transport_message_id = getattr(sent_message, "message_id", None)
        if transport_message_id is not None:
            accepted_ids.append(str(transport_message_id))
        if outbox_outcome is not None:
            all_ids = list(outbox_outcome.get("accepted_message_ids") or ())
            if transport_message_id is not None:
                all_ids.append(str(transport_message_id))
            outbox_outcome["accepted_message_ids"] = all_ids
            outbox_outcome["accepted_effect_count"] = int(
                outbox_outcome.get("accepted_effect_count") or 0
            ) + 1
        runtime.telegram_logger.info(
            f"Sent canonical Telegram media for request_id={request_id} "
            f"(purpose={purpose}, attachment={attachment_id}, "
            f"index={index}/{len(media_refs)})"
        )
    return accepted_ids, accepted_count


def format_backend_error_for_user(
    engine: str,
    error_text: str,
    *,
    locale: str | None = None,
    error_context: Mapping[str, Any] | None = None,
) -> str:
    selected = ui_language.normalize_locale(locale or ui_language.DEFAULT_LOCALE)
    context = error_context if isinstance(error_context, Mapping) else {}
    raw = str(error_text or "").strip() or ui_language.tr(
        "error.unknown",
        locale=selected,
    )
    exact = _extract_backend_error_message(raw)
    lines: list[str] = [
        ui_language.tr(
            "error.details",
            locale=selected,
            error=exact,
        )
    ]

    from orchestrator.frontend_contracts import normalize_public_error_code
    error_code = normalize_public_error_code(context.get("error_code"))
    if not error_code:
        code_match = re.match(r"^\[([A-Z][A-Z0-9_]+)\]", exact)
        error_code = normalize_public_error_code(code_match.group(1)) if code_match else ""
    if error_code and error_code not in exact:
        lines.append(ui_language.tr("error.code", locale=selected, code=error_code))

    http_status = context.get("http_status")
    if http_status is not None and str(http_status).strip():
        lines.append(
            ui_language.tr(
                "error.http_status",
                locale=selected,
                status=http_status,
            )
        )
    provider_request_id = str(context.get("provider_request_id") or "").strip()
    if provider_request_id:
        lines.append(
            ui_language.tr(
                "error.provider_request_id",
                locale=selected,
                request_id=provider_request_id,
            )
        )

    reconciliation = context.get("effect_reconciliation")
    replay_blocked = bool(context.get('side_effects_possible'))
    if isinstance(reconciliation, Mapping):
        confirmed_reads = max(0, int(reconciliation.get("confirmed_read_count") or 0))
        confirmed = max(0, int(reconciliation.get("confirmed_write_count") or 0))
        unverified = max(0, int(reconciliation.get("unverified_action_count") or 0))
        completed_jobs = max(0, int(reconciliation.get("completed_background_job_count") or 0))
        no_change = max(0, int(reconciliation.get('no_change_count') or 0))
        if confirmed_reads:
            lines.append(
                ui_language.tr(
                    "error.confirmed_reads", locale=selected, count=confirmed_reads
                )
            )
        if confirmed:
            lines.append(
                ui_language.tr("error.confirmed_writes", locale=selected, count=confirmed)
            )
        if completed_jobs:
            lines.append(
                ui_language.tr("error.completed_background_jobs", locale=selected, count=completed_jobs)
            )
        if no_change:
            lines.append(ui_language.tr('error.no_change_actions', locale=selected, count=no_change))
        if unverified:
            lines.append(
                ui_language.tr("error.unverified_actions", locale=selected, count=unverified)
            )
        if reconciliation.get("evidence_limited") is True:
            lines.append(ui_language.tr("error.audit_incomplete", locale=selected))
        replay_blocked = replay_blocked or bool(confirmed or completed_jobs or unverified or reconciliation.get('evidence_limited'))
        if replay_blocked:
            lines.append(ui_language.tr("error.no_blind_retry", locale=selected))
    elif bool(context.get("side_effects_possible")):
        lines.append(
            ui_language.tr("error.warning_partial_execution", locale=selected)
        )

    if "requires a newer version of" in exact:
        match = re.search(r"requires a newer version of ([^.]+)", exact, re.IGNORECASE)
        runtime_name = match.group(1).strip() if match else _backend_runtime_name(engine)
        lines.append(
            ui_language.tr(
                "error.action_newer_runtime",
                locale=selected,
                runtime=runtime_name,
            )
        )
    elif re.search(r"\bmodel\b.*\bnot supported\b", exact, re.IGNORECASE):
        runtime_name = _backend_runtime_name(engine)
        lines.append(
            ui_language.tr(
                "error.action_unsupported_model",
                locale=selected,
                runtime=runtime_name,
            )
        )
    elif error_code == "PROVIDER_BAD_REQUEST":
        lines.append(ui_language.tr("error.action_bad_request", locale=selected))
    elif context.get("error_retryable") is True and not replay_blocked:
        lines.append(ui_language.tr("error.action_retryable", locale=selected))

    if exact != raw:
        lines.append("")
        lines.append(ui_language.tr("error.raw", locale=selected, error=raw))
    return "\n".join(lines).strip()


def build_public_failure(engine: str, error_text: str, *, locale: str | None = None,
                         error_context: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Build the one owner-defined terminal meaning consumed by every frontend."""
    from orchestrator.bootstrap_logging import redact_log_text
    from orchestrator.flexible_backend_registry import public_backend_engine
    from orchestrator.frontend_contracts import normalize_public_failure
    context = dict(error_context or {})
    backend = public_backend_engine(context.get('backend') or engine)
    effects = context.get('effect_reconciliation') or {}
    effects = dict(effects) if isinstance(effects, Mapping) else {}
    blocked = bool(context.get('side_effects_possible') or effects.get('confirmed_write_count')
        or effects.get('unverified_action_count') or effects.get('pending_action_count')
        or effects.get('completed_background_job_count') or effects.get('evidence_limited'))
    action = 'verify_results' if blocked else 'retry' if context.get('error_retryable') is True else 'inspect_failure'
    body = format_backend_error_for_user(backend, redact_log_text(str(error_text or '')),
                                        locale=locale, error_context=context)
    if len(body) > 12000:
        body = body[:12000] + '\n' + ui_language.tr('error.truncated', locale=locale)
    header = '❌ ' + ui_language.tr('error.backend_header', locale=locale, backend=backend)
    return normalize_public_failure({'type':'hashi.public-failure', 'version':1,
        'backend':backend, 'error_code':context.get('error_code'), 'error_retryable':context.get('error_retryable'),
        'side_effects_possible':bool(context.get('side_effects_possible')), 'effects':effects,
        'retry_action':action, 'text':header + '\n\n' + body})


async def _send_long_message_transport(
    runtime: Any,
    *,
    chat_id: int,
    text: str,
    request_id: str | None = None,
    purpose: str = "response",
    delivery_mode: str = "final_delivery",
    parse_mode: str | None = None,
    error_context: Mapping[str, Any] | None = None,
    frontend_event_id: str | None = None,
    frontend_session_id: str | None = None,
    frontend_owner_id: str | None = None,
    skip_local_capture: bool = False,
    outbox_outcome: dict[str, Any] | None = None,
    reply_markup: Any = None,
):
    """Send Markdown or pre-rendered Telegram HTML with safe chunking."""
    from orchestrator.admin_local_testing import capture_local_command_output

    if not skip_local_capture and await capture_local_command_output(
        runtime, chat_id, text, request_id=request_id, purpose=purpose,
        parse_mode=parse_mode,
    ):
        return 0.0, 1
    canonical = getattr(runtime, "canonical_audit", None)
    accepted_message_ids: list[str] = []
    from orchestrator.frontend_connector_registry import endpoint_id_for

    audit_endpoint_id = endpoint_id_for(
        "telegram",
        ingress_transport="telegram",
        channel_key=str(chat_id),
    )

    def record_frontend_receipt(status: str) -> None:
        if not frontend_event_id or not frontend_session_id or not frontend_owner_id:
            return
        if status == "delivered" and not accepted_message_ids:
            # A successful adapter return without a transport message id is an
            # acceptance acknowledgement, not proof of transport delivery.
            status = "accepted"
        proof = None
        if status == "delivered":
            material = "\n".join(accepted_message_ids)
            proof = {
                "type": "telegram-api-accepted",
                "value": "tgmsg_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32],
            }
        from orchestrator.frontend_connector_registry import endpoint_id_for

        endpoint_id = endpoint_id_for(
            "telegram",
            ingress_transport="telegram",
            channel_key=str(chat_id),
        )
        from orchestrator.frontend_contracts import (
            DELIVERY_RECEIPT_TYPE,
            DELIVERY_RECEIPT_VERSION,
        )

        from orchestrator.runtime_session import record_frontend_delivery_receipt

        record_frontend_delivery_receipt(
            runtime,
            session_id=str(frontend_session_id),
            owner_id=str(frontend_owner_id),
            receipt={
                "type": DELIVERY_RECEIPT_TYPE,
                "version": DELIVERY_RECEIPT_VERSION,
                "event_id": str(frontend_event_id),
                "endpoint_id": endpoint_id,
                "status": status,
                "proof": proof,
            },
        )

    def record_delivery(stage: str, **fields: Any) -> None:
        if outbox_outcome is not None:
            accepted_count = len(accepted_message_ids)
            outbox_outcome["accepted_message_ids"] = list(accepted_message_ids)
            outbox_outcome["accepted_effect_count"] = max(
                int(outbox_outcome.get("accepted_effect_count") or 0),
                accepted_count,
            )
            if stage == "completed":
                outbox_outcome["status"] = "completed"
            elif stage == "failed":
                known_rejection = fields.get("error_type") == "BadRequest"
                outbox_outcome["status"] = (
                    "failed" if known_rejection and not accepted_count else "unknown"
                )
            elif stage in {"skipped", "blocked"}:
                outbox_outcome["status"] = (
                    "unknown" if accepted_count else "failed"
                )
        if canonical is None:
            return
        try:
            safe_fields = {
                key: value
                for key, value in fields.items()
                if key
                in {
                    "disposition",
                    "chunks",
                    "retry_after_seconds",
                    "error_type",
                    "http_status",
                }
                and isinstance(value, (str, int, float, bool, type(None)))
            }
            canonical.record(
                "delivery_event",
                {
                    "stage": stage,
                    "connector_id": "telegram",
                    "endpoint_id": audit_endpoint_id,
                    "purpose": purpose,
                    "delivery_mode": delivery_mode,
                    "content_chars": len(text),
                    "content_sha256": hashlib.sha256(
                        text.encode("utf-8")
                    ).hexdigest(),
                    **safe_fields,
                },
                request_id=str(request_id or ""),
                provenance={"connector_id": "telegram"},
            )
        except Exception as exc:
            runtime.error_logger.error(
                "Canonical delivery audit failed for %s: %s",
                request_id or "<none>",
                exc,
            )
            raise

    record_delivery("requested")
    await runtime_delivery_order.wait_for_turn(runtime, request_id)
    if not runtime.telegram_connected:
        runtime.logger.info(
            f"Telegram disconnected — skipping send for {request_id or 'unknown'} "
            f"(purpose={purpose}, text_len={len(text)})"
        )
        record_delivery("skipped", disposition="telegram_disconnected", chunks=0)
        record_frontend_receipt("skipped")
        return 0.0, 0

    send_started = monotonic()
    tg_max_len = 4096
    chunk_count = 0

    if await telegram_delivery_failover.handle_blocked_send(
        runtime,
        chat_id=chat_id,
        request_id=request_id,
        purpose=purpose,
        text=text,
    ):
        runtime.telegram_logger.warning(
            f"Telegram delivery blocked for {request_id or '<none>'} "
            f"(purpose={purpose}, mode={delivery_mode})"
        )
        record_delivery("blocked", disposition="delivery_failover", chunks=0)
        record_frontend_receipt("skipped")
        return 0.0, 0

    async def _send_or_skip(**kwargs) -> bool:
        try:
            sent_message = await runtime.app.bot.send_message(**kwargs)
            message_id = getattr(sent_message, "message_id", None)
            if message_id is not None:
                stable_message_id = str(message_id)
                accepted_message_ids.append(stable_message_id)
                if (
                    frontend_event_id
                    and frontend_session_id
                    and frontend_owner_id
                ):
                    store = getattr(runtime, "session_store", None)
                    recorder = getattr(
                        store, "record_frontend_transport_reference", None
                    )
                    if callable(recorder):
                        try:
                            recorder(
                                session_id=str(frontend_session_id),
                                owner_id=str(frontend_owner_id),
                                connector_id="telegram",
                                endpoint_id=audit_endpoint_id,
                                transport_message_id=stable_message_id,
                                event_id=str(frontend_event_id),
                            )
                        except Exception as exc:
                            runtime.logger.warning(
                                "Telegram reply-reference persistence failed for %s (%s)",
                                request_id or "<none>",
                                type(exc).__name__,
                            )
            if outbox_outcome is not None:
                outbox_outcome["accepted_effect_count"] = int(
                    outbox_outcome.get("accepted_effect_count") or 0
                ) + 1
                outbox_outcome["accepted_message_ids"] = list(
                    accepted_message_ids
                )
            return True
        except Exception as exc:
            retry_after = _retry_after_seconds(exc)
            if retry_after is not None:
                await telegram_delivery_failover.handle_retry_after(
                    runtime,
                    exc=exc,
                    chat_id=chat_id,
                    request_id=request_id,
                    purpose=purpose,
                    text=text,
                )
                runtime.telegram_logger.warning(
                    f"Telegram flood control for request_id={request_id or '<none>'} "
                    f"(purpose={purpose}); skipping send, retry_after_s={retry_after}"
                )
                record_delivery(
                    "blocked",
                    disposition="telegram_retry_after",
                    retry_after_seconds=retry_after,
                )
                return False
            record_delivery(
                "failed",
                disposition="telegram_exception",
                error_type=type(exc).__name__,
                error=str(exc),
            )
            record_frontend_receipt(
                "failed" if isinstance(exc, BadRequest) else "unknown"
            )
            raise

    if purpose == "error":
        locale = ui_language.preferred_locale(runtime, actor_id=chat_id)
        errors_path = str(getattr(runtime, "session_dir", runtime.workspace_dir) / "errors.log")
        max_excerpt = 2400
        public_failure = error_context.get('public_failure') if isinstance(error_context, Mapping) else None
        if not isinstance(public_failure, Mapping):
            public_failure = build_public_failure(runtime.config.active_backend, text,
                                                  locale=locale, error_context=error_context)
        s = public_failure['text']
        if len(s) > max_excerpt:
            head = s[:1200]
            tail = s[-800:]
            truncated = ui_language.tr("error.truncated", locale=locale)
            excerpt = head + f"\n... ({truncated}) ...\n" + tail
        else:
            excerpt = s

        log_lines = [
            ui_language.tr("error.diagnostic_log", locale=locale, path=errors_path)
        ]
        if request_id:
            log_lines.append(
                ui_language.tr(
                    "error.log_lookup",
                    locale=locale,
                    request_id=request_id,
                )
            )
        msg = f"{excerpt}\n\n" + "\n".join(log_lines)
        if len(msg) > tg_max_len:
            truncated = ui_language.tr("error.truncated", locale=locale)
            msg = msg[: tg_max_len - len(truncated) - 9] + f"\n... ({truncated})"

        sent = await _send_or_skip(
            chat_id=chat_id,
            text=msg,
            disable_notification=_safe_disable_notification(
                runtime, purpose="error"
            ),
        )
        if not sent:
            record_frontend_receipt("unknown" if accepted_message_ids else "failed")
            return max(0.0, monotonic() - send_started), 0
        runtime.telegram_logger.info(
            f"Sent Telegram message for request_id={request_id or '<none>'} "
            f"(purpose=error, chunks=1, text_len={len(msg)})"
        )
        record_delivery("completed", disposition="sent", chunks=1)
        record_frontend_receipt("delivered")
        return max(0.0, monotonic() - send_started), 1

    input_is_html = str(parse_mode or "").strip().casefold() == "html"
    if input_is_html:
        rendered_html = text
        # A failed HTML send must fall back to readable text, never visible tags.
        fallback_text = html_lib.unescape(re.sub(r"<[^>]*>", "", text))
        if len(rendered_html) > tg_max_len:
            # Splitting arbitrary HTML can bisect a tag. Oversized cards degrade
            # safely to plain text while normal short cards retain formatting.
            rendered_html = html_lib.escape(fallback_text)
    else:
        rendered_html = _md_to_html(text)
        fallback_text = text

    async def _send_chunk(
        chunk_raw: str, chunk_html: str, chunk_index: int, *, final_chunk: bool
    ):
        buttons = {'reply_markup': reply_markup} if final_chunk and reply_markup is not None else {}
        notification_disabled = _safe_disable_notification(
            runtime,
            purpose=purpose,
            delivery_mode=delivery_mode,
            final_chunk=final_chunk,
        )
        try:
            sent = await _send_or_skip(
                chat_id=chat_id,
                text=chunk_html,
                parse_mode=constants.ParseMode.HTML,
                disable_notification=notification_disabled,
                **buttons,
            )
            if not sent:
                return False
        except Exception as e:
            runtime.telegram_logger.warning(
                f"Send failed for request_id={request_id or '<none>'} "
                f"(purpose={purpose}, chunk={chunk_index}, mode=html): {e}. Fallback to raw text."
            )
            if frontend_event_id and not isinstance(e, BadRequest):
                # A transport exception may already have taken effect.  The
                # managed path must retain unknown instead of replaying the
                # same chunk through the plain-text compatibility fallback.
                raise
            if len(chunk_raw) <= tg_max_len:
                sent = await _send_or_skip(
                    chat_id=chat_id,
                    text=chunk_raw,
                    disable_notification=notification_disabled,
                    **buttons,
                )
                if not sent:
                    return False
            else:
                remain = chunk_raw
                while remain:
                    if len(remain) <= tg_max_len:
                        sent = await _send_or_skip(
                            chat_id=chat_id,
                            text=remain,
                            disable_notification=notification_disabled,
                            **buttons,
                        )
                        return sent
                    split_at = remain.rfind("\n", 0, tg_max_len)
                    if split_at == -1:
                        split_at = tg_max_len
                    sent = await _send_or_skip(
                        chat_id=chat_id,
                        text=remain[:split_at],
                        disable_notification=notification_disabled,
                    )
                    if not sent:
                        return False
                    remain = remain[split_at:].lstrip("\n")
        return True

    if len(rendered_html) <= tg_max_len:
        chunk_count = 1
        if not await _send_chunk(
            fallback_text,
            rendered_html,
            chunk_count,
            final_chunk=True,
        ):
            record_frontend_receipt(
                "unknown" if accepted_message_ids else "failed"
            )
            return max(0.0, monotonic() - send_started), 0
        runtime.telegram_logger.info(
            f"Sent Telegram message for request_id={request_id or '<none>'} "
            f"(purpose={purpose}, chunks={chunk_count}, text_len={len(text)})"
        )
        record_delivery("completed", disposition="sent", chunks=chunk_count)
        record_frontend_receipt("delivered")
        return max(0.0, monotonic() - send_started), chunk_count

    raw_chunks, html_chunks = [], []
    raw_remain, html_remain = fallback_text, rendered_html
    while raw_remain:
        if len(html_remain) <= tg_max_len:
            raw_chunks.append(raw_remain)
            html_chunks.append(html_remain)
            break
        split_at = html_remain.rfind("\n", 0, tg_max_len)
        if split_at == -1:
            split_at = tg_max_len
        raw_split = raw_remain.rfind("\n", 0, split_at + 500)
        if raw_split == -1:
            raw_split = min(split_at, len(raw_remain))

        raw_chunks.append(raw_remain[:raw_split])
        html_chunks.append(html_remain[:split_at])
        raw_remain = raw_remain[raw_split:].lstrip("\n")
        html_remain = html_remain[split_at:].lstrip("\n")

    total_chunks = len(raw_chunks)
    for chunk_count, (rc, hc) in enumerate(zip(raw_chunks, html_chunks), start=1):
        if not await _send_chunk(
            rc, hc, chunk_count, final_chunk=chunk_count == total_chunks
        ):
            record_frontend_receipt(
                "unknown" if accepted_message_ids else "failed"
            )
            return max(0.0, monotonic() - send_started), chunk_count - 1
    runtime.telegram_logger.info(
        f"Sent Telegram message for request_id={request_id or '<none>'} "
        f"(purpose={purpose}, chunks={chunk_count}, text_len={len(text)})"
    )
    record_delivery("completed", disposition="sent", chunks=chunk_count)
    record_frontend_receipt("delivered")
    return max(0.0, monotonic() - send_started), chunk_count


async def dispatch_claimed_telegram_event(
    runtime: Any,
    *,
    chat_id: int,
    store: Any,
    claim: Mapping[str, Any],
    frontend_owner_id: str,
    request_id: str,
    purpose: str,
    delivery_mode: str = "final_delivery",
    error_context: Mapping[str, Any] | None = None,
    include_text: bool = True,
) -> tuple[float, int]:
    """Render one already-claimed standard FC Event through Telegram."""

    from orchestrator.frontend_connector_registry import require_connector_event
    from orchestrator.frontend_dispatch import project_claimed_frontend_event

    standard_event = project_claimed_frontend_event(
        store,
        claim,
        session_id=str(claim["session_id"]),
        owner_id=str(frontend_owner_id),
    )
    require_connector_event("telegram", standard_event)
    canonical_text, canonical_parse_mode = _render_standard_event_for_telegram(
        standard_event
    )
    canonical_markup = telegram_reply_markup_for_event(runtime, standard_event)
    if not include_text:
        canonical_text = ""
    media_refs = _standard_event_media_refs(standard_event)
    outcome: dict[str, Any] = {}
    claim_terminal = False
    send_started = monotonic()
    try:
        if not media_refs:
            if not canonical_text:
                store.complete_delivery_outbox(
                    outbox_id=str(claim["outbox_id"]),
                    lease_token=str(claim["lease_token"]),
                    status="suppressed",
                    error_code="telegram_empty_projection",
                )
                claim_terminal = True
                return 0.0, 0
            duration, chunks = await _send_long_message_transport(
                runtime,
                chat_id=chat_id,
                text=canonical_text,
                request_id=request_id,
                purpose=purpose,
                delivery_mode=delivery_mode,
                parse_mode=canonical_parse_mode,
                error_context=error_context,
                frontend_event_id=str(claim["event_id"]),
                frontend_session_id=str(claim["session_id"]),
                frontend_owner_id=str(frontend_owner_id),
                skip_local_capture=True,
                outbox_outcome=outcome,
                reply_markup=canonical_markup,
            )
            status = str(
                outcome.get("status") or ("completed" if chunks else "failed")
            )
            store.complete_delivery_outbox(
                outbox_id=str(claim["outbox_id"]),
                lease_token=str(claim["lease_token"]),
                status=status,
                error_code=None if status == "completed" else f"telegram_{status}",
            )
            claim_terminal = True
            return duration, chunks

        chunks = 0
        message_ids: list[str] = []
        accepted_effects = 0
        if canonical_text:
            await _send_long_message_transport(
                runtime,
                chat_id=chat_id,
                text=canonical_text,
                request_id=request_id,
                purpose=purpose,
                delivery_mode=delivery_mode,
                parse_mode=canonical_parse_mode,
                error_context=error_context,
                # One combined receipt is persisted only after every ordered
                # media effect has completed under this same outbox lease.
                skip_local_capture=True,
                outbox_outcome=outcome,
            )
            message_ids = list(outcome.get("accepted_message_ids") or ())
            accepted_effects = int(outcome.get("accepted_effect_count") or 0)
            if str(outcome.get("status") or "") != "completed":
                raw_status = str(outcome.get("status") or "failed")
                status = (
                    "unknown"
                    if raw_status == "unknown" or accepted_effects
                    else "failed"
                )
                _record_standard_telegram_receipt(
                    store,
                    session_id=str(claim["session_id"]),
                    owner_id=str(frontend_owner_id),
                    event_id=str(claim["event_id"]),
                    endpoint_id=str(claim["endpoint_id"]),
                    status=status,
                    message_ids=message_ids,
                )
                store.complete_delivery_outbox(
                    outbox_id=str(claim["outbox_id"]),
                    lease_token=str(claim["lease_token"]),
                    status=status,
                    error_code=f"telegram_{status}",
                )
                claim_terminal = True
                return max(0.0, monotonic() - send_started), chunks
            chunks = accepted_effects
        else:
            await runtime_delivery_order.wait_for_turn(runtime, request_id)
            blocked = not runtime.telegram_connected
            error_code = "telegram_disconnected"
            if not blocked:
                blocked = await telegram_delivery_failover.handle_blocked_send(
                    runtime,
                    chat_id=chat_id,
                    request_id=request_id,
                    purpose=purpose,
                    text="",
                )
                error_code = "telegram_delivery_blocked"
            if blocked:
                _record_standard_telegram_receipt(
                    store,
                    session_id=str(claim["session_id"]),
                    owner_id=str(frontend_owner_id),
                    event_id=str(claim["event_id"]),
                    endpoint_id=str(claim["endpoint_id"]),
                    status="failed",
                    message_ids=[],
                )
                store.complete_delivery_outbox(
                    outbox_id=str(claim["outbox_id"]),
                    lease_token=str(claim["lease_token"]),
                    status="failed",
                    error_code=error_code,
                )
                claim_terminal = True
                return 0.0, 0

        try:
            media_message_ids, media_count = (
                await _send_standard_event_media_to_telegram(
                    runtime,
                    chat_id=chat_id,
                    store=store,
                    claim=claim,
                    owner_id=str(frontend_owner_id),
                    event=standard_event,
                    request_id=request_id,
                    purpose=purpose,
                    delivery_mode=delivery_mode,
                    outbox_outcome=outcome,
                )
            )
        except Exception as media_error:
            message_ids = list(outcome.get("accepted_message_ids") or ())
            accepted_effects = int(outcome.get("accepted_effect_count") or 0)
            status = (
                "failed"
                if isinstance(media_error, BadRequest) and not accepted_effects
                else "unknown"
            )
            _record_standard_telegram_receipt(
                store,
                session_id=str(claim["session_id"]),
                owner_id=str(frontend_owner_id),
                event_id=str(claim["event_id"]),
                endpoint_id=str(claim["endpoint_id"]),
                status=status,
                message_ids=message_ids,
            )
            store.complete_delivery_outbox(
                outbox_id=str(claim["outbox_id"]),
                lease_token=str(claim["lease_token"]),
                status=status,
                error_code=f"telegram_media_{status}",
            )
            claim_terminal = True
            raise

        message_ids.extend(media_message_ids)
        accepted_effects += media_count
        chunks += media_count
        receipt_status = (
            "delivered"
            if message_ids and len(message_ids) == accepted_effects
            else "accepted"
        )
        _record_standard_telegram_receipt(
            store,
            session_id=str(claim["session_id"]),
            owner_id=str(frontend_owner_id),
            event_id=str(claim["event_id"]),
            endpoint_id=str(claim["endpoint_id"]),
            status=receipt_status,
            message_ids=message_ids,
        )
        store.complete_delivery_outbox(
            outbox_id=str(claim["outbox_id"]),
            lease_token=str(claim["lease_token"]),
            status="completed",
        )
        claim_terminal = True
        return max(0.0, monotonic() - send_started), chunks
    except Exception:
        if not claim_terminal:
            if media_refs:
                try:
                    _record_standard_telegram_receipt(
                        store,
                        session_id=str(claim["session_id"]),
                        owner_id=str(frontend_owner_id),
                        event_id=str(claim["event_id"]),
                        endpoint_id=str(claim["endpoint_id"]),
                        status="unknown",
                        message_ids=list(outcome.get("accepted_message_ids") or ()),
                    )
                except Exception as receipt_error:
                    runtime.logger.warning(
                        "Frontend receipt persistence failed for %s (%s)",
                        request_id,
                        type(receipt_error).__name__,
                    )
            try:
                store.complete_delivery_outbox(
                    outbox_id=str(claim["outbox_id"]),
                    lease_token=str(claim["lease_token"]),
                    status="unknown",
                    error_code="telegram_transport_exception",
                )
            except Exception as completion_error:
                runtime.logger.warning(
                    "Frontend outbox completion failed for %s (%s)",
                    request_id,
                    type(completion_error).__name__,
                )
        raise


async def send_long_message(
    runtime: Any,
    *,
    chat_id: int,
    text: str,
    request_id: str | None = None,
    purpose: str = "response",
    delivery_mode: str = "final_delivery",
    parse_mode: str | None = None,
    error_context: Mapping[str, Any] | None = None,
    frontend_event_id: str | None = None,
    frontend_session_id: str | None = None,
    frontend_owner_id: str | None = None,
    frontend_outbox: bool = False,
    include_canonical_text: bool = True,
):
    """Send through Telegram, fencing canonical Run output with its FC outbox."""

    if (
        not frontend_outbox
        and request_id
        and not frontend_event_id
        and purpose != "response_continuation"
        and getattr(runtime, "session_store", None) is not None
    ):
        from orchestrator.admin_local_testing import capture_local_command_output

        if await capture_local_command_output(
            runtime,
            chat_id,
            text,
            request_id=request_id,
            purpose=purpose,
            parse_mode=parse_mode,
        ):
            return 0.0, 1
        from orchestrator import runtime_session
        from orchestrator.frontend_connector_registry import endpoint_id_for

        channel = (
            "meter"
            if "meter" in purpose
            else "herv2"
            if "her" in purpose
            else "approval"
            if "approval" in purpose
            else "notification"
            if "notification" in purpose
            else "error"
            if "error" in purpose
            else "status"
        )
        publication = runtime_session.publish_frontend_message(
            runtime,
            role="assistant",
            text=text,
            source="telegram.long-message",
            publication_id=(
                "long_"
                + hashlib.sha256(
                    f"{request_id}\0{purpose}\0{text}".encode("utf-8")
                ).hexdigest()
            ),
            surface="telegram",
            channel_key=str(chat_id),
            content_format=(
                "telegram-html"
                if str(parse_mode or "").strip().casefold() == "html"
                else "plain-text"
            ),
            presentation_channel=channel,
        )
        endpoint_id = endpoint_id_for(
            "telegram",
            ingress_transport="telegram",
            channel_key=str(chat_id),
        )
        claims = runtime.session_store.claim_delivery_outbox(
            session_id=str(publication["session_id"]),
            owner_id=str(publication["owner_id"]),
            worker_id=f"fc-telegram-long-{getattr(runtime, 'name', 'agent')}-{uuid4().hex}",
            event_id=str(publication["delivery_event_id"]),
            connector_id="telegram",
            endpoint_id=endpoint_id,
            limit=1,
        )
        if not claims:
            return 0.0, 0
        claim = claims[0]
        outcome: dict[str, Any] = {}
        try:
            duration, chunks = await _send_long_message_transport(
                runtime,
                chat_id=chat_id,
                text=text,
                request_id=request_id,
                purpose=purpose,
                delivery_mode=delivery_mode,
                parse_mode=parse_mode,
                error_context=error_context,
                frontend_event_id=str(publication["delivery_event_id"]),
                frontend_session_id=str(publication["session_id"]),
                frontend_owner_id=str(publication["owner_id"]),
                skip_local_capture=True,
                outbox_outcome=outcome,
            )
            status = str(
                outcome.get("status") or ("completed" if chunks else "failed")
            )
            runtime.session_store.complete_delivery_outbox(
                outbox_id=str(claim["outbox_id"]),
                lease_token=str(claim["lease_token"]),
                status=status,
                error_code=None if status == "completed" else f"telegram_{status}",
            )
            return duration, chunks
        except Exception:
            runtime.session_store.complete_delivery_outbox(
                outbox_id=str(claim["outbox_id"]),
                lease_token=str(claim["lease_token"]),
                status="unknown",
                error_code="telegram_transport_exception",
            )
            raise

    if not frontend_outbox or not request_id:
        return await _send_long_message_transport(
            runtime,
            chat_id=chat_id,
            text=text,
            request_id=request_id,
            purpose=purpose,
            delivery_mode=delivery_mode,
            parse_mode=parse_mode,
            error_context=error_context,
            frontend_event_id=frontend_event_id,
            frontend_session_id=frontend_session_id,
            frontend_owner_id=frontend_owner_id,
        )

    from orchestrator.admin_local_testing import capture_local_command_output

    if await capture_local_command_output(
        runtime,
        chat_id,
        text,
        request_id=request_id,
        purpose=purpose,
        parse_mode=parse_mode,
    ):
        return 0.0, 1

    from orchestrator.runtime_session import claim_run_delivery_outbox, ensure_store, owner_id

    worker_id = f"fc-telegram-{getattr(runtime, 'name', 'agent')}-{uuid4().hex}"
    claim_result = claim_run_delivery_outbox(
        runtime,
        request_id=request_id,
        surface="telegram",
        channel_key=str(chat_id),
        worker_id=worker_id,
    )
    if claim_result is None:
        # A legacy or non-Session message still uses the compatibility adapter.
        return await _send_long_message_transport(
            runtime,
            chat_id=chat_id,
            text=text,
            request_id=request_id,
            purpose=purpose,
            delivery_mode=delivery_mode,
            parse_mode=parse_mode,
            error_context=error_context,
            frontend_event_id=frontend_event_id,
            frontend_session_id=frontend_session_id,
            frontend_owner_id=frontend_owner_id,
        )
    if claim_result.get("state") != "claimed":
        runtime.logger.info(
            f"Frontend delivery not sent for {request_id} "
            f"(outbox state={claim_result.get('state')})"
        )
        return 0.0, 0

    claim = claim_result["claim"]
    store = ensure_store(runtime)
    return await dispatch_claimed_telegram_event(
        runtime,
        chat_id=chat_id,
        store=store,
        claim=claim,
        frontend_owner_id=owner_id(runtime),
        request_id=request_id,
        purpose=purpose,
        delivery_mode=delivery_mode,
        error_context=error_context,
        include_text=include_canonical_text,
    )


async def typing_loop(runtime: Any, chat_id: int, stop_event: asyncio.Event):
    if not runtime.telegram_connected:
        return
    while not stop_event.is_set():
        try:
            await runtime.app.bot.send_chat_action(chat_id=chat_id, action=constants.ChatAction.TYPING)
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=4.0)
        except asyncio.TimeoutError:
            pass
