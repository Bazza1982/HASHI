from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from telegram import constants

from orchestrator.runtime_common import _md_to_html


async def reply_text(runtime: Any, update: Any, text: str, **kwargs):
    last_error = None
    for _ in range(2):
        try:
            return await update.message.reply_text(text, **kwargs)
        except Exception as exc:
            last_error = exc
            runtime.telegram_logger.warning(f"Reply failed: {exc}")
            await asyncio.sleep(0.8)
    raise last_error


async def send_text(runtime: Any, chat_id: int, text: str, **kwargs):
    last_error = None
    for _ in range(2):
        try:
            return await runtime.app.bot.send_message(chat_id=chat_id, text=text, **kwargs)
        except Exception as exc:
            last_error = exc
            runtime.telegram_logger.warning(f"Send failed: {exc}")
            await asyncio.sleep(0.8)
    raise last_error


async def send_long_message(
    runtime: Any,
    *,
    chat_id: int,
    text: str,
    request_id: str | None = None,
    purpose: str = "response",
):
    if not runtime.telegram_connected:
        runtime.logger.info(
            f"Telegram disconnected — skipping send for {request_id or 'unknown'} "
            f"(purpose={purpose}, text_len={len(text)})"
        )
        return 0.0, 0

    send_started = datetime.now()
    tg_max_len = 4096
    chunk_count = 0

    if purpose == "error":
        errors_path = str(getattr(runtime, "session_dir", runtime.workspace_dir) / "errors.log")
        header = f"❌ Backend error ({runtime.config.active_backend})"
        if request_id:
            header += f" | {request_id}"

        max_excerpt = 2400
        s = (text or "").strip()
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

        await runtime.app.bot.send_message(chat_id=chat_id, text=msg)
        runtime.telegram_logger.info(
            f"Sent Telegram message for request_id={request_id or '<none>'} "
            f"(purpose=error, chunks=1, text_len={len(msg)})"
        )
        return (datetime.now() - send_started).total_seconds(), 1

    html = _md_to_html(text)

    async def _send_chunk(chunk_raw: str, chunk_html: str, chunk_index: int):
        try:
            await runtime.app.bot.send_message(
                chat_id=chat_id,
                text=chunk_html,
                parse_mode=constants.ParseMode.HTML,
            )
        except Exception as exc:
            runtime.telegram_logger.warning(
                f"Send failed for request_id={request_id or '<none>'} "
                f"(purpose={purpose}, chunk={chunk_index}, mode=html): {exc}. Fallback to raw text."
            )
            if len(chunk_raw) <= tg_max_len:
                await runtime.app.bot.send_message(chat_id=chat_id, text=chunk_raw)
            else:
                remain = chunk_raw
                while remain:
                    if len(remain) <= tg_max_len:
                        await runtime.app.bot.send_message(chat_id=chat_id, text=remain)
                        break
                    split_at = remain.rfind("\n", 0, tg_max_len)
                    if split_at == -1:
                        split_at = tg_max_len
                    await runtime.app.bot.send_message(chat_id=chat_id, text=remain[:split_at])
                    remain = remain[split_at:].lstrip("\n")

    if len(html) <= tg_max_len:
        chunk_count = 1
        await _send_chunk(text, html, chunk_count)
        runtime.telegram_logger.info(
            f"Sent Telegram message for request_id={request_id or '<none>'} "
            f"(purpose={purpose}, chunks={chunk_count}, text_len={len(text)})"
        )
        return (datetime.now() - send_started).total_seconds(), chunk_count

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
        await _send_chunk(rc, hc, chunk_count)
    runtime.telegram_logger.info(
        f"Sent Telegram message for request_id={request_id or '<none>'} "
        f"(purpose={purpose}, chunks={chunk_count}, text_len={len(text)})"
    )
    return (datetime.now() - send_started).total_seconds(), chunk_count


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
