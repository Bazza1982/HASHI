from __future__ import annotations

import html
from typing import Any

from orchestrator.command_registry import RuntimeCommand
from orchestrator.context_compaction import (
    compact_status_text,
    coordinator_for,
    estimate_effective_context_tokens,
    load_policy,
)
from orchestrator.flexible_backend_registry import HER_V2_ENGINE


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
    reply = getattr(runtime, "_reply_text", None)
    if callable(reply):
        await reply(update, text, parse_mode="HTML")
        return
    if chat_id is not None and hasattr(runtime, "send_long_message"):
        await runtime.send_long_message(
            chat_id,
            text,
            request_id="compact-command",
            purpose="command",
        )
        return
    message = getattr(update, "effective_message", None) or getattr(
        update, "message", None
    )
    if message is not None and hasattr(message, "reply_text"):
        await message.reply_text(text, parse_mode="HTML")


def _outcome_text(outcome: Any) -> str:
    title = {
        "completed": "✅ Context compaction completed",
        "not_needed": "ℹ️ Context compaction not needed",
        "locked": "🔒 Context compaction locked",
        "failed": "⚠️ Context compaction failed safely",
    }.get(str(outcome.status), "ℹ️ Context compaction result")
    lines = [
        f"<b>{title}</b>",
    ]
    if outcome.code:
        lines.append(f"<b>Code</b> · <code>{html.escape(str(outcome.code))}</code>")
    if outcome.changed:
        saved = max(0, int(outcome.before_tokens) - int(outcome.after_tokens))
        lines.extend(
            [
                f"<b>Selected history before</b> · <code>{int(outcome.before_tokens):,} tokens</code>",
                f"<b>Selected history after</b> · <code>{int(outcome.after_tokens):,} tokens</code>",
                f"<b>Reduced by</b> · <code>{saved:,} tokens</code>",
            ]
        )
    elif int(getattr(outcome, "before_tokens", 0) or 0) > 0:
        lines.append(
            f"<b>Current context</b> · <code>{int(outcome.before_tokens):,} tokens</code>"
        )
    if outcome.message:
        lines.extend(["", html.escape(str(outcome.message))])
    if outcome.changed:
        lines.extend(["", "Raw transcript records were retained."])
    return "\n".join(lines)


async def compact_command(runtime: Any, update: Any, context: Any) -> None:
    if not _is_authorized(runtime, update):
        return
    if str(getattr(runtime.config, "active_backend", "")) != HER_V2_ENGINE:
        await _send(
            runtime,
            update,
            "🔒 <b>/compact is available only while HER v2 is active.</b>",
        )
        return

    action = str((getattr(context, "args", None) or [""])[0]).strip().lower()
    from orchestrator import runtime_session
    from orchestrator.bridge_memory import BridgeMemoryStore

    session = runtime_session.current_session_for_update(runtime, update)
    workspace = runtime_session.ensure_store(runtime).session_workspace(
        session["session_id"], int(session["context_generation"])
    )
    coordinator = coordinator_for(
        runtime,
        workspace_dir=workspace,
        memory_store=BridgeMemoryStore(workspace),
    )
    if action in {"status", "show", "info"}:
        await _send(runtime, update, compact_status_text(runtime, coordinator=coordinator))
        return
    if action in {"cancel", "stop"}:
        cancelled = await coordinator.cancel()
        await _send(
            runtime,
            update,
            (
                "🛑 <b>Active context compaction cancelled.</b>\n\n"
                "The active pointer was left unchanged."
                if cancelled
                else "ℹ️ <b>No active context compaction is running.</b>"
            ),
        )
        return
    if action and action not in {"run", "now", "force"}:
        await _send(
            runtime,
            update,
            "Usage: <code>/compact</code> | <code>/compact status</code> | "
            "<code>/compact cancel</code>",
        )
        return
    status = coordinator.status()
    if status["running"]:
        await _send(
            runtime,
            update,
            "⏳ <b>Context compaction is already running.</b>\n\n"
            "Use <code>/compact status</code> or <code>/compact cancel</code>.",
        )
        return

    policy = load_policy(runtime)
    current_tokens = estimate_effective_context_tokens(
        runtime,
        coordinator=coordinator,
        use_last_runtime_measurement=False,
    )
    if current_tokens < policy.manual_min_tokens:
        outcome = await coordinator.compact(
            trigger="manual_command",
            request_ref=f"compact-command:{getattr(update, 'update_id', 'unknown')}",
            force=True,
        )
        await _send(runtime, update, _outcome_text(outcome))
        return

    await _send(
        runtime,
        update,
        "🗜️ <b>Context compaction started</b>\n\n"
        f"Current context · <code>{current_tokens:,} tokens</code>\n"
        f"Manual threshold · <code>{policy.manual_min_tokens:,} tokens</code>\n"
        f"Automatic trigger · <code>&gt; {policy.auto_trigger_tokens:,} tokens</code>",
    )
    outcome = await coordinator.compact(
        trigger="manual_command",
        request_ref=f"compact-command:{getattr(update, 'update_id', 'unknown')}",
        force=True,
    )
    await _send(runtime, update, _outcome_text(outcome))


COMMANDS = [
    RuntimeCommand(
        name="compact",
        description="Compact eligible HER v2 history [status|cancel]",
        callback=compact_command,
    )
]
