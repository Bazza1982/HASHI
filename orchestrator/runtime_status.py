from __future__ import annotations

import html
from datetime import datetime
from typing import Any, Mapping

from orchestrator.audit_mode import load_audit_config, visible_audit_criteria
from orchestrator.memory_plus_mode import get_memory_plus_status
from orchestrator import telegram_delivery_failover
from orchestrator import telegram_stream_policy
from orchestrator.wrapper_mode import load_wrapper_config, visible_wrapper_slots
from orchestrator.command_ui import card_title


def compute_status_string(runtime) -> str:
    if not runtime.backend_ready:
        return "offline"
    if telegram_delivery_failover.is_delivery_blocked(runtime):
        return "delivery-blocked"
    if runtime.telegram_connected:
        return "online"
    return "local"


def _format_duration(seconds: float | int) -> str:
    total = max(0, int(seconds))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _delivery_line(delivery: Mapping[str, Any] | None) -> str:
    if not delivery:
        return "<b>Delivery</b> · <code>HEALTHY</code>"

    blocked_until_raw = delivery.get("blocked_until")
    failover_agent = delivery.get("active_failover_agent") or "failover"
    status = str(delivery.get("status") or "blocked")
    remaining_text = "remaining unknown"

    if blocked_until_raw:
        try:
            blocked_until = datetime.fromisoformat(str(blocked_until_raw))
            now = datetime.now().astimezone()
            if blocked_until.tzinfo is None:
                blocked_until = blocked_until.replace(tzinfo=now.tzinfo)
            remaining = (blocked_until.astimezone() - now).total_seconds()
            if remaining > 0:
                remaining_text = f"~{_format_duration(remaining)} remaining"
            else:
                remaining_text = "recovery due now"
        except Exception:
            remaining_text = "remaining unknown"
    elif status == "recovery_due":
        remaining_text = "recovery due now"

    if blocked_until_raw:
        return (
            f"<b>Delivery</b> · <code>{html.escape(status.upper())}</code> · "
            f"{html.escape(remaining_text)} · until <code>{html.escape(str(blocked_until_raw))}</code> "
            f"· via <code>{html.escape(str(failover_agent))}</code>"
        )
    return (
        f"<b>Delivery</b> · <code>{html.escape(status.upper())}</code> · "
        f"{html.escape(remaining_text)} · via <code>{html.escape(str(failover_agent))}</code>"
    )


def job_counts(runtime) -> tuple[int, int]:
    if not runtime.skill_manager:
        return 0, 0
    heartbeat_count = sum(
        1
        for job in runtime.skill_manager.list_jobs("heartbeat", agent_name=runtime.name)
        if job.get("enabled")
    )
    cron_count = sum(
        1
        for job in runtime.skill_manager.list_jobs("cron", agent_name=runtime.name)
        if job.get("enabled")
    )
    return heartbeat_count, cron_count


def format_status_mode_block(mode: str, state: Mapping[str, Any], detailed: bool) -> list[str]:
    if mode == "audit":
        cfg = load_audit_config(state)
        lines = [
            "",
            "🧪 <b>AUDIT</b>",
            f"<b>Core</b> · <code>{html.escape(cfg.core_backend)} / {html.escape(cfg.core_model)}</code>",
            f"<b>Auditor</b> · <code>{html.escape(cfg.audit_backend)} / {html.escape(cfg.audit_model)}</code>",
            f"<b>Delivery</b> · <code>{html.escape(cfg.delivery)}</code>",
            f"<b>Threshold</b> · <code>{html.escape(cfg.severity_threshold)}</code>",
            f"<b>Timeout</b> · <code>{cfg.timeout_s:g}s</code>",
            "",
        ]
        if detailed:
            lines.pop()
            criteria = visible_audit_criteria(state.get("audit_criteria"))
            lines.extend(["", "🧪 <b>AUDIT CRITERIA</b>"])
            if criteria:
                for key in sorted(
                    criteria,
                    key=lambda value: (
                        not str(value).isdigit(),
                        int(value) if str(value).isdigit() else str(value),
                    ),
                ):
                    lines.append(
                        f"<code>{html.escape(str(key))}</code> · {html.escape(str(criteria[key]))}"
                    )
            else:
                lines.append("• default risk sensors")
            lines.append("")
        return lines

    if mode == "wrapper":
        cfg = load_wrapper_config(state)
        slots = visible_wrapper_slots(state.get("wrapper_slots"))
        slot_count = len(slots)
        lines = [
            "",
            "🎭 <b>WRAPPER</b>",
            f"<b>Core</b> · <code>{html.escape(cfg.core_backend)} / {html.escape(cfg.core_model)}</code>",
            f"<b>Wrapper</b> · <code>{html.escape(cfg.wrapper_backend)} / {html.escape(cfg.wrapper_model)}</code>",
            f"<b>Context window</b> · <code>{cfg.context_window}</code>",
            f"<b>Slots</b> · <code>{slot_count}</code> configured",
            "",
        ]
        if detailed:
            lines.pop()
            lines.extend(["", "🎭 <b>WRAPPER SLOTS</b>"])
            if slots:
                for key in sorted(
                    slots,
                    key=lambda value: (
                        not str(value).isdigit(),
                        int(value) if str(value).isdigit() else str(value),
                    ),
                ):
                    lines.append(
                        f"<code>{html.escape(str(key))}</code> · {html.escape(str(slots[key]))}"
                    )
            else:
                lines.append("• none")
            lines.append("")
        return lines

    return []


