from __future__ import annotations

from typing import Any, Mapping

from orchestrator.audit_mode import load_audit_config, visible_audit_criteria
from orchestrator.wrapper_mode import load_wrapper_config, visible_wrapper_slots


def compute_status_string(runtime) -> str:
    if not runtime.backend_ready:
        return "offline"
    if runtime.telegram_connected:
        return "online"
    return "local"


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
            "🧪 Audit",
            f"• Core: {cfg.core_backend} / {cfg.core_model}",
            f"• Auditor: {cfg.audit_backend} / {cfg.audit_model}",
            f"• Delivery: {cfg.delivery}",
            f"• Threshold: {cfg.severity_threshold}",
            f"• Timeout: {cfg.timeout_s:g}s",
            "",
        ]
        if detailed:
            lines.pop()
            criteria = visible_audit_criteria(state.get("audit_criteria"))
            lines.extend(["", "🧪 Audit Criteria:"])
            if criteria:
                for key in sorted(
                    criteria,
                    key=lambda value: (
                        not str(value).isdigit(),
                        int(value) if str(value).isdigit() else str(value),
                    ),
                ):
                    lines.append(f"• {key}: {criteria[key]}")
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
            "🎭 Wrapper",
            f"• Core: {cfg.core_backend} / {cfg.core_model}",
            f"• Wrapper: {cfg.wrapper_backend} / {cfg.wrapper_model}",
            f"• Context window: {cfg.context_window}",
            f"• Slots: {slot_count} configured",
            "",
        ]
        if detailed:
            lines.pop()
            lines.extend(["", "🎭 Wrapper Slots:"])
            if slots:
                for key in sorted(
                    slots,
                    key=lambda value: (
                        not str(value).isdigit(),
                        int(value) if str(value).isdigit() else str(value),
                    ),
                ):
                    lines.append(f"• {key}: {slots[key]}")
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
    tg_status = "✓" if runtime.telegram_connected else "✗"
    wa_status = "✓" if runtime._get_whatsapp_connected() else "✗"
    channel_line = f"Telegram {tg_status} • WhatsApp {wa_status} • Workbench ✓"
    mode_str = getattr(runtime.backend_manager, "agent_mode", "flex")
    try:
        state_snapshot = runtime.backend_manager.get_state_snapshot()
    except Exception:
        state_snapshot = {}
    session_id_short = "none"
    if mode_str == "fixed" and getattr(runtime.backend_manager, "current_backend", None):
        sid = getattr(runtime.backend_manager.current_backend, "_session_id", None) or "none"
        session_id_short = sid[:8] + "…" if sid != "none" and len(sid) > 8 else sid
    lines = [
        f"🧠 {runtime.name}",
        f"🔀 Mode: {mode_str}",
        f"⚙️ Active backend: {runtime.config.active_backend} • {runtime.get_current_model()}",
    ]
    if mode_str == "fixed":
        lines.append(f"🧷 Session: {session_id_short}")
    lines.extend(runtime._format_status_mode_block(mode_str, state_snapshot, detailed))
    lines.extend(
        [
            f"📶 Channels: {channel_line}",
            f"📡 Runtime: {'busy' if runtime.is_generating else 'idle'} • queue {runtime.queue.qsize()} • process {runtime._process_info()}",
            f"🧾 Current: {current_line}",
            f"🧠 Memory: skills {', '.join(active_skills) if active_skills else 'none'} • recall {'ON' if recall_on else 'OFF'} • FYI {'armed' if runtime._pending_session_primer else 'clear'}",
            f"🔔 Proactive: {active_mode} • every {active_interval} • hb {heartbeat_count} • cron {cron_count}",
            f"🩺 Health: {health_line}",
            f"🕒 Activity: last success {runtime._format_age(runtime.last_success_at)} • last activity {runtime._format_age(runtime.last_activity_at)}",
        ]
    )
    if detailed:
        allowed = ", ".join(b["engine"] for b in runtime.config.allowed_backends)
        current_effort = runtime._get_current_effort() or "n/a"

        session_id = "none"
        if mode_str == "fixed" and getattr(runtime.backend_manager, "current_backend", None):
            session_id = getattr(runtime.backend_manager.current_backend, "_session_id", "none") or "none"

        lines.extend(
            [
                "",
                f"📁 Workspace: {runtime.workspace_dir}",
                f"📝 Transcript: {runtime.transcript_log_path.name}",
                f"🚀 Started: {runtime.session_started_at.isoformat(timespec='seconds')}",
                f"🧩 Allowed Backends: {allowed}",
                f"🎛️ Effort: {current_effort}",
                f"⚙️ Mode: {mode_str} • Session ID: {session_id}",
                f"🔁 Retry Cache: prompt {'yes' if runtime.last_prompt else 'no'} • response {'yes' if runtime.last_response else 'no'}",
                f"🧷 Primers: FYI {'armed' if runtime._pending_session_primer else 'clear'} • auto-recall {'armed' if runtime._pending_auto_recall_context else 'clear'}",
                f"📚 Bridge Memory: {runtime.memory_store.get_stats()['turns']} turns • {runtime.memory_store.get_stats()['memories']} memories",
                f"📘 Handoff Files: recent {'yes' if runtime.recent_context_path.exists() else 'no'} • handoff {'yes' if runtime.handoff_path.exists() else 'no'}",
                f"🔍 Verbose: {'ON' if runtime._verbose else 'OFF'}",
                f"💭 Think: {'ON' if runtime._think else 'OFF'}",
                f"🕓 Last Switch: {runtime._format_age(runtime.last_backend_switch_at)}",
            ]
        )
        try:
            from tools.token_tracker import format_status_line, get_summary

            usage_summary = get_summary(runtime.workspace_dir, session_id=runtime.session_id_dt)
            lines.append(f"💰 Tokens: {format_status_line(usage_summary)}")
        except Exception:
            pass
    else:
        lines.append("")
        lines.append("Use /status full for more detail.")
    return "\n".join(lines)


async def cmd_status(runtime, update, context) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    detailed = bool(context.args and context.args[0].strip().lower() in {"full", "all", "more"})
    await runtime._reply_text(update, runtime._build_status_text(detailed=detailed))
