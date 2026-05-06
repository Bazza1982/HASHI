from __future__ import annotations

import json
import logging
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def _build_jobs_with_buttons(agent_name: str, skill_manager, filter_agent: str | None = None):
    """Build combined jobs message text and inline keyboard with run/toggle buttons.

    filter_agent: if set, only show jobs whose 'agent' field matches. None means show all.
    Returns (text: str, markup: InlineKeyboardMarkup | None).
    """
    if skill_manager is None or not hasattr(skill_manager, "tasks_path"):
        return "No task scheduler configured.", None
    try:
        if not skill_manager.tasks_path.exists():
            data = {"heartbeats": [], "crons": []}
        else:
            data = json.loads(skill_manager.tasks_path.read_text(encoding="utf-8"))
    except Exception:
        return "Could not read tasks.json.", None

    if filter_agent:
        title = f"<b>📋 Jobs — {filter_agent}</b>"
    else:
        title = "<b>📋 Jobs — all agents</b>"
    lines = [title]
    buttons: list = []

    all_jobs: list[tuple[str, dict]] = []

    for h in data.get("heartbeats", []):
        if filter_agent and h.get("agent") != filter_agent:
            continue
        interval = h.get("interval_seconds", 0)
        if interval >= 3600:
            interval_s = f"every {interval // 3600}h"
        elif interval >= 60:
            interval_s = f"every {interval // 60}m"
        else:
            interval_s = f"every {interval}s"
        status = "✅" if h.get("enabled", False) else "❌"
        owner = h.get("agent", "?")
        lines.append(f"\n{status} ⏱ <code>{h['id']}</code> — {interval_s} [{owner}]")
        note = h.get("note", "")
        if note and note != h["id"]:
            lines.append(f"   {note}")
        all_jobs.append(("heartbeat", h))

    for c in data.get("crons", []):
        if filter_agent and c.get("agent") != filter_agent:
            continue
        enabled = c.get("enabled", False)
        schedule = c.get("schedule", "")
        parts = schedule.split() if schedule else []
        if len(parts) == 5:
            minute, hour, dom, month, dow = parts
            if dom == "*" and month == "*":
                if dow == "*":
                    if hour.startswith("*/"):
                        interval_h = hour[2:]
                        time_s = f"every {interval_h}h"
                    elif minute.startswith("*/"):
                        interval_m = minute[2:]
                        time_s = f"every {interval_m}m"
                    else:
                        try:
                            time_s = f"{int(hour):02d}:{int(minute):02d}"
                        except ValueError:
                            time_s = schedule
                else:
                    time_s = schedule
            else:
                time_s = schedule
        else:
            time_s = schedule or "??:??"
        freq_label = "every " if "every" in time_s else "daily "
        status = "✅" if enabled else "❌"
        owner = c.get("agent", "?")
        lines.append(f"\n{status} 📅 <code>{c['id']}</code> — {freq_label}{time_s} [{owner}]")
        note = c.get("note", "")
        if note and note != c["id"]:
            lines.append(f"   {note}")
        all_jobs.append(("cron", c))

    for kind, job in all_jobs:
        jid = job["id"]
        enabled = job.get("enabled", False)
        toggle_mode = "off" if enabled else "on"
        toggle_label = "ON" if enabled else "OFF"
        icon = "⏱" if kind == "heartbeat" else "📅"
        short_id = jid[:22]
        buttons.append([InlineKeyboardButton(f"{icon} {short_id}", callback_data="noop")])
        buttons.append([
            InlineKeyboardButton("▶ Run", callback_data=f"skilljob:{kind}:run:{jid}:now"),
            InlineKeyboardButton(toggle_label, callback_data=f"skilljob:{kind}:toggle:{jid}:{toggle_mode}"),
            InlineKeyboardButton("📤 Transfer", callback_data=f"skilljob:{kind}:transfer:{jid}:select"),
            InlineKeyboardButton("🗑 Del", callback_data=f"skilljob:{kind}:delete:{jid}:confirm"),
        ])

    if not all_jobs:
        lines.append("\nNo jobs configured.")

    markup = InlineKeyboardMarkup(buttons) if buttons else None
    return "\n".join(lines), markup


