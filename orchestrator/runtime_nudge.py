from __future__ import annotations

import html


def parse_nudge_create_args(args_text: str) -> tuple[int, str]:
    raw = (args_text or "").strip()
    parts = raw.split(None, 1)
    if len(parts) < 2:
        raise ValueError("Usage: /nudge <minutes> <exit condition>")
    try:
        minutes = int(parts[0])
    except ValueError as exc:
        raise ValueError("Minutes must be a whole number, e.g. /nudge 5 until done") from exc
    if minutes < 1:
        raise ValueError("Minutes must be at least 1.")
    exit_condition = parts[1].strip()
    if not exit_condition:
        raise ValueError("Exit condition is required.")
    return minutes, exit_condition


def build_nudge_usage_text() -> str:
    return (
        "🫧 <b>Nudge — Idle Continuation Manager</b>\n\n"
        "<code>/nudge &lt;minutes&gt; &lt;exit condition&gt;</code> — nudge when idle\n"
        "<code>/nudge list</code> — list active nudges\n"
        "<code>/nudge stop [id]</code> — stop nudge(s)\n\n"
        "Example:\n"
        "<code>/nudge 1 until the scanning is all done</code>"
    )


def build_nudge_list_text(skill_manager, agent_name: str) -> str:
    jobs = [j for j in skill_manager.list_jobs("nudge", agent_name=agent_name) if j.get("nudge_meta")]
    if not jobs:
        return "No nudge jobs for this agent."
    lines = ["🫧 <b>Nudges</b>\n"]
    for job in jobs:
        meta = job.get("nudge_meta", {})
        status = "🟢 ON" if job.get("enabled") else "🔴 OFF"
        count = int(meta.get("count", 0) or 0)
        max_count = int(meta.get("max", 100) or 100)
        interval = int(job.get("interval_seconds", 0) or 0)
        minutes = max(1, interval // 60) if interval else "?"
        reason = meta.get("stopped_reason", "")
        exit_condition = html.escape(str(job.get("exit_condition") or job.get("note") or "")[:120])
        lines.append(f"<code>{html.escape(job['id'])}</code> [{status}] every {minutes} min ({count}/{max_count})")
        if exit_condition:
            lines.append(f"  until {exit_condition}")
        if reason:
            lines.append(f"  ⚠️ {html.escape(str(reason))}")
    return "\n".join(lines)


def stop_nudges(skill_manager, agent_name: str, stop_arg: str) -> str:
    jobs = [
        j for j in skill_manager.list_jobs("nudge", agent_name=agent_name)
        if j.get("nudge_meta") and j.get("enabled")
    ]
    if not jobs:
        return "No active nudges to stop."
    stopped = []
    for job in jobs:
        if not stop_arg or stop_arg in job["id"]:
            skill_manager.set_job_enabled("nudge", job["id"], enabled=False)
            stopped.append(job["id"])
    if stopped:
        return f"⏹ Stopped nudges: {', '.join(stopped)}"
    return f"No nudge matching '{html.escape(stop_arg)}' found."


async def cmd_nudge(runtime, update, context) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    if not runtime.skill_manager:
        await runtime._reply_text(update, "Skill manager not available.")
        return

    args_text = " ".join(context.args or []).strip()
    raw = (args_text or "").strip()
    if not raw:
        await runtime._reply_text(update, build_nudge_usage_text(), parse_mode="HTML")
        return

    lowered = raw.lower()
    if lowered == "list":
        await runtime._reply_text(
            update,
            build_nudge_list_text(runtime.skill_manager, runtime.name),
            parse_mode="HTML",
        )
        return

    if lowered.startswith("stop"):
        stop_arg = lowered[4:].strip()
        await runtime._reply_text(
            update,
            stop_nudges(runtime.skill_manager, runtime.name, stop_arg),
            parse_mode="HTML",
        )
        return

    try:
        minutes, exit_condition = parse_nudge_create_args(raw)
    except ValueError as exc:
        await runtime._reply_text(update, f"⚠️ {html.escape(str(exc))}", parse_mode="HTML")
        return

    create = getattr(runtime.skill_manager, "create_nudge_job", None)
    if not callable(create):
        await runtime._reply_text(update, "Nudge jobs are not available in this build.")
        return
    job = create(
        agent_name=runtime.name,
        interval_minutes=minutes,
        exit_condition=exit_condition,
    )
    await runtime._reply_text(
        update,
        (
            "🫧 <b>Nudge created</b>\n\n"
            f"Job: <code>{html.escape(job['id'])}</code>\n"
            f"Every: <b>{minutes} min</b> when idle\n"
            f"Exit: {html.escape(exit_condition)}"
        ),
        parse_mode="HTML",
    )
