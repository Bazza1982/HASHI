from __future__ import annotations

import importlib
from typing import Any


async def cmd_token(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return

    try:
        token_tracker = importlib.import_module("tools.token_tracker")
    except ImportError:
        await runtime._reply_text(update, "❌ token_tracker not available.")
        return

    get_summary_extended = token_tracker.get_summary_extended
    fmt_tokens = token_tracker.fmt_tokens

    orchestrator = getattr(runtime, "orchestrator", None)
    if orchestrator is None:
        await runtime._reply_text(update, "❌ Orchestrator unavailable.")
        return

    groups: dict[str, dict[str, list[dict[str, Any]]]] = {}
    totals = {
        period: {"input": 0, "output": 0, "thinking": 0, "cost_usd": 0.0, "requests": 0}
        for period in ("all_time", "session", "weekly", "monthly")
    }
    total_agents = 0

    for other_runtime in orchestrator.runtimes:
        summary = get_summary_extended(other_runtime.workspace_dir, session_id=other_runtime.session_id_dt)
        if summary["all_time"]["requests"] == 0:
            continue
        total_agents += 1

        backend_manager = getattr(other_runtime, "backend_manager", None)
        backend = (
            getattr(backend_manager, "active_backend", None)
            or getattr(getattr(other_runtime, "config", None), "active_backend", None)
            or "unknown"
        )

        model = "unknown"
        try:
            model = other_runtime.get_current_model() or "unknown"
        except Exception:
            pass

        if backend.endswith("-cli"):
            category = "🖥️ CLI Backends"
        elif backend.endswith("-api"):
            category = "🌐 API Backends"
        else:
            category = "❓ Other"

        groups.setdefault(category, {}).setdefault(backend, []).append(
            {"name": other_runtime.name, "model": model, "summary": summary}
        )

        for period in ("all_time", "session", "weekly", "monthly"):
            source = summary.get(period) or {}
            for key in ("input", "output", "thinking", "cost_usd", "requests"):
                totals[period][key] += source.get(key, 0)

    if total_agents == 0:
        await runtime._reply_text(update, "📊 No token usage recorded yet.")
        return

    lines = ["<b>📊 Token Summary — All Agents</b>"]
    for category in ("🖥️ CLI Backends", "🌐 API Backends", "❓ Other"):
        if category not in groups:
            continue
        lines.append(f"\n<b>{category}</b>")
        for backend, agents in sorted(groups[category].items()):
            lines.append(f"  <b>{backend}</b>")
            backend_all_time = {"input": 0, "output": 0, "thinking": 0, "cost_usd": 0.0, "requests": 0}
            for agent in agents:
                all_time = agent["summary"]["all_time"]
                session = agent["summary"].get("session") or {}
                thinking_part = f"  💭{fmt_tokens(all_time['thinking'])}" if all_time["thinking"] > 0 else ""
                session_part = f"  <i>(sess ${session['cost_usd']:.4f})</i>" if session.get("requests", 0) > 0 else ""
                lines.append(
                    f"    {agent['name']:<10} <code>{agent['model']}</code>"
                    f"  in:{fmt_tokens(all_time['input'])}"
                    f"  out:{fmt_tokens(all_time['output'])}"
                    f"{thinking_part}"
                    f"  <b>${all_time['cost_usd']:.4f}</b>{session_part}"
                )
                for key in ("input", "output", "thinking", "cost_usd", "requests"):
                    backend_all_time[key] += all_time.get(key, 0)
            if len(agents) > 1:
                lines.append(
                    f"    {'─'*38}\n"
                    f"    {'Subtotal':<10}  "
                    f"in:{fmt_tokens(backend_all_time['input'])}"
                    f"  out:{fmt_tokens(backend_all_time['output'])}"
                    f"  <b>${backend_all_time['cost_usd']:.4f}</b>"
                )

    lines.append(f"\n{'═'*44}")
    all_time = totals["all_time"]
    lines.append(
        f"<b>All-time</b>  {total_agents} agents"
        f"  in:{fmt_tokens(all_time['input'])}"
        f"  out:{fmt_tokens(all_time['output'])}"
        + (f"  💭{fmt_tokens(all_time['thinking'])}" if all_time["thinking"] > 0 else "")
        + f"  <b>${all_time['cost_usd']:.4f}</b>"
          f"  ({all_time['requests']} req)"
    )

    session = totals["session"]
    if session["requests"] > 0:
        lines.append(
            f"<b>Session</b>    in:{fmt_tokens(session['input'])}"
            f"  out:{fmt_tokens(session['output'])}"
            + (f"  💭{fmt_tokens(session['thinking'])}" if session["thinking"] > 0 else "")
            + f"  <b>${session['cost_usd']:.4f}</b>"
        )

    weekly = totals["weekly"]
    if weekly["requests"] > 0:
        from datetime import datetime, timedelta, timezone

        now_utc = datetime.now(timezone.utc)
        days_ago = (now_utc.weekday() + 1) % 7
        week_label = (now_utc - timedelta(days=days_ago)).strftime("%m/%d")
        lines.append(
            f"<b>This week</b>  (since {week_label})"
            f"  in:{fmt_tokens(weekly['input'])}"
            f"  out:{fmt_tokens(weekly['output'])}"
            + (f"  💭{fmt_tokens(weekly['thinking'])}" if weekly["thinking"] > 0 else "")
            + f"  <b>${weekly['cost_usd']:.4f}</b>"
        )

    monthly = totals["monthly"]
    if monthly["requests"] > 0:
        from datetime import datetime, timezone

        now_utc = datetime.now(timezone.utc)
        month_label = now_utc.strftime(f"%b 1–{now_utc.day}")
        lines.append(
            f"<b>This month</b> ({month_label})"
            f"  in:{fmt_tokens(monthly['input'])}"
            f"  out:{fmt_tokens(monthly['output'])}"
            + (f"  💭{fmt_tokens(monthly['thinking'])}" if monthly["thinking"] > 0 else "")
            + f"  <b>${monthly['cost_usd']:.4f}</b>"
        )

    await runtime._reply_text(update, "\n".join(lines), parse_mode="HTML")