def _build_jobs_text(agent_name: str, skill_manager) -> str:
    """Build a formatted jobs listing for a single agent."""
    if skill_manager is None or not hasattr(skill_manager, "tasks_path"):
        return "No task scheduler configured."
    try:
        if not skill_manager.tasks_path.exists():
            data = {"heartbeats": [], "crons": []}
        else:
            data = json.loads(skill_manager.tasks_path.read_text(encoding="utf-8"))
    except Exception:
        return "Could not read tasks.json."

    lines = [f"<b>Jobs for {agent_name}</b>", ""]
    found = False

    hbs = [h for h in data.get("heartbeats", []) if h.get("agent") == agent_name]
    if hbs:
        lines.append("<b>Heartbeats</b>")
        for h in hbs:
            enabled = "✓" if h.get("enabled") else "✗"
            interval = h.get("interval_seconds", 0)
            if interval >= 3600:
                interval_s = f"{interval // 3600}h"
            elif interval >= 60:
                interval_s = f"{interval // 60}m"
            else:
                interval_s = f"{interval}s"
            note = h.get("note", h.get("id", ""))
            action = h.get("action", "enqueue_prompt")
            lines.append(f"  {enabled} <code>{h['id']}</code>  every {interval_s}")
            if action != "enqueue_prompt":
                lines.append(f"      action: {action}")
            if note and note != h["id"]:
                lines.append(f"      {note}")
        lines.append("")
        found = True

    crons = [c for c in data.get("crons", []) if c.get("agent") == agent_name]
    if crons:
        lines.append("<b>Crons</b>")
        for c in crons:
            enabled = "✓" if c.get("enabled") else "✗"
            time_s = c.get("time", "??:??")
            action = c.get("action", "enqueue_prompt")
            note = c.get("note", c.get("id", ""))
            lines.append(f"  {enabled} <code>{c['id']}</code>  at {time_s}")
            if action != "enqueue_prompt":
                lines.append(f"      action: {action}")
            if note and note != c["id"]:
                lines.append(f"      {note}")
        lines.append("")
        found = True

    if not found:
        lines.append("No jobs configured for this agent.")

    return "\n".join(lines)


def build_job_transfer_keyboard(runtime, kind: str, task_id: str) -> InlineKeyboardMarkup:
    """Build inline keyboard for job transfer: same-instance agents + remote instances."""
    buttons = []

    orchestrator = getattr(runtime, "orchestrator", None)
    local_agents = []
    if orchestrator:
        for rt in getattr(orchestrator, "runtimes", []):
            name = getattr(rt, "name", "")
            if name and name != runtime.name:
                local_agents.append(name)

    if local_agents:
        buttons.append([InlineKeyboardButton("── This instance ──", callback_data="noop")])
        row = []
        for agent in sorted(local_agents):
            row.append(
                InlineKeyboardButton(
                    agent,
                    callback_data=runtime._job_transfer_callback(kind, task_id, agent),
                )
            )
            if len(row) == 3:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)

    try:
        instances_path = runtime.global_config.project_root / "instances.json"
        if instances_path.exists():
            data = json.loads(instances_path.read_text(encoding="utf-8"))
            for inst_id, inst_info in data.get("instances", {}).items():
                if not inst_info.get("active", False):
                    continue
                display = inst_info.get("display_name", inst_id)
                platform = inst_info.get("platform", "")
                if platform == "portable":
                    continue
                if platform == "windows":
                    wsl_root = inst_info.get("wsl_root")
                    agents_path = Path(wsl_root) / "agents.json" if wsl_root else None
                else:
                    root = inst_info.get("root")
                    agents_path = Path(root) / "agents.json" if root else None

                if not agents_path or not agents_path.exists():
                    continue
                try:
                    adata = json.loads(agents_path.read_text(encoding="utf-8-sig"))
                    remote_agents = [a["name"] for a in adata.get("agents", []) if a.get("is_active", True)]
                except Exception:
                    continue

                if not remote_agents:
                    continue

                buttons.append([InlineKeyboardButton(f"── {display} ──", callback_data="noop")])
                row = []
                for agent in sorted(remote_agents):
                    cb = runtime._job_transfer_callback(kind, task_id, agent, instance_id=inst_id)
                    row.append(InlineKeyboardButton(f"{agent}", callback_data=cb))
                    if len(row) == 3:
                        buttons.append(row)
                        row = []
                if row:
                    buttons.append(row)
    except Exception as exc:
        logger = getattr(runtime, "logger", logging.getLogger(__name__))
        logger.warning("Failed to build remote agent transfer buttons: %s", exc)

    buttons.append([InlineKeyboardButton("✖ Cancel", callback_data="noop")])
    return InlineKeyboardMarkup(buttons)


