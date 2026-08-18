from __future__ import annotations

import asyncio
import html
from datetime import datetime
from typing import Any

from orchestrator import runtime_pending
from orchestrator.command_registry import RuntimeCommand
from orchestrator.command_ui import card_title

USAGE = (
    "Usage: /queue [list|show <request_id>|cancel <request_id>|clear|history]\n"
    "Manages this agent's in-memory pending request queue."
)


def _is_authorized(runtime: Any, update: Any) -> bool:
    checker = getattr(runtime, "_is_authorized_user", None)
    user = getattr(update, "effective_user", None)
    user_id = getattr(user, "id", None)
    if callable(checker):
        return bool(checker(user_id))
    global_config = getattr(runtime, "global_config", None)
    authorized_id = getattr(global_config, "authorized_id", None)
    return authorized_id is None or user_id == authorized_id


async def _send(runtime: Any, update: Any, text: str) -> None:
    chat = getattr(update, "effective_chat", None)
    chat_id = getattr(chat, "id", None)
    send_text = getattr(runtime, "_send_text", None)
    if chat_id is not None and callable(send_text):
        await send_text(chat_id, text, parse_mode="HTML")
        return
    message = getattr(update, "message", None)
    if message is not None and hasattr(message, "reply_text"):
        await message.reply_text(text, parse_mode="HTML")
        return
    if chat_id is not None and hasattr(runtime, "send_long_message"):
        await runtime.send_long_message(
            chat_id,
            text,
            request_id="queue-command",
            purpose="command",
        )


def _queue_items(runtime: Any) -> list[Any]:
    queue = getattr(runtime, "queue", None)
    raw = getattr(queue, "_queue", None)
    if raw is None:
        return []
    return list(raw)


def _queue_size(runtime: Any) -> int:
    queue = getattr(runtime, "queue", None)
    qsize = getattr(queue, "qsize", None)
    if callable(qsize):
        try:
            return int(qsize())
        except Exception:
            return len(_queue_items(runtime))
    return len(_queue_items(runtime))


def _age(iso_ts: str | None) -> str:
    if not iso_ts:
        return "?"
    try:
        delta = datetime.now() - datetime.fromisoformat(str(iso_ts))
    except Exception:
        return "?"
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h"


def _short(value: str | None, limit: int = 120) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _item_id(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("id") or item.get("request_id") or "")
    return str(getattr(item, "request_id", "") or "")


def _matches(item: Any, request_id: str) -> bool:
    wanted = str(request_id or "").strip()
    if not wanted:
        return False
    rid = _item_id(item)
    return rid == wanted or rid.endswith(wanted)


def _item_line(index: int, item: Any) -> str:
    rid = html.escape(_item_id(item) or f"#{index}")
    source = html.escape(str(getattr(item, "source", "?") or "?"))
    summary = html.escape(
        _short(getattr(item, "summary", "") or getattr(item, "prompt", ""))
    )
    age = html.escape(_age(getattr(item, "created_at", None)))
    silent = " silent" if bool(getattr(item, "silent", False)) else ""
    return f"{index}. <code>{rid}</code> [{source}{silent}] {summary} ({age})"


def _current_line(runtime: Any) -> str:
    if not getattr(runtime, "is_generating", False):
        return "<b>Running</b> · <code>0</code>"
    current = getattr(runtime, "current_request_meta", None) or {}
    rid = html.escape(str(current.get("request_id") or "current"))
    source = html.escape(str(current.get("source") or "?"))
    summary = html.escape(_short(current.get("summary") or ""))
    return f"<b>Running</b> · <code>1</code>\n<code>{rid}</code> · {source} · {summary}"


def _delayed_line(index: int, record: dict[str, Any]) -> str:
    delay_id = html.escape(str(record.get("id") or f"delay-{index}"))
    summary = html.escape(
        _short(str(record.get("summary") or record.get("prompt") or ""))
    )
    try:
        due = datetime.fromtimestamp(float(record.get("due_at") or 0)).astimezone()
        due_text = due.strftime("%Y-%m-%d %H:%M:%S %Z")
    except (TypeError, ValueError, OSError):
        due_text = "?"
    return f"{index}. <code>{delay_id}</code> [text] {summary} (due {html.escape(due_text)})"


