from __future__ import annotations

import asyncio
import json
import re
from time import monotonic
from typing import Any

from telegram import constants
from telegram.error import RetryAfter

from orchestrator import runtime_delivery_order, telegram_delivery_failover
from orchestrator.runtime_common import _md_to_html
from orchestrator.telegram_notifications import disable_notification


def _retry_after_seconds(exc: Exception) -> int | None:
    if isinstance(exc, RetryAfter):
        return int(getattr(exc, "retry_after", 0) or 0)
    return None


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
        "claw-cli": "HASHI Engine Runtime (HER)",
    }
    return names.get(str(engine or "").strip().lower(), str(engine or "").strip() or "backend")


def format_backend_error_for_user(engine: str, error_text: str) -> str:
    raw = str(error_text or "").strip() or "Unknown error"
    exact = _extract_backend_error_message(raw)
    lines: list[str] = [f"Exact backend failure: {exact}"]

    if "requires a newer version of" in exact:
        match = re.search(r"requires a newer version of ([^.]+)", exact, re.IGNORECASE)
        runtime_name = match.group(1).strip() if match else _backend_runtime_name(engine)
        lines.append(
            f"Action: this model is not supported by the installed {runtime_name}. "
            f"Upgrade {runtime_name} or switch this backend to a model your current {runtime_name} supports."
        )
    elif re.search(r"\bmodel\b.*\bnot supported\b", exact, re.IGNORECASE):
        runtime_name = _backend_runtime_name(engine)
        lines.append(
            f"Action: this model is not supported by the current {runtime_name}. "
            f"Upgrade {runtime_name} or switch to a supported model."
        )

    if exact != raw:
        lines.append("")
        lines.append(f"Raw error: {raw}")
    return "\n".join(lines).strip()


async def send_long_message(
    runtime: Any,
    *,
    chat_id: int,
    text: str,
    request_id: str | None = None,
    purpose: str = "response",
    delivery_mode: str = "final_delivery",
):
    """Send a message to Telegram with safe chunking."""
    await runtime_delivery_order.wait_for_turn(runtime, request_id)
    if not runtime.telegram_connected:
        runtime.logger.info(
            f"Telegram disconnected — skipping send for {request_id or 'unknown'} "
            f"(purpose={purpose}, text_len={len(text)})"
        )
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
                return False
            raise

    if purpose == "error":
        errors_path = str(getattr(runtime, "session_dir", runtime.workspace_dir) / "errors.log")
        header = f"❌ Backend error ({runtime.config.active_backend})"
        if request_id:
            header += f" | {request_id}"

        max_excerpt = 2400
        s = format_backend_error_for_user(runtime.config.active_backend, text)
        if len(s) > max_excerpt:
            head = s[:1200]
            tail = s[-800:]
            excerpt = head + "\n... (truncated) ...\n" + tail
        else:
            excerpt = s

        msg = (
            f"{header}\n\n"
            f"{excerpt}\n\n"
            f"Full log (local): {errors_path}\n"
            f"Tip: use /verbose off to reduce progress message noise."
        )
        if len(msg) > tg_max_len:
            msg = msg[: tg_max_len - 20] + "\n... (truncated)"

        sent = await _send_or_skip(
            chat_id=chat_id,
            text=msg,
            disable_notification=disable_notification(runtime),
        )
        if not sent:
            return max(0.0, monotonic() - send_started), 0
        runtime.telegram_logger.info(
            f"Sent Telegram message for request_id={request_id or '<none>'} "
            f"(purpose=error, chunks=1, text_len={len(msg)})"
        )
        return max(0.0, monotonic() - send_started), 1

    html = _md_to_html(text)

    async def _send_chunk(chunk_raw: str, chunk_html: str, chunk_index: int):
        try:
            sent = await _send_or_skip(
                chat_id=chat_id,
                text=chunk_html,
                parse_mode=constants.ParseMode.HTML,
                disable_notification=disable_notification(runtime),
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
                    disable_notification=disable_notification(runtime),
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
                            disable_notification=disable_notification(runtime),
                        )
                        return sent
                    split_at = remain.rfind("\n", 0, tg_max_len)
                    if split_at == -1:
                        split_at = tg_max_len
                    sent = await _send_or_skip(
                        chat_id=chat_id,
                        text=remain[:split_at],
                        disable_notification=disable_notification(runtime),
                    )
                    if not sent:
                        return False
                    remain = remain[split_at:].lstrip("\n")
        return True

    if len(html) <= tg_max_len:
        chunk_count = 1
        if not await _send_chunk(text, html, chunk_count):
            return max(0.0, monotonic() - send_started), 0
        runtime.telegram_logger.info(
            f"Sent Telegram message for request_id={request_id or '<none>'} "
            f"(purpose={purpose}, chunks={chunk_count}, text_len={len(text)})"
        )
        return max(0.0, monotonic() - send_started), chunk_count

    raw_chunks, html_chunks = [], []
    raw_remain, html_remain = text, html
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

    for chunk_count, (rc, hc) in enumerate(zip(raw_chunks, html_chunks), start=1):
        if not await _send_chunk(rc, hc, chunk_count):
            return max(0.0, monotonic() - send_started), chunk_count - 1
    runtime.telegram_logger.info(
        f"Sent Telegram message for request_id={request_id or '<none>'} "
        f"(purpose={purpose}, chunks={chunk_count}, text_len={len(text)})"
    )
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
