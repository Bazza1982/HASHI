from __future__ import annotations

import datetime
from typing import Any

from orchestrator import ui_language


_USAGE_TOTAL_NUMERIC_FIELDS = (
    "input",
    "output",
    "thinking",
    "total_tokens",
    "cost_usd",
    "requests",
    "provider_requests",
    "provider_metrics_records",
    "prompt_cache_hit_tokens",
    "prompt_cache_miss_tokens",
    "cache_observed_input_tokens",
    "cache_metrics_records",
    "no_cache_cost_usd",
    "no_cache_cost_known_records",
    "cache_savings_usd",
    "cache_savings_known_records",
    "thinking_in_output_tokens",
    "separate_thinking_tokens",
)


def _empty_usage_total() -> dict[str, Any]:
    return {
        **{key: 0 for key in _USAGE_TOTAL_NUMERIC_FIELDS},
        "pricing_revisions": [],
    }


def _merge_usage_total(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key in _USAGE_TOTAL_NUMERIC_FIELDS:
        target[key] += source.get(key, 0) or 0
    target["pricing_revisions"] = sorted(
        {
            str(value)
            for value in list(target.get("pricing_revisions") or [])
            + list(source.get("pricing_revisions") or [])
            if str(value).strip()
        }
    )


def _format_usage_metrics(data: dict[str, Any], fmt_tokens) -> str:
    """Render cache/provider telemetry only when new-format records exist."""

    provider_requests = int(data.get("provider_requests") or 0)
    rich_records = int(data.get("provider_metrics_records") or 0)
    total_records = int(data.get("requests") or 0)
    if rich_records <= 0 or provider_requests <= 0:
        return ""
    cache_records = int(data.get("cache_metrics_records") or 0)
    no_cache_records = int(data.get("no_cache_cost_known_records") or 0)
    savings_records = int(data.get("cache_savings_known_records") or 0)
    cache_input = int(data.get("cache_observed_input_tokens") or 0)
    detailed_records = min(cache_records, no_cache_records, savings_records)
    detailed = (
        detailed_records > 0
        and cache_records == no_cache_records == savings_records
        and cache_input > 0
    )
    covered_records = detailed_records if detailed else rich_records
    coverage = (
        ui_language.tr(
            "usage.metrics.coverage", covered=covered_records, total=total_records
        )
        if covered_records < total_records
        else ""
    )
    if not detailed:
        return ui_language.tr(
            "usage.metrics.provider_cache_unknown",
            provider_requests=provider_requests,
            coverage=coverage,
        )
    cache_hit = int(data.get("prompt_cache_hit_tokens") or 0)
    no_cache_cost = float(data.get("no_cache_cost_usd") or 0.0)
    cache_savings = float(data.get("cache_savings_usd") or 0.0)
    cache_rate = max(0.0, min(100.0, cache_hit * 100.0 / cache_input))
    savings_rate = (
        max(0.0, min(100.0, cache_savings * 100.0 / no_cache_cost))
        if no_cache_cost > 0
        else 0.0
    )
    revisions = ", ".join(str(value) for value in data.get("pricing_revisions") or [])
    return ui_language.tr(
        "usage.metrics.full",
        provider_requests=provider_requests,
        cache_hit_tokens=fmt_tokens(cache_hit),
        cache_input_tokens=fmt_tokens(cache_input),
        cache_hit_percent=f"{cache_rate:.1f}",
        no_cache_cost=f"US${no_cache_cost:.4f}",
        cache_savings=f"US${cache_savings:.4f}",
        cache_savings_percent=f"{savings_rate:.1f}",
        pricing_revision=revisions or ui_language.tr("common.unknown"),
        coverage=coverage,
    )


def _format_reasoning_annotation(data: dict[str, Any], fmt_tokens) -> str:
    thinking = int(data.get("thinking") or 0)
    if thinking <= 0:
        return ""
    included = int(data.get("thinking_in_output_tokens") or 0)
    separate = int(data.get("separate_thinking_tokens") or 0)
    if included <= 0 and separate <= 0:
        # Legacy records predate the explicit split but already persisted a
        # non-double-counted total_tokens value.
        included = (
            thinking
            if int(data.get("total_tokens") or 0)
            <= int(data.get("input") or 0) + int(data.get("output") or 0)
            else 0
        )
    key = (
        "usage.reasoning_in_output"
        if included == thinking and separate == 0
        else "usage.reasoning_separate"
    )
    return ui_language.tr(key, tokens=fmt_tokens(thinking))


async def cmd_usage(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    try:
        from tools.token_tracker import format_summary_text, get_summary
    except ImportError:
        await runtime._reply_text(update, ui_language.tr("usage.tracker_unavailable"))
        return

    args = [arg.strip().lower() for arg in (context.args or []) if arg.strip()]
    show_all = bool(args and args[0] == "all")

    if show_all:
        orchestrator = getattr(runtime, "orchestrator", None)
        if orchestrator is None:
            await runtime._reply_text(
                update, ui_language.tr("usage.orchestrator_unavailable_all")
            )
            return
        lines = [f"<b>{ui_language.tr('usage.title_all')}</b>\n"]
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
                f"<b>{agent_runtime.name}</b>  "
                + ui_language.tr(
                    "usage.agent_line", tokens=f"{tokens // 1000}K", cost=f"{cost:.4f}"
                )
                + (
                    "  ("
                    + ui_language.tr(
                        "usage.session_suffix",
                        tokens=f"{session_tokens // 1000}K",
                        cost=f"{session_cost:.4f}",
                    )
                    + ")"
                    if session.get("requests")
                    else ""
                )
            )
        lines.append(
            f"\n<b>{ui_language.tr('usage.total_cost', cost=f'{total_cost:.4f}')}</b>"
        )
        await runtime._reply_text(update, "\n".join(lines), parse_mode="HTML")
        return

    summary = get_summary(runtime.workspace_dir, session_id=runtime.session_id_dt)
    labels = {
        "title": ui_language.tr("usage.title"),
        "thinking": ui_language.tr("usage.thinking"),
        "input": ui_language.tr("usage.input_short"),
        "output": ui_language.tr("usage.output_short"),
        "total": ui_language.tr("usage.total_tokens"),
        "requests": ui_language.tr("usage.requests_short", count="{count}"),
        "no_record": ui_language.tr("usage.no_record"),
        "all_time": ui_language.tr("usage.block.all_time"),
        "session": ui_language.tr("usage.block.session"),
        "by_model": ui_language.tr("usage.by_model"),
        "tokens": ui_language.tr("status.tokens"),
    }
    try:
        text = format_summary_text(
            summary,
            agent_name=runtime.name,
            labels=labels,
        )
    except TypeError as exc:
        if "unexpected keyword argument 'labels'" not in str(exc):
            raise
        text = format_summary_text(summary, agent_name=runtime.name)
    await runtime._reply_text(update, text, parse_mode="HTML")


async def cmd_token(runtime: Any, update: Any, context: Any) -> None:
    """Render system-wide token usage grouped by backend type."""
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    try:
        from tools.token_tracker import fmt_tokens, get_summary_extended
    except ImportError:
        await runtime._reply_text(update, ui_language.tr("usage.tracker_unavailable"))
        return

    orchestrator = getattr(runtime, "orchestrator", None)
    if orchestrator is None:
        await runtime._reply_text(
            update, ui_language.tr("usage.orchestrator_unavailable")
        )
        return

    groups: dict[str, dict[str, list]] = {}
    totals = {
        period: _empty_usage_total()
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
            category = "cli"
        elif backend.endswith("-api"):
            category = "api"
        else:
            category = "other"
        groups.setdefault(category, {}).setdefault(backend, []).append(
            {"name": agent_runtime.name, "model": model, "summary": summary}
        )
        for period in totals:
            source = summary.get(period) or {}
            _merge_usage_total(totals[period], source)

    if total_agents == 0:
        await runtime._reply_text(update, ui_language.tr("usage.none"))
        return

    lines = [f"<b>{ui_language.tr('usage.summary_all')}</b>"]
    category_order = ["cli", "api", "other"]
    for category in category_order:
        if category not in groups:
            continue
        lines.append(f"\n<b>{ui_language.tr(f'usage.category.{category}')}</b>")
        for backend, agents in sorted(groups[category].items()):
            lines.append(f"  <b>{backend}</b>")
            backend_total = _empty_usage_total()
            for agent in agents:
                all_time = agent["summary"]["all_time"]
                session = agent["summary"].get("session") or {}
                thinking = _format_reasoning_annotation(all_time, fmt_tokens)
                session_part = (
                    "  <i>("
                    + ui_language.tr(
                        "usage.session_cost", cost=f"{session['cost_usd']:.4f}"
                    )
                    + ")</i>"
                    if session.get("requests", 0) > 0
                    else ""
                )
                lines.append(
                    f"    {agent['name']:<10} <code>{agent['model']}</code>"
                    f"  {ui_language.tr('usage.input_short')}:{fmt_tokens(all_time['input'])}"
                    f"  {ui_language.tr('usage.output_short')}:{fmt_tokens(all_time['output'])}"
                    f"{thinking}"
                    f"  <b>${all_time['cost_usd']:.4f}</b>{session_part}"
                )
                metrics = _format_usage_metrics(all_time, fmt_tokens)
                if metrics:
                    lines.append(f"      {metrics}")
                _merge_usage_total(backend_total, all_time)
            if len(agents) > 1:
                lines.append(
                    f"    {'─' * 38}\n"
                    f"    {ui_language.tr('usage.subtotal'):<10}  "
                    f"{ui_language.tr('usage.input_short')}:{fmt_tokens(backend_total['input'])}"
                    f"  {ui_language.tr('usage.output_short')}:{fmt_tokens(backend_total['output'])}"
                    f"{_format_reasoning_annotation(backend_total, fmt_tokens)}"
                    f"  <b>${backend_total['cost_usd']:.4f}</b>"
                )
                metrics = _format_usage_metrics(backend_total, fmt_tokens)
                if metrics:
                    lines.append(f"      {metrics}")

    lines.append(f"\n{'═' * 44}")
    all_time = totals["all_time"]
    lines.append(
        f"<b>{ui_language.tr('usage.all_time')}</b>  "
        f"{ui_language.tr('usage.agent_count', count=total_agents)}"
        f"  {ui_language.tr('usage.input_short')}:{fmt_tokens(all_time['input'])}"
        f"  {ui_language.tr('usage.output_short')}:{fmt_tokens(all_time['output'])}"
        + _format_reasoning_annotation(all_time, fmt_tokens)
        + f"  <b>${all_time['cost_usd']:.4f}</b>"
        f"  ({ui_language.tr('usage.requests_short', count=all_time['requests'])})"
    )
    all_time_metrics = _format_usage_metrics(all_time, fmt_tokens)
    if all_time_metrics:
        lines.append(f"  {all_time_metrics}")

    session = totals["session"]
    if session["requests"] > 0:
        lines.append(
            f"<b>{ui_language.tr('usage.session')}</b>    "
            f"{ui_language.tr('usage.input_short')}:{fmt_tokens(session['input'])}"
            f"  {ui_language.tr('usage.output_short')}:{fmt_tokens(session['output'])}"
            + _format_reasoning_annotation(session, fmt_tokens)
            + f"  <b>${session['cost_usd']:.4f}</b>"
        )
        session_metrics = _format_usage_metrics(session, fmt_tokens)
        if session_metrics:
            lines.append(f"  {session_metrics}")

    weekly = totals["weekly"]
    if weekly["requests"] > 0:
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        days_ago = (now_utc.weekday() + 1) % 7
        week_label = (now_utc - datetime.timedelta(days=days_ago)).strftime("%m/%d")
        lines.append(
            f"<b>{ui_language.tr('usage.this_week')}</b>  "
            f"({ui_language.tr('usage.since', date=week_label)})"
            f"  {ui_language.tr('usage.input_short')}:{fmt_tokens(weekly['input'])}"
            f"  {ui_language.tr('usage.output_short')}:{fmt_tokens(weekly['output'])}"
            + _format_reasoning_annotation(weekly, fmt_tokens)
            + f"  <b>${weekly['cost_usd']:.4f}</b>"
        )
        weekly_metrics = _format_usage_metrics(weekly, fmt_tokens)
        if weekly_metrics:
            lines.append(f"  {weekly_metrics}")

    monthly = totals["monthly"]
    if monthly["requests"] > 0:
        now_month = datetime.datetime.now(datetime.timezone.utc)
        month_label = now_month.strftime(f"%b 1–{now_month.day}")
        lines.append(
            f"<b>{ui_language.tr('usage.this_month')}</b> ({month_label})"
            f"  {ui_language.tr('usage.input_short')}:{fmt_tokens(monthly['input'])}"
            f"  {ui_language.tr('usage.output_short')}:{fmt_tokens(monthly['output'])}"
            + _format_reasoning_annotation(monthly, fmt_tokens)
            + f"  <b>${monthly['cost_usd']:.4f}</b>"
        )
        monthly_metrics = _format_usage_metrics(monthly, fmt_tokens)
        if monthly_metrics:
            lines.append(f"  {monthly_metrics}")

    await runtime._reply_text(update, "\n".join(lines), parse_mode="HTML")