def _build_list(runtime: Any, delayed_items: list[dict[str, Any]] | None = None) -> str:
    items = _queue_items(runtime)
    if delayed_items is None:
        delayed_items = runtime_pending.delayed_messages_now(runtime)
    else:
        delayed_items = list(delayed_items)
    total = len(items) + len(delayed_items)
    current_line = f"<b>Current</b> · <code>{total}</code> pending"
    if delayed_items:
        current_line += (
            f" · <code>{len(items)}</code> ready · "
            f"<code>{len(delayed_items)}</code> delayed"
        )
    lines = [
        card_title("📥", "Request queue"),
        "",
        current_line,
        _current_line(runtime),
        f"<b>Agent</b> · <code>{html.escape(str(getattr(runtime, 'name', 'agent')))}</code>",
        "<b>Scope</b> · READY FIFO plus persistent FUTURE delays for this agent",
    ]
    if items:
        lines.append("")
        lines.append("<b>PENDING</b>")
        for index, item in enumerate(items[:25], 1):
            lines.append(_item_line(index, item))
        if len(items) > 25:
            lines.append(f"<i>... and {len(items) - 25} more</i>")
    if delayed_items:
        lines.append("")
        lines.append("<b>DELAYED</b>")
        for index, record in enumerate(delayed_items[:25], 1):
            lines.append(_delayed_line(index, record))
        if len(delayed_items) > 25:
            lines.append(f"<i>... and {len(delayed_items) - 25} more delayed</i>")
    if not items and not delayed_items:
        lines.append("")
        lines.append("Queue is empty.")
    lines.append("")
    lines.extend(
        [
            "<b>Use</b>",
            "<code>/queue show &lt;id&gt;</code> · inspect one request",
            "<code>/queue cancel &lt;id&gt;</code> · remove one pending request",
            "<code>/queue clear</code> · remove all pending requests",
            "<code>/queue history</code> · show recent request caches",
        ]
    )
    return "\n".join(lines)


def _find_item(runtime: Any, request_id: str) -> Any | None:
    for item in _queue_items(runtime):
        if _matches(item, request_id):
            return item
    delayed_matches = [
        record
        for record in runtime_pending.delayed_messages_now(runtime)
        if _matches(record, request_id)
    ]
    if len(delayed_matches) == 1:
        return delayed_matches[0]
    return None


def _format_detail(item: Any) -> str:
    if isinstance(item, dict) and str(item.get("id") or "").startswith("delay-"):
        return _format_delayed_detail(item)
    prompt = str(getattr(item, "prompt", "") or "")
    clipped = prompt[:2000]
    lines = [
        card_title("📥", "Queue item"),
        "",
        "<b>Current</b> · <b>PENDING</b>",
        f"<b>ID</b> · <code>{html.escape(_item_id(item))}</code>",
        f"<b>Source</b> · <code>{html.escape(str(getattr(item, 'source', '?') or '?'))}</code>",
        f"<b>Summary</b> · {html.escape(str(getattr(item, 'summary', '') or ''))}",
        f"<b>Created</b> · <code>{html.escape(str(getattr(item, 'created_at', '') or ''))}</code>",
        f"<b>Silent</b> · <code>{'YES' if bool(getattr(item, 'silent', False)) else 'NO'}</code>",
        f"<b>Retry</b> · <code>{'YES' if bool(getattr(item, 'is_retry', False)) else 'NO'}</code>",
        "",
        "<b>PROMPT</b>",
        f"<pre>{html.escape(clipped)}</pre>",
    ]
    if len(prompt) > len(clipped):
        lines.append(f"<i>... ({len(prompt)} chars total)</i>")
    return "\n".join(lines)


def _remove_items(runtime: Any, predicate) -> list[Any]:
    """Compatibility adapter for local queue-button extensions.

    Current built-in handlers use the locked async helpers in runtime_pending.
    This non-awaiting adapter keeps existing private queue UI modules working
    while balancing asyncio.Queue unfinished-task accounting.
    """

    delayed = runtime_pending.delayed_messages_now(runtime)
    selected_delayed = [record for record in delayed if predicate(record)]
    if selected_delayed:
        scheduler = runtime_pending.scheduler_for(runtime)
        cancel_now = getattr(scheduler, "cancel_delayed_messages_now", None)
        if callable(cancel_now):
            selected_ids = {str(record["id"]) for record in selected_delayed}
            selected_delayed = list(
                cancel_now(
                    getattr(runtime, "name", ""),
                    delay_ids=selected_ids,
                )
                or []
            )
        else:
            selected_delayed = []

    queue = getattr(runtime, "queue", None)
    drained: list[Any] = []
    if queue is not None:
        while True:
            try:
                drained.append(queue.get_nowait())
                queue.task_done()
            except asyncio.QueueEmpty:
                break

    removed_ready: list[Any] = []
    kept: list[Any] = []
    for item in drained:
        if predicate(item):
            removed_ready.append(item)
        else:
            kept.append(item)
    for item in kept:
        queue.put_nowait(item)
    return removed_ready + selected_delayed