def build_status_text(runtime, detailed: bool = False) -> str:
    active_skills = (
        sorted(runtime.skill_manager.get_active_toggle_ids(runtime.workspace_dir))
        if runtime.skill_manager
        else []
    )
    recall_on = "recall" in active_skills
    heartbeat_count, cron_count = runtime._job_counts()
    active_job = runtime.skill_manager.get_active_heartbeat_job(runtime.name) if runtime.skill_manager else None
    active_mode = "ON" if active_job and active_job.get("enabled") else "OFF"
    active_interval = (
        f"{max(1, int(active_job.get('interval_seconds', 600) // 60))} min"
        if active_job
        else "10 min"
    )
    current = runtime.current_request_meta or {}
    current_line = (
        f"{current.get('request_id')} • {current.get('source')} • {current.get('summary')}"
        if current
        else "none"
    )
    health_line = (
        f"⚠️ {runtime.last_error_summary} ({runtime._format_age(runtime.last_error_at)})"
        if runtime.last_error_summary
        else "✅ healthy"
    )
    delivery = telegram_delivery_failover.delivery_status_summary(runtime)
    stream_policy = telegram_stream_policy.get_policy(runtime)
    tg_status = "✓" if runtime.telegram_connected else "✗"
    wa_status = "✓" if runtime._get_whatsapp_connected() else "✗"
    channel_line = f"Telegram {tg_status} • WhatsApp {wa_status} • Workbench ✓"
    mode_str = getattr(runtime.backend_manager, "agent_mode", "flex")
    try:
        state_snapshot = runtime.backend_manager.get_state_snapshot()
    except Exception:
        state_snapshot = {}
    memory_plus = get_memory_plus_status(runtime.workspace_dir)
    current_effort = runtime._get_current_effort() or "n/a"
    session_id_short = "none"
    if mode_str == "fixed" and getattr(runtime.backend_manager, "current_backend", None):
        sid = getattr(runtime.backend_manager.current_backend, "_session_id", None) or "none"
        session_id_short = sid[:8] + "…" if sid != "none" and len(sid) > 8 else sid
    status = (
        compute_status_string(runtime).upper()
        if hasattr(runtime, "backend_ready")
        else ("ONLINE" if runtime.telegram_connected else "LOCAL")
    )
    lines = [
        card_title("📊", "Hashi status"),
        "",
        f"<b>Current</b> · <b>{html.escape(status)}</b>",
        f"<b>Agent</b> · <code>{html.escape(str(runtime.name))}</code>",
        f"<b>Mode</b> · <code>{html.escape(str(mode_str))}</code>",
        f"<b>Backend</b> · <code>{html.escape(str(runtime.config.active_backend))}</code>",
        f"<b>Model</b> · <code>{html.escape(str(runtime.get_current_model()))}</code>",
        f"<b>Effort</b> · <code>{html.escape(str(current_effort))}</code>",
        (
            f"<b>Memory+</b> · <code>{'ON' if memory_plus['enabled'] else 'OFF'}</code>"
            f" · <code>{memory_plus['open_items']}</code> open"
            + (
                f" · carried from <code>{html.escape(str(memory_plus['carryover_from']))}</code>"
                if memory_plus["carryover_from"]
                else ""
            )
        ),
    ]
    if mode_str == "fixed":
        lines.append(f"<b>Session</b> · <code>{html.escape(str(session_id_short))}</code>")
    lines.extend(runtime._format_status_mode_block(mode_str, state_snapshot, detailed))
    lines.extend(
        [
            "",
            "<b>CONNECTIONS</b>",
            f"<b>Channels</b> · {html.escape(channel_line)}",
            _delivery_line(delivery),
            f"<b>Telegram stream</b> · <code>{'ON' if stream_policy.enabled else 'OFF'}</code> · {html.escape(str(stream_policy.source))}",
            "",
            "<b>ACTIVITY</b>",
            f"<b>Runtime</b> · <code>{'BUSY' if runtime.is_generating else 'IDLE'}</code> · queue <code>{runtime.queue.qsize()}</code> · process <code>{html.escape(str(runtime._process_info()))}</code>",
            f"<b>Request</b> · {html.escape(str(current_line))}",
            f"<b>Memory</b> · skills {html.escape(', '.join(active_skills) if active_skills else 'none')} · recall <code>{'ON' if recall_on else 'OFF'}</code> · FYI <code>{'ARMED' if runtime._pending_session_primer else 'CLEAR'}</code>",
            f"<b>Proactive</b> · <code>{active_mode}</code> · every {html.escape(active_interval)} · hb <code>{heartbeat_count}</code> · cron <code>{cron_count}</code>",
            f"<b>Health</b> · {html.escape(health_line)}",
            f"<b>Last activity</b> · success {html.escape(runtime._format_age(runtime.last_success_at))} · activity {html.escape(runtime._format_age(runtime.last_activity_at))}",
        ]
    )
    if detailed:
        allowed = ", ".join(b["engine"] for b in runtime.config.allowed_backends)

        session_id = "none"
        if mode_str == "fixed" and getattr(runtime.backend_manager, "current_backend", None):
            session_id = getattr(runtime.backend_manager.current_backend, "_session_id", "none") or "none"

        lines.extend(
            [
                "",
                "<b>DETAILS</b>",
                f"<b>Workspace</b> · <code>{html.escape(str(runtime.workspace_dir))}</code>",
                f"<b>Transcript</b> · <code>{html.escape(runtime.transcript_log_path.name)}</code>",
                f"<b>Started</b> · <code>{html.escape(runtime.session_started_at.isoformat(timespec='seconds'))}</code>",
                f"<b>Allowed backends</b> · {html.escape(allowed or 'none')}",
                f"<b>Session ID</b> · <code>{html.escape(str(session_id))}</code>",
                f"<b>Retry cache</b> · prompt <code>{'YES' if runtime.last_prompt else 'NO'}</code> · response <code>{'YES' if runtime.last_response else 'NO'}</code>",
                f"<b>Primers</b> · FYI <code>{'ARMED' if runtime._pending_session_primer else 'CLEAR'}</code> · auto-recall <code>{'ARMED' if runtime._pending_auto_recall_context else 'CLEAR'}</code>",
                f"<b>Bridge memory</b> · <code>{runtime.memory_store.get_stats()['turns']}</code> turns · <code>{runtime.memory_store.get_stats()['memories']}</code> memories",
                f"<b>Memory+ card</b> · <code>{memory_plus['today_chars']}</code> chars · "
                f"<code>{memory_plus['history_days']}</code> archived days · <code>{html.escape(str(memory_plus['state_path']))}</code>",
                f"<b>Handoff files</b> · recent <code>{'YES' if runtime.recent_context_path.exists() else 'NO'}</code> · handoff <code>{'YES' if runtime.handoff_path.exists() else 'NO'}</code>",
                f"<b>Verbose</b> · <code>{'ON' if runtime._verbose else 'OFF'}</code>",
                f"<b>Think</b> · <code>{'ON' if runtime._think else 'OFF'}</code>",
                f"<b>Preview</b> · <code>{'ON' if stream_policy.preview_enabled else 'OFF'}</code> · {html.escape(str(stream_policy.component_sources['preview']))}",
                f"<b>Last switch</b> · {html.escape(runtime._format_age(runtime.last_backend_switch_at))}",
            ]
        )
        try:
            from tools.token_tracker import format_status_line, get_summary

            usage_summary = get_summary(runtime.workspace_dir, session_id=runtime.session_id_dt)
            lines.append(f"<b>Tokens</b> · {html.escape(format_status_line(usage_summary))}")
        except Exception:
            pass
    else:
        lines.append("")
        lines.append("Use <code>/status full</code> for more detail.")
    return "\n".join(lines)


async def cmd_status(runtime, update, context) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    detailed = bool(context.args and context.args[0].strip().lower() in {"full", "all", "more"})
    await runtime._reply_text(
        update,
        runtime._build_status_text(detailed=detailed),
        parse_mode="HTML",
    )
