from __future__ import annotations

import asyncio
import html as html_lib
import json
import re
from time import monotonic
from typing import Any

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
        return int(getattr(exc, "retry_after", 0) or 0)
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
) -> str:
    selected = ui_language.normalize_locale(locale or ui_language.DEFAULT_LOCALE)
    raw = str(error_text or "").strip() or ui_language.tr(
        "error.unknown",
        locale=selected,
    )
    exact = _extract_backend_error_message(raw)
    lines: list[str] = [
        ui_language.tr(
            "error.exact_failure",
            locale=selected,
            error=exact,
        )
    ]

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

    if exact != raw:
        lines.append("")
        lines.append(ui_language.tr("error.raw", locale=selected, error=raw))
    return "\n".join(lines).strip()


async def send_long_message(
    runtime: Any,
    *,
    chat_id: int,
    text: str,
    request_id: str | None = None,
    purpose: str = "response",
    delivery_mode: str = "final_delivery",
    parse_mode: str | None = None,
):
    """Send Markdown or pre-rendered Telegram HTML with safe chunking."""
    canonical = getattr(runtime, "canonical_audit", None)

    def record_delivery(stage: str, **fields: Any) -> None:
        if canonical is None:
            return
        try:
            canonical.record(
                "delivery_event",
                {
                    "stage": stage,
                    "chat_id": chat_id,
                    "purpose": purpose,
                    "delivery_mode": delivery_mode,
                    "text": text,
                    **fields,
                },
                request_id=str(request_id or ""),
                provenance={"transport": "telegram"},
            )
        except Exception as exc:
            runtime.error_logger.error(
                "Canonical delivery audit failed for %s: %s",
                request_id or "<none>",
                exc,
            )

    record_delivery("requested")
    await runtime_delivery_order.wait_for_turn(runtime, request_id)
    if not runtime.telegram_connected:
        runtime.logger.info(
            f"Telegram disconnected — skipping send for {request_id or 'unknown'} "
            f"(purpose={purpose}, text_len={len(text)})"
        )
        record_delivery("skipped", disposition="telegram_disconnected", chunks=0)
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
        return 0.0, 0

    async def _send_or_skip(**kwargs) -> bool:
        try:
            await runtime.app.bot.send_message(**kwargs)
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
        )
        if len(s) > max_excerpt:
            head = s[:1200]
            tail = s[-800:]
            truncated = ui_language.tr("error.truncated", locale=locale)
            excerpt = head + f"\n... ({truncated}) ...\n" + tail
        else:
            excerpt = s

        msg = (
            f"{header}\n\n"
            f"{excerpt}\n\n"
            f"{ui_language.tr('error.full_log', locale=locale, path=errors_path)}\n"
            f"{ui_language.tr('error.verbose_tip', locale=locale)}"
        )
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
            return max(0.0, monotonic() - send_started), 0
        runtime.telegram_logger.info(
            f"Sent Telegram message for request_id={request_id or '<none>'} "
            f"(purpose=error, chunks=1, text_len={len(msg)})"
        )
        record_delivery("completed", disposition="sent", chunks=1)
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
            return max(0.0, monotonic() - send_started), 0
        runtime.telegram_logger.info(
            f"Sent Telegram message for request_id={request_id or '<none>'} "
            f"(purpose={purpose}, chunks={chunk_count}, text_len={len(text)})"
        )
        record_delivery("completed", disposition="sent", chunks=chunk_count)
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
            return max(0.0, monotonic() - send_started), chunk_count - 1
    runtime.telegram_logger.info(
        f"Sent Telegram message for request_id={request_id or '<none>'} "
        f"(purpose={purpose}, chunks={chunk_count}, text_len={len(text)})"
    )
    record_delivery("completed", disposition="sent", chunks=chunk_count)
    return max(0.0, monotonic() - send_started), chunk_count


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
