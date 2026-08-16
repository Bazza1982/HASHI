from __future__ import annotations

import html
from typing import Any

from orchestrator.command_registry import RuntimeCommand
from orchestrator.her_rebuild import HERRebuildError
from orchestrator.her_rebuild_manager import RebuildJobRecord


def _authorized(runtime: Any, update: Any) -> bool:
    user = getattr(update, "effective_user", None)
    user_id = getattr(user, "id", None)
    checker = getattr(runtime, "_is_authorized_user", None)
    if callable(checker):
        return bool(checker(user_id))
    authorized_id = getattr(
        getattr(runtime, "global_config", None), "authorized_id", None
    )
    return authorized_id is None or user_id == authorized_id


async def _send(runtime: Any, update: Any, text: str) -> None:
    if hasattr(runtime, "_reply_text"):
        await runtime._reply_text(update, text, parse_mode="HTML")
        return
    message = getattr(update, "effective_message", None) or getattr(
        update, "message", None
    )
    if message is not None and hasattr(message, "reply_text"):
        await message.reply_text(text, parse_mode="HTML")
        return
    chat = getattr(update, "effective_chat", None)
    if chat is not None and hasattr(runtime, "send_long_message"):
        await runtime.send_long_message(
            chat.id,
            text,
            request_id="her-rebuild-command",
            purpose="command",
        )


def _manager(runtime: Any) -> Any | None:
    kernel = getattr(runtime, "orchestrator", None) or getattr(runtime, "kernel", None)
    return getattr(kernel, "her_rebuild_manager", None) if kernel is not None else None


def _status(record: RebuildJobRecord | None) -> str:
    if record is None:
        return "🛠️ <b>HER rebuild</b>\n\nNo rebuild job has been recorded."
    lines = [
        "🛠️ <b>HER rebuild</b>",
        "",
        f"<b>Job</b> · <code>{html.escape(record.job_id)}</code>",
        f"<b>State</b> · <code>{html.escape(record.state.value)}</code>",
        f"<b>Agent</b> · <code>{html.escape(record.target_agent)}</code>",
        f"<b>Fingerprint</b> · <code>{html.escape(record.source_fingerprint[:16])}</code>",
    ]
    if record.candidate_id:
        lines.append(
            f"<b>Candidate</b> · <code>{html.escape(record.candidate_id)}</code>"
        )
    if record.failure_kind:
        lines.append(
            f"<b>Failure</b> · <code>{html.escape(record.failure_kind.value)}</code>"
        )
    if record.error:
        lines.extend(["", html.escape(record.error)])
    return "\n".join(lines)


async def command_rebuild(runtime: Any, update: Any, context: Any) -> None:
    if not _authorized(runtime, update):
        await _send(
            runtime, update, "⛔ This command is restricted to the authorized owner."
        )
        return
    manager = _manager(runtime)
    if manager is None:
        await _send(
            runtime, update, "❌ HER rebuild manager is unavailable in this kernel."
        )
        return
    args = list(getattr(context, "args", None) or [])
    if args and args[0].lower() in {"status", "show"}:
        await _send(
            runtime, update, _status(manager.get(args[1] if len(args) > 1 else None))
        )
        return
    if args:
        await _send(
            runtime,
            update,
            "Usage: <code>/rebuild</code> or <code>/rebuild status [job-id]</code>",
        )
        return

    user = getattr(update, "effective_user", None)
    chat = getattr(update, "effective_chat", None)
    origin = {
        "channel": "telegram",
        "chat_id": str(getattr(chat, "id", "")),
        "message_id": str(
            getattr(getattr(update, "effective_message", None), "message_id", "")
        ),
    }
    try:
        record, joined = await manager.submit(
            target_agent=str(runtime.name),
            actor_id=str(getattr(user, "id", "")),
            origin=origin,
        )
    except HERRebuildError as exc:
        await _send(
            runtime,
            update,
            "❌ HER rebuild was rejected before starting · "
            f"<code>{html.escape(exc.failure_kind.value)}</code> · {html.escape(str(exc))}",
        )
        return
    verb = "Joined existing build" if joined else "Build accepted"
    await _send(
        runtime,
        update,
        "🛠️ <b>HER rebuild</b>\n\n"
        f"{verb} · <code>{html.escape(record.job_id)}</code>\n"
        f"Fingerprint · <code>{html.escape(record.source_fingerprint[:16])}</code>\n\n"
        "Cargo runs in the background. The current HER remains active until a verified candidate can be safely adopted; a final success/failure reason will be sent automatically.",
    )


COMMANDS = [
    RuntimeCommand(
        name="rebuild",
        description="Rebuild and safely reload HER",
        callback=command_rebuild,
    )
]
