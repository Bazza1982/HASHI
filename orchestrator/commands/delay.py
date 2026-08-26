from __future__ import annotations

import html
import re
import time
from datetime import datetime
from typing import Any

from orchestrator import runtime_pending, runtime_session
from orchestrator.command_registry import RuntimeCommand
from orchestrator.command_ui import card_title
from orchestrator.scheduler import MAX_DELAY_MINUTES

_DELAY_COMMAND_RE = re.compile(
    r"^/delay(?:@\w+)?(?:\s+(.*))?$",
    re.IGNORECASE | re.DOTALL,
)

USAGE = (
    "Usage:\n"
    "/delay &lt;minutes&gt; &lt;message&gt;\n"
    "/delay list\n"
    "/delay cancel &lt;delay-id&gt;\n\n"
    "Example: /delay 5 send me a message to say hi"
)


def _is_authorized(runtime: Any, update: Any) -> bool:
    checker = getattr(runtime, "_is_authorized_user", None)
    user_id = getattr(getattr(update, "effective_user", None), "id", None)
    if callable(checker):
        return bool(checker(user_id))
    authorized_id = getattr(
        getattr(runtime, "global_config", None), "authorized_id", None
    )
    return authorized_id is None or user_id == authorized_id


async def _send(runtime: Any, update: Any, text: str) -> None:
    chat_id = getattr(getattr(update, "effective_chat", None), "id", None)
    send_text = getattr(runtime, "_send_text", None)
    if chat_id is not None and callable(send_text):
        await send_text(chat_id, text, parse_mode="HTML")
        return
    message = getattr(update, "effective_message", None) or getattr(
        update, "message", None
    )
    if message is not None and hasattr(message, "reply_text"):
        await message.reply_text(text, parse_mode="HTML")
        return
    if chat_id is not None and hasattr(runtime, "send_long_message"):
        await runtime.send_long_message(
            chat_id,
            text,
            request_id="delay-command",
            purpose="command",
        )


def _command_body(update: Any, context: Any) -> str:
    message = getattr(update, "effective_message", None) or getattr(
        update, "message", None
    )
    raw = str(getattr(message, "text", "") or "").strip()
    match = _DELAY_COMMAND_RE.match(raw)
    if match:
        return str(match.group(1) or "").strip()
    return " ".join(str(arg) for arg in (getattr(context, "args", None) or [])).strip()


def parse_delay_request(body: str) -> tuple[int, str]:
    parts = str(body or "").strip().split(None, 1)
    if len(parts) != 2:
        raise ValueError("Provide whole minutes followed by a non-empty message.")
    if not re.fullmatch(r"[1-9]\d*", parts[0]):
        raise ValueError("Minutes must be a positive whole number.")
    minutes = int(parts[0])
    if minutes > MAX_DELAY_MINUTES:
        raise ValueError(f"Minutes cannot exceed {MAX_DELAY_MINUTES} (7 days).")
    message = parts[1].strip()
    if not message:
        raise ValueError("Delayed message cannot be empty.")
    return minutes, message


def _format_remaining(due_at: float, *, now_ts: float | None = None) -> str:
    remaining = max(0, int(due_at - (time.time() if now_ts is None else now_ts)))
    if remaining < 60:
        return f"{remaining}s"
    if remaining < 3600:
        return f"{(remaining + 59) // 60}m"
    hours, remainder = divmod(remaining, 3600)
    minutes = remainder // 60
    return f"{hours}h {minutes}m" if minutes else f"{hours}h"


def _format_due(due_at: float) -> str:
    return (
        datetime.fromtimestamp(float(due_at))
        .astimezone()
        .strftime("%Y-%m-%d %H:%M:%S %Z")
    )


def _excerpt(text: str, limit: int = 140) -> str:
    compact = " ".join(str(text or "").split())
    return compact if len(compact) <= limit else compact[: limit - 3].rstrip() + "..."


def _list_text(runtime: Any, records: list[dict[str, Any]]) -> str:
    lines = [
        card_title("⏳", "Delayed messages"),
        "",
        f"<b>Current</b> · <code>{len(records)}</code> pending",
        f"<b>Agent</b> · <code>{html.escape(str(getattr(runtime, 'name', 'agent')))}</code>",
        "<b>Queue</b> · FUTURE; enters the normal FIFO only when due",
    ]
    if not records:
        lines.extend(["", "No delayed messages."])
    else:
        lines.append("")
        for index, record in enumerate(records, 1):
            delay_id = html.escape(str(record.get("id") or "unknown"))
            due_at = float(record.get("due_at") or 0)
            prompt = html.escape(_excerpt(str(record.get("prompt") or "")))
            lines.extend(
                [
                    f"{index}. <code>{delay_id}</code> · in <code>{_format_remaining(due_at)}</code>",
                    f"   <code>{html.escape(_format_due(due_at))}</code> · {prompt}",
                ]
            )
    lines.extend(
        [
            "",
            "<code>/delay &lt;minutes&gt; &lt;message&gt;</code>",
            "<code>/delay cancel &lt;delay-id&gt;</code>",
            "<code>/recall [count]</code> also recalls delayed messages.",
        ]
    )
    return "\n".join(lines)


