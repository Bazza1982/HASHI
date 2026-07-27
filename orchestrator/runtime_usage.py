from __future__ import annotations

import datetime
from typing import Any


async def cmd_usage(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    try:
        from tools.token_tracker import format_summary_text, get_summary
    except ImportError:
        await runtime._reply_text(update, "❌ token_tracker not available.")
        return

    args = [arg.strip().lower() for arg in (context.args or []) if arg.strip()]
    show_all = bool(args and args[0] == "all")

    if show_all:
        orchestrator = getattr(runtime, "orchestrator", None)
        if orchestrator is None:
            await runtime._reply_text(update, "❌ Orchestrator unavailable for all-agents view.")
            return
        lines = ["<b>📊 Token Usage — All Agents</b>\n"]
        total_cost = 0.0
        for agent_runtime in orchestrator.runtimes:
            summary = get_summary(
                agent_runtime.workspace_dir,
                session_id=agent_runtime.session_id_dt,
            )
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
                f"<b>{agent_runtime.name}</b>  {tokens // 1000}K tokens  ${cost:.4f}"
                + (
                    f"  (session {session_tokens // 1000}K ${session_cost:.4f})"
                    if session.get("requests")
                    else ""
                )
            )
        lines.append(f"\n<b>Total: ${total_cost:.4f}</b>")
        await runtime._reply_text(update, "\n".join(lines), parse_mode="HTML")
        return

    summary = get_summary(runtime.workspace_dir, session_id=runtime.session_id_dt)
    text = format_summary_text(summary, agent_name=runtime.name)
    await runtime._reply_text(update, text, parse_mode="HTML")


async def cmd_token(runtime: Any, update: Any, context: Any) -> None:
    """Render system-wide token usage grouped by backend type."""
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    try:
        from tools.token_tracker import fmt_tokens, get_summary_extended
    except ImportError:
        await runtime._reply_text(update, "❌ token_tracker not available.")
        return

    orchestrator = getattr(runtime, "orchestrator", None)
    if orchestrator is None:
        await runtime._reply_text(update, "❌ Orchestrator unavailable.")
        return

    groups: dict[str, dict[str, list]] = {}
    totals = {
        period: {
            "input": 0,
            "output": 0,
            "thinking": 0,
            "cost_usd": 0.0,
            "requests": 0,
        }
        for period in ("all_time", "session", "weekly", "monthly")
    }
    total_agents = 0

    for agent_runtime in orchestrator.runtimes:
        summary = get_summary_extended(
            agent_runtime.workspace_dir,
            session_id=agent_runtime.session_id_dt,
        )
        if summary["all_time"]["requests"] == 0:
            continue
        total_agents += 1
        manager = getattr(agent_runtime, "backend_manager", None)
        backend = (
            getattr(manager, "active_backend", None)
            or getattr(getattr(agent_runtime, "config", None), "active_backend", None)
            or "unknown"
        )
        try:
            model = agent_runtime.get_current_model() or "unknown"
        except Exception:
            model = "unknown"
        if backend.endswith("-cli"):
            category = "🖥️ CLI Backends"
        elif backend.endswith("-api"):
            category = "🌐 API Backends"
        else:
            category = "❓ Other"
        groups.setdefault(category, {}).setdefault(backend, []).append(
            {"name": agent_runtime.name, "model": model, "summary": summary}
        )
        for period in totals:
            source = summary.get(period) or {}
            for key in totals[period]:
                totals[period][key] += source.get(key, 0)

    if total_agents == 0:
        await runtime._reply_text(update, "📊 No token usage recorded yet.")
        return

    lines = ["<b>📊 Token Summary — All Agents</b>"]
    category_order = ["🖥️ CLI Backends", "🌐 API Backends", "❓ Other"]
    for category in category_order:
        if category not in groups:
            continue
        lines.append(f"\n<b>{category}</b>")
        for backend, agents in sorted(groups[category].items()):
            lines.append(f"  <b>{backend}</b>")
            backend_total = {
                "input": 0,
                "output": 0,
                "thinking": 0,
                "cost_usd": 0.0,
                "requests": 0,
            }
            for agent in agents:
                all_time = agent["summary"]["all_time"]
                session = agent["summary"].get("session") or {}
                thinking = (
                    f"  💭{fmt_tokens(all_time['thinking'])}"
                    if all_time["thinking"] > 0
                    else ""
                )
                session_part = (
                    f"  <i>(sess ${session['cost_usd']:.4f})</i>"
                    if session.get("requests", 0) > 0
                    else ""
                )
                lines.append(
                    f"    {agent['name']:<10} <code>{agent['model']}</code>"
                    f"  in:{fmt_tokens(all_time['input'])}"
                    f"  out:{fmt_tokens(all_time['output'])}"
                    f"{thinking}"
                    f"  <b>${all_time['cost_usd']:.4f}</b>{session_part}"
                )
                for key in backend_total:
                    backend_total[key] += all_time.get(key, 0)
            if len(agents) > 1:
                lines.append(
                    f"    {'─' * 38}\n"
                    f"    {'Subtotal':<10}  "
                    f"in:{fmt_tokens(backend_total['input'])}"
                    f"  out:{fmt_tokens(backend_total['output'])}"
                    f"  <b>${backend_total['cost_usd']:.4f}</b>"
                )

    lines.append(f"\n{'═' * 44}")
    all_time = totals["all_time"]
    lines.append(
        f"<b>All-time</b>  {total_agents} agents"
        f"  in:{fmt_tokens(all_time['input'])}"
        f"  out:{fmt_tokens(all_time['output'])}"
        + (
            f"  💭{fmt_tokens(all_time['thinking'])}"
            if all_time["thinking"] > 0
            else ""
        )
        + f"  <b>${all_time['cost_usd']:.4f}</b>"
        f"  ({all_time['requests']} req)"
    )

    session = totals["session"]
    if session["requests"] > 0:
        lines.append(
            f"<b>Session</b>    in:{fmt_tokens(session['input'])}"
            f"  out:{fmt_tokens(session['output'])}"
            + (
                f"  💭{fmt_tokens(session['thinking'])}"
                if session["thinking"] > 0
                else ""
            )
            + f"  <b>${session['cost_usd']:.4f}</b>"
        )

    weekly = totals["weekly"]
    if weekly["requests"] > 0:
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        days_ago = (now_utc.weekday() + 1) % 7
        week_label = (now_utc - datetime.timedelta(days=days_ago)).strftime("%m/%d")
        lines.append(
            f"<b>This week</b>  (since {week_label})"
            f"  in:{fmt_tokens(weekly['input'])}"
            f"  out:{fmt_tokens(weekly['output'])}"
            + (
                f"  💭{fmt_tokens(weekly['thinking'])}"
                if weekly["thinking"] > 0
                else ""
            )
            + f"  <b>${weekly['cost_usd']:.4f}</b>"
        )

    monthly = totals["monthly"]
    if monthly["requests"] > 0:
        now_month = datetime.datetime.now(datetime.timezone.utc)
        month_label = now_month.strftime(f"%b 1–{now_month.day}")
        lines.append(
            f"<b>This month</b> ({month_label})"
            f"  in:{fmt_tokens(monthly['input'])}"
            f"  out:{fmt_tokens(monthly['output'])}"
            + (
                f"  💭{fmt_tokens(monthly['thinking'])}"
                if monthly["thinking"] > 0
                else ""
            )
            + f"  <b>${monthly['cost_usd']:.4f}</b>"
        )

    await runtime._reply_text(update, "\n".join(lines), parse_mode="HTML")