def _format_delayed_detail(record: dict[str, Any]) -> str:
    prompt = str(record.get("prompt") or "")
    clipped = prompt[:2000]
    try:
        due = (
            datetime.fromtimestamp(float(record.get("due_at") or 0))
            .astimezone()
            .isoformat(timespec="seconds")
        )
    except (TypeError, ValueError, OSError):
        due = "?"
    lines = [
        card_title("⏳", "Delayed queue item"),
        "",
        "<b>Current</b> · <b>DELAYED</b>",
        f"<b>ID</b> · <code>{html.escape(str(record.get('id') or 'unknown'))}</code>",
        "<b>Source</b> · <code>text</code>",
        f"<b>Due</b> · <code>{html.escape(due)}</code>",
        f"<b>Attempts</b> · <code>{int(record.get('attempts') or 0)}</code>",
        "",
        "<b>PROMPT</b>",
        f"<pre>{html.escape(clipped)}</pre>",
    ]
    if len(prompt) > len(clipped):
        lines.append(f"<i>... ({len(prompt)} chars total)</i>")
    return "\n".join(lines)


async def _cancel(runtime: Any, update: Any, request_id: str) -> None:
    removed = await runtime_pending.cancel_pending_by_id(runtime, request_id)
    if not removed.total:
        await _send(
            runtime,
            update,
            f"Item <code>{html.escape(request_id)}</code> not found in pending queue.",
        )
        return
    kind = "delayed" if removed.delayed else "ready"
    await _send(
        runtime,
        update,
        f"Cancelled {removed.total} pending item(s): "
        f"<code>{html.escape(request_id)}</code> ({kind}).",
    )


async def _clear(runtime: Any, update: Any) -> None:
    removed = await runtime_pending.recall_pending(runtime)
    detail = ""
    if removed.delayed:
        detail = f" ({removed.ready} ready, {removed.delayed} delayed)"
    await _send(
        runtime,
        update,
        f"Cleared {removed.total} pending item(s){detail}. Running request was not interrupted.",
    )


def _history(runtime: Any) -> str:
    last_prompt = getattr(runtime, "last_prompt", None)
    last_response = getattr(runtime, "last_response", None)
    lines = [
        card_title("📥", "Queue history"),
        "",
        f"<b>Current</b> · prompt <code>{'CACHED' if last_prompt is not None else 'EMPTY'}</code> · response <code>{'CACHED' if last_response else 'EMPTY'}</code>",
        f"<b>Agent</b> · <code>{html.escape(str(getattr(runtime, 'name', 'agent')))}</code>",
        "",
    ]
    if last_prompt is not None:
        lines.append("<b>Last prompt</b>")
        lines.append(_item_line(1, last_prompt))
    else:
        lines.append("Last prompt: none")
    if last_response:
        rid = html.escape(str(last_response.get("request_id") or "unknown"))
        text = html.escape(_short(last_response.get("text") or ""))
        lines.append("")
        lines.append("<b>Last response</b>")
        lines.append(f"• <code>{rid}</code> {text}")
    else:
        lines.append("Last response: none")
    return "\n".join(lines)


async def queue_command(runtime: Any, update: Any, context: Any) -> None:
    if not _is_authorized(runtime, update):
        return
    args = [
        str(arg).strip()
        for arg in (getattr(context, "args", []) or [])
        if str(arg).strip()
    ]
    sub = args[0].lower() if args else "list"
    if sub in {"help", "-h", "--help"}:
        await _send(runtime, update, html.escape(USAGE))
        return
    if sub in {"list", "ls", "status"}:
        delayed = await runtime_pending.delayed_messages(runtime)
        await _send(runtime, update, _build_list(runtime, delayed))
        return
    if sub == "show" and len(args) >= 2:
        item = _find_item(runtime, args[1])
        if item is not None:
            await _send(runtime, update, _format_detail(item))
            return
        delayed = await runtime_pending.delayed_messages(runtime)
        delayed_matches = [
            record
            for record in delayed
            if str(record.get("id") or "") == args[1]
            or str(record.get("id") or "").endswith(args[1])
        ]
        await _send(
            runtime,
            update,
            _format_delayed_detail(delayed_matches[0])
            if len(delayed_matches) == 1
            else f"Item <code>{html.escape(args[1])}</code> not found.",
        )
        return
    if sub == "cancel" and len(args) >= 2:
        await _cancel(runtime, update, args[1])
        return
    if sub == "clear":
        await _clear(runtime, update)
        return
    if sub == "history":
        await _send(runtime, update, _history(runtime))
        return
    await _send(runtime, update, html.escape(USAGE))


COMMANDS = [
    RuntimeCommand(
        name="queue",
        description="View and manage this agent's pending queue",
        callback=queue_command,
    )
]