def _scheduler(runtime: Any) -> Any | None:
    return runtime_pending.scheduler_for(runtime)


def _session_route(
    runtime: Any, update: Any
) -> tuple[Any, dict[str, Any], bool] | None:
    config = getattr(runtime, "global_config", None)
    has_store_config = bool(
        getattr(runtime, "session_store", None) is not None
        or getattr(config, "bridge_home", None)
        or getattr(config, "project_root", None)
    )
    if not has_store_config or not getattr(runtime, "name", None):
        return None
    try:
        return runtime_session.request_route_for_update(runtime, update)
    except (AttributeError, runtime_session.SessionNotFound):
        return None


def _idempotency_key(update: Any, runtime: Any) -> str | None:
    update_id = getattr(update, "update_id", None)
    if update_id is None:
        message = getattr(update, "effective_message", None) or getattr(
            update, "message", None
        )
        update_id = getattr(message, "message_id", None)
    if update_id is None:
        return None
    chat_id = getattr(getattr(update, "effective_chat", None), "id", "unknown")
    return f"delay:{getattr(runtime, 'name', 'agent')}:{chat_id}:{update_id}"


async def delay_command(runtime: Any, update: Any, context: Any) -> None:
    if not _is_authorized(runtime, update):
        return
    scheduler = _scheduler(runtime)
    if scheduler is None:
        await _send(
            runtime, update, "Delay service is unavailable; nothing was scheduled."
        )
        return

    body = _command_body(update, context)
    normalized = body.strip().lower()
    route = _session_route(runtime, update)
    session_id = str(route[1].get("session_id") or "") if route else None
    if not body or normalized in {"list", "ls", "status"}:
        records = await runtime_pending.delayed_messages(
            runtime, session_id=session_id
        )
        await _send(runtime, update, _list_text(runtime, records))
        return
    if normalized in {"help", "-h", "--help"}:
        await _send(runtime, update, USAGE)
        return

    if normalized.startswith("cancel "):
        requested = body.split(None, 1)[1].strip()
        records = await runtime_pending.delayed_messages(
            runtime, session_id=session_id
        )
        matches = [
            record
            for record in records
            if str(record.get("id") or "") == requested
            or str(record.get("id") or "").endswith(requested)
        ]
        if not matches:
            await _send(
                runtime,
                update,
                f"Delayed message <code>{html.escape(requested)}</code> was not found.",
            )
            return
        if len(matches) > 1:
            await _send(
                runtime,
                update,
                "Delay ID is ambiguous; use the full ID. Nothing was cancelled.",
            )
            return
        async with runtime_pending.pending_lock(runtime):
            removed = await scheduler.cancel_delayed_messages(
                getattr(runtime, "name", ""),
                delay_ids={str(matches[0]["id"])},
            )
        if removed:
            await _send(
                runtime,
                update,
                f"Cancelled delayed message <code>{html.escape(str(matches[0]['id']))}</code>.",
            )
        else:
            await _send(
                runtime,
                update,
                "The delayed message was already dispatched or cancelled.",
            )
        return
    if normalized == "cancel":
        await _send(runtime, update, USAGE)
        return

    try:
        minutes, message = parse_delay_request(body)
    except ValueError as exc:
        await _send(runtime, update, f"{html.escape(str(exc))}\n\n{USAGE}")
        return

    chat_id = getattr(getattr(update, "effective_chat", None), "id", None)
    if chat_id is None:
        await _send(
            runtime, update, "Could not resolve the target chat; nothing was scheduled."
        )
        return
    try:
        enqueue_chat_id = route[0] if route else int(chat_id)
        request_metadata = route[1] if route else None
        deliver_to_telegram = route[2] if route else True
        async with runtime_pending.pending_lock(runtime):
            record = await scheduler.schedule_delayed_message(
                agent_name=getattr(runtime, "name", ""),
                chat_id=enqueue_chat_id,
                prompt=message,
                delay_minutes=minutes,
                idempotency_key=_idempotency_key(update, runtime),
                request_metadata=request_metadata,
                deliver_to_telegram=deliver_to_telegram,
            )
    except (RuntimeError, ValueError) as exc:
        await _send(
            runtime, update, f"Delay was not scheduled: {html.escape(str(exc))}"
        )
        return

    duplicate = " Existing schedule reused." if record.get("deduplicated") else ""
    await _send(
        runtime,
        update,
        "⏳ Delayed message scheduled."
        f"{duplicate}\n"
        f"<b>ID</b> · <code>{html.escape(str(record['id']))}</code>\n"
        f"<b>Due</b> · <code>{html.escape(_format_due(float(record['due_at'])))}</code>\n"
        "It will join the normal FIFO no earlier than the due time; current work and scheduled jobs are unchanged.",
    )


COMMANDS = [
    RuntimeCommand(
        name="delay",
        description="Queue a message for later",
        callback=delay_command,
    )
]
