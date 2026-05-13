from __future__ import annotations

import html
from collections import deque
from datetime import datetime
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from orchestrator.command_registry import RuntimeCallback, RuntimeCommand


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


async def _send(runtime: Any, update: Any, text: str, *, reply_markup: Any = None) -> None:
    chat = getattr(update, "effective_chat", None)
    chat_id = getattr(chat, "id", None)
    send_text = getattr(runtime, "_send_text", None)
    if chat_id is not None and callable(send_text):
        await send_text(chat_id, text, parse_mode="HTML", reply_markup=reply_markup)
        return
    message = getattr(update, "message", None)
    if message is not None and hasattr(message, "reply_text"):
        await message.reply_text(text, parse_mode="HTML", reply_markup=reply_markup)
        return
    if chat_id is not None and hasattr(runtime, "send_long_message"):
        await runtime.send_long_message(
            chat_id,
            text,
            request_id="queue-command",
            purpose="command",
            reply_markup=reply_markup,
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
    return str(getattr(item, "request_id", "") or "")


def _matches(item: Any, request_id: str) -> bool:
    wanted = str(request_id or "").strip()
    if not wanted:
        return False
    rid = _item_id(item)
    return rid == wanted or rid.endswith(wanted)


def _resolve_item_id(runtime: Any, token: str) -> str:
    value = str(token or "").strip()
    if not value:
        return ""
    if value.isdigit():
        index = int(value)
        items = _queue_items(runtime)
        if 1 <= index <= len(items):
            return _item_id(items[index - 1])
    return value


def _item_line(index: int, item: Any) -> str:
    rid = html.escape(_item_id(item) or f"#{index}")
    source = html.escape(str(getattr(item, "source", "?") or "?"))
    summary = html.escape(_short(getattr(item, "summary", "") or getattr(item, "prompt", "")))
    age = html.escape(_age(getattr(item, "created_at", None)))
    silent = " silent" if bool(getattr(item, "silent", False)) else ""
    return f"{index}. <code>{rid}</code> [{source}{silent}] {summary} ({age})"


def _current_line(runtime: Any) -> str:
    if not getattr(runtime, "is_generating", False):
        return "running: 0"
    current = getattr(runtime, "current_request_meta", None) or {}
    rid = html.escape(str(current.get("request_id") or "current"))
    source = html.escape(str(current.get("source") or "?"))
    summary = html.escape(_short(current.get("summary") or ""))
    return f"running: 1\n• <code>{rid}</code> [{source}] {summary}"


def _build_list(runtime: Any) -> str:
    items = _queue_items(runtime)
    lines = [
        f"<b>Queue — {html.escape(str(getattr(runtime, 'name', 'agent')))}</b>",
        _current_line(runtime),
        f"pending: {_queue_size(runtime)}",
    ]
    if items:
        lines.append("")
        lines.append("<b>PENDING</b>")
        for index, item in enumerate(items[:25], 1):
            lines.append(_item_line(index, item))
        if len(items) > 25:
            lines.append(f"<i>... and {len(items) - 25} more</i>")
    else:
        lines.append("")
        lines.append("Queue is empty.")
    lines.append("")
    lines.append("<i>Use the buttons below, or commands: /queue show &lt;id&gt;, /queue cancel &lt;id&gt;, /queue clear, /queue history</i>")
    return "\n".join(lines)


def _queue_keyboard(runtime: Any) -> InlineKeyboardMarkup | None:
    rows: list[list[InlineKeyboardButton]] = []
    for index, item in enumerate(_queue_items(runtime)[:10], 1):
        rid = _item_id(item)
        if not rid:
            continue
        short_id = rid if len(rid) <= 12 else f"...{rid[-9:]}"
        rows.append(
            [
                InlineKeyboardButton(f"👁 {index}. {short_id}", callback_data=f"queue:show:{rid}"),
                InlineKeyboardButton("❌ Cancel", callback_data=f"queue:cancel:{rid}"),
            ]
        )
    nav = [InlineKeyboardButton("🔄 Refresh", callback_data="queue:list")]
    if _queue_items(runtime):
        nav.append(InlineKeyboardButton("🧹 Clear", callback_data="queue:clear:ask"))
    rows.append(nav)
    rows.append([InlineKeyboardButton("📜 History", callback_data="queue:history")])
    return InlineKeyboardMarkup(rows) if rows else None


def _detail_keyboard(item: Any) -> InlineKeyboardMarkup | None:
    rid = _item_id(item)
    if not rid:
        return None
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("❌ Cancel this item", callback_data=f"queue:cancel:{rid}")],
            [InlineKeyboardButton("↩ Queue list", callback_data="queue:list")],
        ]
    )


def _clear_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("⚠️ Clear pending queue", callback_data="queue:clear:yes")],
            [InlineKeyboardButton("↩ Keep queue", callback_data="queue:list")],
        ]
    )


def _find_item(runtime: Any, request_id: str) -> Any | None:
    for item in _queue_items(runtime):
        if _matches(item, request_id):
            return item
    return None


def _format_detail(item: Any) -> str:
    prompt = str(getattr(item, "prompt", "") or "")
    clipped = prompt[:2000]
    lines = [
        "<b>Queue item</b>",
        "",
        f"ID: <code>{html.escape(_item_id(item))}</code>",
        f"Source: {html.escape(str(getattr(item, 'source', '?') or '?'))}",
        f"Summary: {html.escape(str(getattr(item, 'summary', '') or ''))}",
        f"Created: {html.escape(str(getattr(item, 'created_at', '') or ''))}",
        f"Silent: {'yes' if bool(getattr(item, 'silent', False)) else 'no'}",
        f"Retry: {'yes' if bool(getattr(item, 'is_retry', False)) else 'no'}",
        "",
        "Prompt:",
        f"<pre>{html.escape(clipped)}</pre>",
    ]
    if len(prompt) > len(clipped):
        lines.append(f"<i>... ({len(prompt)} chars total)</i>")
    return "\n".join(lines)


