from __future__ import annotations

import asyncio
import hashlib
import html as html_lib
import json
import re
from collections.abc import Mapping
from time import monotonic
from typing import Any
from uuid import uuid4

from telegram import constants
from telegram.error import RetryAfter

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
        "gemini-cli": "Gemini CLI",
        "grok-cli": "Grok CLI",
        "her": "HASHI Engine Runtime (HER)",
        "her-v2": "HASHI Engine Runtime (HER)",
    }
    return names.get(str(engine or "").strip().lower(), str(engine or "").strip() or "backend")


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

    error_code = str(context.get("error_code") or "").strip()
    if not error_code:
        code_match = re.match(r"^\[([A-Z][A-Z0-9_]+)\]", exact)
        error_code = code_match.group(1) if code_match else ""
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

    if bool(context.get("side_effects_possible")):
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
    elif context.get("error_retryable") is True:
        lines.append(ui_language.tr("error.action_retryable", locale=selected))

    if exact != raw:
        lines.append("")
        lines.append(ui_language.tr("error.raw", locale=selected, error=raw))
    return "\n".join(lines).strip()


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
        proof = None
        if status == "delivered":
            material = "\n".join(accepted_message_ids) or uuid4().hex
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
            if stage == "completed":
                outbox_outcome["status"] = "completed"
            elif stage in {"failed", "skipped", "blocked"}:
                outbox_outcome["status"] = (
                    "unknown" if stage == "failed" or accepted_count else "failed"
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
                accepted_message_ids.append(str(message_id))
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
            record_frontend_receipt("unknown")
            raise

    if purpose == "error":
        locale = ui_language.preferred_locale(runtime, actor_id=chat_id)
        errors_path = str(getattr(runtime, "session_dir", runtime.workspace_dir) / "errors.log")
        header = "❌ " + ui_language.tr(
            "error.backend_header",
            locale=locale,
            backend=runtime.config.active_backend,
        )
        if request_id:
            header += f" | {request_id}"

        max_excerpt = 2400
        s = format_backend_error_for_user(
            runtime.config.active_backend,
            text,
            locale=locale,
            error_context=error_context,
        )
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
        msg = f"{header}\n\n{excerpt}\n\n" + "\n".join(log_lines)
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
            )
            if not sent:
                return False
        except Exception as e:
            runtime.telegram_logger.warning(
                f"Send failed for request_id={request_id or '<none>'} "
                f"(purpose={purpose}, chunk={chunk_index}, mode=html): {e}. Fallback to raw text."
            )
            if len(chunk_raw) <= tg_max_len:
                sent = await _send_or_skip(
                    chat_id=chat_id,
                    text=chunk_raw,
                    disable_notification=notification_disabled,
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
):
    """Send through Telegram, fencing canonical Run output with its FC outbox."""

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
    outcome: dict[str, Any] = {}
    store = ensure_store(runtime)
    try:
        result = await _send_long_message_transport(
            runtime,
            chat_id=chat_id,
            text=text,
            request_id=request_id,
            purpose=purpose,
            delivery_mode=delivery_mode,
            parse_mode=parse_mode,
            error_context=error_context,
            frontend_event_id=str(claim["event_id"]),
            frontend_session_id=str(claim["session_id"]),
            frontend_owner_id=owner_id(runtime),
            skip_local_capture=True,
            outbox_outcome=outcome,
        )
        duration, chunks = result
        status = str(outcome.get("status") or ("completed" if chunks else "failed"))
        store.complete_delivery_outbox(
            outbox_id=str(claim["outbox_id"]),
            lease_token=str(claim["lease_token"]),
            status=status,
            error_code=None if status == "completed" else f"telegram_{status}",
        )
        return duration, chunks
    except Exception:
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