def job_transfer_callback(
    runtime,
    kind: str,
    task_id: str,
    target_agent: str,
    *,
    instance_id: str | None = None,
    max_selections: int = 256,
) -> str:
    store = getattr(runtime, "_job_transfer_selections", None)
    if store is None:
        store = {}
        runtime._job_transfer_selections = store
    if len(store) >= max_selections:
        store.clear()
    token = f"jtx{len(store) + 1:x}"
    store[token] = {
        "kind": kind,
        "task_id": task_id,
        "target_agent": target_agent,
        "instance_id": instance_id,
        "remote": instance_id is not None,
    }
    return f"skilljob:{kind}:xferkey:{token}:go"


async def handle_skill_job_callback(runtime, query, data: str) -> bool:
    if not data.startswith("skilljob:"):
        return False

    _, kind, action, task_id, value = data.split(":", 4)
    if action == "toggle":
        ok, message = runtime.skill_manager.set_job_enabled(kind, task_id, enabled=(value == "on"))
        await query.answer(message, show_alert=not ok)
        await runtime._render_skill_jobs(query, kind)
        return True
    if action == "delete":
        ok, message = runtime.skill_manager.delete_job(kind, task_id)
        await query.answer(message, show_alert=not ok)
        await runtime._render_skill_jobs(query, kind)
        return True
    if action == "run":
        job = runtime.skill_manager.get_job(kind, task_id)
        if not job:
            await query.answer("Unknown job", show_alert=True)
            return True
        await query.answer("Running job now")
        await runtime._run_job_now(job)
        return True
    if action == "transfer":
        markup = runtime._build_job_transfer_keyboard(kind, task_id)
        job = runtime.skill_manager.get_job(kind, task_id)
        job_label = (job.get("note") or task_id) if job else task_id
        await query.edit_message_text(
            f"📤 <b>Transfer job</b>\n<code>{job_label[:60]}</code>\n\nSelect target agent:",
            parse_mode="HTML",
            reply_markup=markup,
        )
        await query.answer()
        return True
    if action == "xfer_to":
        target_agent = value
        job = runtime.skill_manager.get_job(kind, task_id)
        if not job:
            await query.answer("Job not found", show_alert=True)
            return True
        ok, message, _ = runtime.skill_manager.transfer_job(kind, task_id, target_agent)
        await query.answer(message, show_alert=not ok)
        if ok:
            await query.edit_message_text(
                f"✅ Job transferred to <b>{target_agent}</b> (disabled — review before enabling).",
                parse_mode="HTML",
            )
        return True
    if action == "xfer_remote":
        parts = value.split(":", 1)
        if len(parts) != 2:
            await query.answer("Invalid target", show_alert=True)
            return True
        target_agent, instance_id = parts
        job = runtime.skill_manager.get_job(kind, task_id)
        if not job:
            await query.answer("Job not found", show_alert=True)
            return True
        await query.answer("Sending to remote instance…")
        ok, msg = await runtime._transfer_job_remote(kind, job, target_agent, instance_id)
        if ok:
            runtime.skill_manager.set_job_enabled(kind, task_id, enabled=False)
            await query.edit_message_text(
                f"✅ Job transferred to <b>{target_agent}@{instance_id}</b> (original disabled).",
                parse_mode="HTML",
            )
        else:
            await query.edit_message_text(f"❌ Transfer failed: {msg}")
        return True
    if action == "xferkey":
        selection = getattr(runtime, "_job_transfer_selections", {}).get(task_id)
        if not selection:
            await query.answer("Transfer selection expired. Open /jobs and try again.", show_alert=True)
            return True
        target_kind = selection["kind"]
        target_task_id = selection["task_id"]
        target_agent = selection["target_agent"]
        job = runtime.skill_manager.get_job(target_kind, target_task_id)
        if not job:
            await query.answer("Job not found", show_alert=True)
            return True
        if selection.get("remote"):
            instance_id = selection["instance_id"]
            await query.answer("Sending to remote instance…")
            ok, msg = await runtime._transfer_job_remote(target_kind, job, target_agent, instance_id)
            if ok:
                runtime.skill_manager.set_job_enabled(target_kind, target_task_id, enabled=False)
                await query.edit_message_text(
                    f"✅ Job transferred to <b>{target_agent}@{instance_id}</b> (original disabled).",
                    parse_mode="HTML",
                )
            else:
                await query.edit_message_text(f"❌ Transfer failed: {msg}")
            return True
        ok, message, _ = runtime.skill_manager.transfer_job(target_kind, target_task_id, target_agent)
        await query.answer(message, show_alert=not ok)
        if ok:
            await query.edit_message_text(
                f"✅ Job transferred to <b>{target_agent}</b> (disabled — review before enabling).",
                parse_mode="HTML",
            )
        return True

    return False
