from __future__ import annotations

import html
from datetime import datetime
from typing import Any, Mapping

from orchestrator import runtime_pending, telegram_delivery_failover, ui_language
from orchestrator import telegram_stream_policy
from orchestrator.audit_mode import load_audit_config, visible_audit_criteria
from orchestrator.command_ui import card_title
from orchestrator.memory_plus_mode import get_memory_plus_status, is_memory_plus_enabled
from orchestrator.wrapper_mode import load_wrapper_config, visible_wrapper_slots
from orchestrator.her_v2.models import effort_display_label


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
        return ui_language.tr(
            "status.duration.days", days=days, hours=hours
        )
    if hours:
        return ui_language.tr(
            "status.duration.hours", hours=hours, minutes=minutes
        )
    if minutes:
        return ui_language.tr(
            "status.duration.minutes", minutes=minutes, seconds=secs
        )
    return ui_language.tr("status.duration.seconds", seconds=secs)


def _delivery_line(delivery: Mapping[str, Any] | None) -> str:
    if not delivery:
        return (
            f"<b>{html.escape(ui_language.tr('common.delivery'))}</b> · "
            f"<code>{html.escape(ui_language.tr('status.delivery.healthy'))}</code>"
        )

    blocked_until_raw = delivery.get("blocked_until")
    failover_agent = delivery.get("active_failover_agent") or "failover"
    status = str(delivery.get("status") or "blocked")
    remaining_text = ui_language.tr("status.delivery.remaining_unknown")

    if blocked_until_raw:
        try:
            blocked_until = datetime.fromisoformat(str(blocked_until_raw))
            now = datetime.now().astimezone()
            if blocked_until.tzinfo is None:
                blocked_until = blocked_until.replace(tzinfo=now.tzinfo)
            remaining = (blocked_until.astimezone() - now).total_seconds()
            if remaining > 0:
                remaining_text = ui_language.tr(
                    "status.delivery.remaining",
                    duration=_format_duration(remaining),
                )
            else:
                remaining_text = ui_language.tr("status.delivery.due")
        except Exception:
            remaining_text = ui_language.tr("status.delivery.remaining_unknown")
    elif status == "recovery_due":
        remaining_text = ui_language.tr("status.delivery.due")

    if blocked_until_raw and status == "blocked":
        return (
            f"<b>{html.escape(ui_language.tr('common.delivery'))}</b> · "
            f"<code>{html.escape(status.upper())}</code> · "
            f"{html.escape(remaining_text)} · "
            f"{html.escape(ui_language.tr('status.delivery.until'))} "
            f"<code>{html.escape(str(blocked_until_raw))}</code> · "
            f"{html.escape(ui_language.tr('status.delivery.via'))} "
            f"<code>{html.escape(str(failover_agent))}</code>"
        )
    return (
        f"<b>{html.escape(ui_language.tr('common.delivery'))}</b> · "
        f"<code>{html.escape(status.upper())}</code> · "
        f"{html.escape(remaining_text)} · "
        f"{html.escape(ui_language.tr('status.delivery.via'))} "
        f"<code>{html.escape(str(failover_agent))}</code>"
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


def _status_value(value: str) -> str:
    key = f"status.state.{str(value).strip().lower().replace('-', '_')}"
    translated = ui_language.tr(key)
    return value.upper() if translated == key else translated


def _source_value(value: Any) -> str:
    normalized = str(value or "").strip().lower().replace(" ", "_")
    key = f"status.source.{normalized}"
    translated = ui_language.tr(key)
    return str(value) if translated == key else translated


def format_status_mode_block(mode: str, state: Mapping[str, Any], detailed: bool) -> list[str]:
    if mode == "audit":
        cfg = load_audit_config(state)
        lines = [
            "",
            f"🧪 <b>{html.escape(ui_language.tr('status.mode.audit'))}</b>",
            f"<b>{html.escape(ui_language.tr('common.core'))}</b> · "
            f"<code>{html.escape(cfg.core_backend)} / {html.escape(cfg.core_model)}</code>",
            f"<b>{html.escape(ui_language.tr('common.auditor'))}</b> · "
            f"<code>{html.escape(cfg.audit_backend)} / {html.escape(cfg.audit_model)}</code>",
            f"<b>{html.escape(ui_language.tr('common.delivery'))}</b> · "
            f"<code>{html.escape(cfg.delivery)}</code>",
            f"<b>{html.escape(ui_language.tr('common.threshold'))}</b> · "
            f"<code>{html.escape(cfg.severity_threshold)}</code>",
            "",
        ]
        if detailed:
            lines.pop()
            criteria = visible_audit_criteria(state.get("audit_criteria"))
            lines.extend(
                [
                    "",
                    f"🧪 <b>{html.escape(ui_language.tr('status.audit_criteria'))}</b>",
                ]
            )
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
                lines.append(ui_language.tr("status.default_risk_sensors"))
            lines.append("")
        return lines

    if mode == "wrapper":
        cfg = load_wrapper_config(state)
        slots = visible_wrapper_slots(state.get("wrapper_slots"))
        slot_count = len(slots)
        lines = [
            "",
            f"🎭 <b>{html.escape(ui_language.tr('status.mode.wrapper'))}</b>",
            f"<b>{html.escape(ui_language.tr('common.core'))}</b> · "
            f"<code>{html.escape(cfg.core_backend)} / {html.escape(cfg.core_model)}</code>",
            f"<b>{html.escape(ui_language.tr('common.wrapper'))}</b> · "
            f"<code>{html.escape(cfg.wrapper_backend)} / {html.escape(cfg.wrapper_model)}</code>",
            f"<b>{html.escape(ui_language.tr('status.context_window'))}</b> · "
            f"<code>{cfg.context_window}</code>",
            f"<b>{html.escape(ui_language.tr('common.slots'))}</b> · "
            f"<code>{slot_count}</code> "
            f"{html.escape(ui_language.tr('status.configured_suffix'))}",
            "",
        ]
        if detailed:
            lines.pop()
            lines.extend(
                [
                    "",
                    f"🎭 <b>{html.escape(ui_language.tr('status.wrapper_slots'))}</b>",
                ]
            )
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
                lines.append(f"• {html.escape(ui_language.tr('status.none'))}")
            lines.append("")
        return lines

    return []


def build_status_text(runtime, detailed: bool = False, *, update: Any | None = None) -> str:
    active_skills = (
        sorted(runtime.skill_manager.get_active_toggle_ids(runtime.workspace_dir))
        if runtime.skill_manager
        else []
    )
    recall_on = "recall" in active_skills
    heartbeat_count, cron_count = runtime._job_counts()
    active_job = runtime.skill_manager.get_active_heartbeat_job(runtime.name) if runtime.skill_manager else None
    active_mode = ui_language.tr(
        "common.on" if active_job and active_job.get("enabled") else "common.off"
    )
    active_interval = (
        f"{max(1, int(active_job.get('interval_seconds', 600) // 60))} "
        f"{ui_language.tr('common.minutes_short')}"
        if active_job
        else f"10 {ui_language.tr('common.minutes_short')}"
    )
    current = runtime.current_request_meta or {}
    current_line = (
        f"{current.get('request_id')} • {current.get('source')} • {current.get('summary')}"
        if current
        else ui_language.tr("status.none")
    )
    health_line = (
        f"⚠️ {runtime.last_error_summary} ({runtime._format_age(runtime.last_error_at)})"
        if runtime.last_error_summary
        else f"✅ {ui_language.tr('status.state.healthy')}"
    )
    delivery = telegram_delivery_failover.delivery_status_summary(runtime)
    display_policy = telegram_stream_policy.get_display_policy(runtime)
    think_enabled = bool(getattr(runtime, "_think", False))
    commentary_enabled = bool(getattr(runtime, "_commentary", True))
    tg_status = "✓" if runtime.telegram_connected else "✗"
    wa_status = "✓" if runtime._get_whatsapp_connected() else "✗"
    channel_line = ui_language.tr(
        "status.channel_line",
        telegram=tg_status,
        whatsapp=wa_status,
    )
    mode_str = getattr(runtime.backend_manager, "agent_mode", "flex")
    try:
        state_snapshot = runtime.backend_manager.get_state_snapshot()
    except Exception:
        state_snapshot = {}
    hashi_session = None
    if update is not None:
        try:
            from orchestrator import runtime_session

            hashi_session = runtime_session.current_session_for_update(runtime, update)
            memory_plus = get_memory_plus_status(
                runtime_session.current_session_workspace(runtime, update)
            )
            memory_plus["enabled"] = is_memory_plus_enabled(runtime.workspace_dir)
        except Exception:
            memory_plus = get_memory_plus_status(runtime.workspace_dir)
    else:
        memory_plus = get_memory_plus_status(runtime.workspace_dir)
    current_effort = runtime._get_current_effort() or "n/a"
    her_backend = str(runtime.config.active_backend) == "her-v2"
    if her_backend and current_effort != "n/a":
        try:
            current_effort = effort_display_label(current_effort)
        except ValueError:
            pass
    effort_heading = ui_language.tr(
        "status.her_execution_mode" if her_backend else "common.effort"
    )
    session_id_short = (
        str(hashi_session["session_id"])
        if hashi_session is not None
        else ui_language.tr("status.unresolved")
    )
    status = (
        _status_value(compute_status_string(runtime))
        if hasattr(runtime, "backend_ready")
        else _status_value("online" if runtime.telegram_connected else "local")
    )
    delayed_count = runtime_pending.delayed_count_now(runtime)
    runtime_line = (
        f"<b>{html.escape(ui_language.tr('status.runtime'))}</b> · "
        f"<code>{html.escape(_status_value('busy' if runtime.is_generating else 'idle'))}</code> "
        f"· {html.escape(ui_language.tr('status.queue'))} "
        f"<code>{runtime.queue.qsize()}</code> · "
        f"{html.escape(ui_language.tr('status.delayed'))} "
        f"<code>{delayed_count}</code> · "
        f"{html.escape(ui_language.tr('status.process'))} "
        f"<code>{html.escape(str(runtime._process_info()))}</code>"
    )
    lines = [
        card_title("📊", "Hashi status"),
        "",
        f"<b>{html.escape(ui_language.tr('common.current'))}</b> · <b>{html.escape(status)}</b>",
        f"<b>{html.escape(ui_language.tr('common.agent'))}</b> · "
        f"<code>{html.escape(str(runtime.name))}</code>",
        f"<b>{html.escape(ui_language.tr('common.mode'))}</b> · "
        f"<code>{html.escape(str(mode_str))}</code>",
        f"<b>{html.escape(ui_language.tr('common.backend'))}</b> · "
        f"<code>{html.escape(str(runtime.config.active_backend))}</code>",
    ]
    provider_getter = getattr(runtime, "get_current_provider", None)
    provider = provider_getter() if callable(provider_getter) else None
    if provider:
        lines.append(
            f"<b>{html.escape(ui_language.tr('common.provider'))}</b> · "
            f"<code>{html.escape(str(provider))}</code>"
        )
    lines.extend(
        [
            f"<b>{html.escape(ui_language.tr('common.model'))}</b> · "
            f"<code>{html.escape(str(runtime.get_current_model()))}</code>",
            f"<b>{effort_heading}</b> · <code>{html.escape(str(current_effort))}</code>",
            (
                f"<b>Memory+</b> · "
                f"<code>{html.escape(ui_language.tr('common.on' if memory_plus['enabled'] else 'common.off'))}</code>"
                f" · {html.escape(ui_language.tr('status.memory.open', count=memory_plus['open_items']))}"
                + (
                    " · "
                    + ui_language.tr(
                        "status.memory.carried",
                        date=(
                            f"<code>{html.escape(str(memory_plus['carryover_from']))}</code>"
                        ),
                    )
                    if memory_plus["carryover_from"]
                    else ""
                )
            ),
            f"<b>{html.escape(ui_language.tr('status.think'))}</b> · "
            f"<code>{html.escape(ui_language.tr('common.on' if think_enabled else 'common.off'))}</code>",
            (
                f"<b>{html.escape(ui_language.tr('status.commentary'))}</b> · "
                f"<code>{html.escape(ui_language.tr('common.on' if commentary_enabled else 'common.off'))}</code>"
            ),
            (
                f"<b>{html.escape(ui_language.tr('status.typing'))}</b> · "
                f"<code>{html.escape(ui_language.tr('common.on' if display_policy.typing_enabled else 'common.off'))}</code>"
                f" · {html.escape(_source_value(display_policy.source))}"
            ),
        ]
    )
    lines.append(
        f"<b>{html.escape(ui_language.tr('status.session'))}</b> · "
        f"<code>{html.escape(str(session_id_short))}</code>"
        + (
            f" · {html.escape(ui_language.tr('status.generation'))} "
            f"<code>{int(hashi_session['context_generation'])}</code>"
            if hashi_session is not None
            else ""
        )
    )
    lines.extend(runtime._format_status_mode_block(mode_str, state_snapshot, detailed))
    lines.extend(
        [
            "",
            f"<b>{html.escape(ui_language.tr('status.section.connections'))}</b>",
            f"<b>{html.escape(ui_language.tr('status.channels'))}</b> · "
            f"{html.escape(channel_line)}",
            _delivery_line(delivery),
            "",
            f"<b>{html.escape(ui_language.tr('status.section.activity'))}</b>",
            runtime_line,
            f"<b>{html.escape(ui_language.tr('status.request'))}</b> · "
            f"{html.escape(str(current_line))}",
            f"<b>{html.escape(ui_language.tr('status.memory'))}</b> · "
            f"{html.escape(ui_language.tr('status.runtime_settings', settings=', '.join(active_skills) if active_skills else ui_language.tr('status.none')))} "
            f"· {html.escape(ui_language.tr('status.recall'))} "
            f"<code>{html.escape(ui_language.tr('common.on' if recall_on else 'common.off'))}</code> "
            f"· {html.escape(ui_language.tr('status.fyi'))} "
            f"<code>{html.escape(ui_language.tr('status.state.armed' if runtime._pending_session_primer else 'status.state.clear'))}</code>",
            f"<b>{html.escape(ui_language.tr('status.proactive'))}</b> · "
            f"<code>{html.escape(active_mode)}</code> · "
            f"{html.escape(ui_language.tr('status.every', interval=active_interval))} · "
            f"{html.escape(ui_language.tr('status.heartbeat_short'))} "
            f"<code>{heartbeat_count}</code> · "
            f"{html.escape(ui_language.tr('status.cron_short'))} "
            f"<code>{cron_count}</code>",
            f"<b>{html.escape(ui_language.tr('status.health'))}</b> · "
            f"{html.escape(health_line)}",
            f"<b>{html.escape(ui_language.tr('status.last_activity'))}</b> · "
            f"{html.escape(ui_language.tr('status.success'))} "
            f"{html.escape(runtime._format_age(runtime.last_success_at))} · "
            f"{html.escape(ui_language.tr('status.activity'))} "
            f"{html.escape(runtime._format_age(runtime.last_activity_at))}",
        ]
    )
    if detailed:
        allowed = ", ".join(b["engine"] for b in runtime.config.allowed_backends)

        session_id = session_id_short

        lines.extend(
            [
                "",
                f"<b>{html.escape(ui_language.tr('status.section.details'))}</b>",
                f"<b>{html.escape(ui_language.tr('status.workspace'))}</b> · "
                f"<code>{html.escape(str(runtime.workspace_dir))}</code>",
                f"<b>{html.escape(ui_language.tr('status.transcript'))}</b> · "
                f"<code>{html.escape(runtime.transcript_log_path.name)}</code>",
                f"<b>{html.escape(ui_language.tr('status.started'))}</b> · "
                f"<code>{html.escape(runtime.session_started_at.isoformat(timespec='seconds'))}</code>",
                f"<b>{html.escape(ui_language.tr('status.allowed_backends'))}</b> · "
                f"{html.escape(allowed or ui_language.tr('status.none'))}",
                f"<b>{html.escape(ui_language.tr('status.session_id'))}</b> · "
                f"<code>{html.escape(str(session_id))}</code>",
                f"<b>{html.escape(ui_language.tr('status.retry_cache'))}</b> · "
                f"{html.escape(ui_language.tr('status.prompt'))} "
                f"<code>{html.escape(ui_language.tr('common.yes' if runtime.last_prompt else 'common.no'))}</code> · "
                f"{html.escape(ui_language.tr('status.response'))} "
                f"<code>{html.escape(ui_language.tr('common.yes' if runtime.last_response else 'common.no'))}</code>",
                f"<b>{html.escape(ui_language.tr('status.primers'))}</b> · "
                f"{html.escape(ui_language.tr('status.fyi'))} "
                f"<code>{html.escape(ui_language.tr('status.state.armed' if runtime._pending_session_primer else 'status.state.clear'))}</code> · "
                f"{html.escape(ui_language.tr('status.auto_recall'))} "
                f"<code>{html.escape(ui_language.tr('status.state.armed' if runtime._pending_auto_recall_context else 'status.state.clear'))}</code>",
                f"<b>{html.escape(ui_language.tr('status.bridge_memory'))}</b> · "
                f"<code>{runtime.memory_store.get_stats()['turns']}</code> "
                f"{html.escape(ui_language.tr('status.turns'))} · "
                f"<code>{runtime.memory_store.get_stats()['memories']}</code> "
                f"{html.escape(ui_language.tr('status.memories'))}",
                f"<b>{html.escape(ui_language.tr('status.memory_card'))}</b> · "
                f"<code>{memory_plus['today_chars']}</code> "
                f"{html.escape(ui_language.tr('status.chars'))} · "
                f"<code>{memory_plus['history_days']}</code> "
                f"{html.escape(ui_language.tr('status.archived_days'))} · "
                f"<code>{html.escape(str(memory_plus['state_path']))}</code>",
                f"<b>{html.escape(ui_language.tr('status.handoff_files'))}</b> · "
                f"{html.escape(ui_language.tr('status.recent'))} "
                f"<code>{html.escape(ui_language.tr('common.yes' if runtime.recent_context_path.exists() else 'common.no'))}</code> · "
                f"{html.escape(ui_language.tr('status.handoff'))} "
                f"<code>{html.escape(ui_language.tr('common.yes' if runtime.handoff_path.exists() else 'common.no'))}</code>",
                f"<b>{html.escape(ui_language.tr('status.verbose'))}</b> · "
                f"<code>{html.escape(ui_language.tr('common.on' if runtime._verbose else 'common.off'))}</code>",
                f"<b>{html.escape(ui_language.tr('status.last_switch'))}</b> · "
                f"{html.escape(runtime._format_age(runtime.last_backend_switch_at))}",
            ]
        )
        try:
            from tools.token_tracker import format_status_line, get_summary

            usage_summary = get_summary(runtime.workspace_dir, session_id=runtime.session_id_dt)
            lines.append(
                f"<b>{html.escape(ui_language.tr('status.tokens'))}</b> · "
                f"{html.escape(format_status_line(usage_summary))}"
            )
        except Exception:
            pass
    else:
        lines.append("")
        lines.append(ui_language.tr("status.more"))
    return "\n".join(lines)


async def cmd_status(runtime, update, context) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    detailed = bool(context.args and context.args[0].strip().lower() in {"full", "all", "more"})
    await runtime._reply_text(
        update,
        runtime._build_status_text(detailed=detailed, update=update),
        parse_mode="HTML",
    )