def _remove_items(runtime: Any, predicate) -> list[Any]:
    queue = getattr(runtime, "queue", None)
    raw = getattr(queue, "_queue", None)
    if raw is None:
        return []
    kept = deque()
    removed = []
    for item in list(raw):
        if predicate(item):
            removed.append(item)
        else:
            kept.append(item)
    raw.clear()
    raw.extend(kept)
    return removed


async def _cancel(runtime: Any, update: Any, request_id: str) -> None:
    resolved_id = _resolve_item_id(runtime, request_id)
    removed = _remove_items(runtime, lambda item: _matches(item, resolved_id))
    if not removed:
        await _send(runtime, update, f"Item <code>{html.escape(request_id)}</code> not found in pending queue.", reply_markup=_queue_keyboard(runtime))
        return
    await _send(
        runtime,
        update,
        f"Cancelled {len(removed)} pending item(s): <code>{html.escape(_item_id(removed[0]))}</code>\n\n{_build_list(runtime)}",
        reply_markup=_queue_keyboard(runtime),
    )


async def _clear(runtime: Any, update: Any) -> None:
    removed = _remove_items(runtime, lambda _item: True)
    await _send(
        runtime,
        update,
        f"Cleared {len(removed)} pending item(s). Running request was not interrupted.\n\n{_build_list(runtime)}",
        reply_markup=_queue_keyboard(runtime),
    )


def _history(runtime: Any) -> str:
    last_prompt = getattr(runtime, "last_prompt", None)
    last_response = getattr(runtime, "last_response", None)
    lines = [f"<b>Queue history — {html.escape(str(getattr(runtime, 'name', 'agent')))}</b>", ""]
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
    args = [str(arg).strip() for arg in (getattr(context, "args", []) or []) if str(arg).strip()]
    sub = args[0].lower() if args else "list"
    if sub in {"help", "-h", "--help"}:
        await _send(runtime, update, html.escape(USAGE))
        return
    if sub in {"list", "ls", "status"}:
        await _send(runtime, update, _build_list(runtime), reply_markup=_queue_keyboard(runtime))
        return
    if sub == "show" and len(args) >= 2:
        resolved_id = _resolve_item_id(runtime, args[1])
        item = _find_item(runtime, resolved_id)
        await _send(
            runtime,
            update,
            _format_detail(item) if item else f"Item <code>{html.escape(args[1])}</code> not found.",
            reply_markup=_detail_keyboard(item) if item else _queue_keyboard(runtime),
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


async def queue_callback(runtime: Any, update: Any, context: Any) -> None:
    query = getattr(update, "callback_query", None)
    if query is None:
        return
    user_id = getattr(getattr(query, "from_user", None), "id", None)
    checker = getattr(runtime, "_is_authorized_user", None)
    if callable(checker) and not checker(user_id):
        await query.answer("Not authorized.", show_alert=True)
        return
    if not callable(checker):
        authorized_id = getattr(getattr(runtime, "global_config", None), "authorized_id", None)
        if authorized_id is not None and user_id != authorized_id:
            await query.answer("Not authorized.", show_alert=True)
            return
    data = str(getattr(query, "data", "") or "")
    parts = data.split(":", 2)
    if len(parts) < 2 or parts[0] != "queue":
        await query.answer()
        return
    action = parts[1]
    arg = parts[2] if len(parts) > 2 else ""
    await query.answer()

    async def edit(text: str, markup: Any = None) -> None:
        if hasattr(query, "edit_message_text"):
            await query.edit_message_text(text=text, parse_mode="HTML", reply_markup=markup)

    if action == "list":
        await edit(_build_list(runtime), _queue_keyboard(runtime))
        return
    if action == "history":
        await edit(_history(runtime), InlineKeyboardMarkup([[InlineKeyboardButton("↩ Queue list", callback_data="queue:list")]]))
        return
    if action == "show":
        item = _find_item(runtime, arg)
        await edit(_format_detail(item) if item else f"Item <code>{html.escape(arg)}</code> not found.", _detail_keyboard(item) if item else _queue_keyboard(runtime))
        return
    if action == "cancel":
        removed = _remove_items(runtime, lambda item: _matches(item, arg))
        if not removed:
            await edit(f"Item <code>{html.escape(arg)}</code> not found in pending queue.\n\n{_build_list(runtime)}", _queue_keyboard(runtime))
            return
        await edit(
            f"Cancelled pending item: <code>{html.escape(_item_id(removed[0]))}</code>\n\n{_build_list(runtime)}",
            _queue_keyboard(runtime),
        )
        return
    if action == "clear" and arg == "ask":
        await edit(f"Clear all {_queue_size(runtime)} pending item(s)? Running request will not be interrupted.", _clear_confirm_keyboard())
        return
    if action == "clear" and arg == "yes":
        removed = _remove_items(runtime, lambda _item: True)
        await edit(f"Cleared {len(removed)} pending item(s). Running request was not interrupted.\n\n{_build_list(runtime)}", _queue_keyboard(runtime))
        return
    await edit(_build_list(runtime), _queue_keyboard(runtime))


COMMANDS = [
    RuntimeCommand(
        name="queue",
        description="View and manage this agent's pending queue",
        callback=queue_command,
    )
]

CALLBACKS = [
    RuntimeCallback(
        pattern=r"^queue:",
        callback=queue_callback,
    )
]
