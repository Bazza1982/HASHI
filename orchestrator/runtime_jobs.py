from __future__ import annotations

import json

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
