from __future__ import annotations

import importlib
from typing import Any


async def cmd_usage(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return

    try:
        token_tracker = importlib.import_module("tools.token_tracker")
    except ImportError:
        await runtime._reply_text(update, "❌ token_tracker not available.")
        return

    format_summary_text = token_tracker.format_summary_text
    get_summary = token_tracker.get_summary

    args = [arg.strip().lower() for arg in (context.args or []) if arg.strip()]
    show_all = bool(args and args[0] == "all")

    if not show_all:
        summary = get_summary(runtime.workspace_dir, session_id=runtime.session_id_dt)
        text = format_summary_text(summary, agent_name=runtime.name)
        await runtime._reply_text(update, text, parse_mode="HTML")
        return

    orchestrator = getattr(runtime, "orchestrator", None)
    if orchestrator is None:
        await runtime._reply_text(update, "❌ Orchestrator unavailable for all-agents view.")
        return

    lines = ["<b>📊 Token Usage — All Agents</b>\n"]
    total_cost = 0.0
    for other_runtime in orchestrator.runtimes:
        summary = get_summary(other_runtime.workspace_dir, session_id=other_runtime.session_id_dt)
        all_time = summary.get("all_time", {})
        if all_time.get("requests", 0) == 0:
            continue
        tokens = all_time["input"] + all_time["output"]
        cost = all_time["cost_usd"]
        total_cost += cost
        session = summary.get("session", {}) or {}
        session_tokens = session.get("input", 0) + session.get("output", 0)
        session_cost = session.get("cost_usd", 0.0)
        lines.append(
            f"<b>{other_runtime.name}</b>  {tokens//1000}K tokens  ${cost:.4f}"
            + (f"  (session {session_tokens//1000}K ${session_cost:.4f})" if session.get("requests") else "")
        )
    lines.append(f"\n<b>Total: ${total_cost:.4f}</b>")
    await runtime._reply_text(update, "\n".join(lines), parse_mode="HTML")
