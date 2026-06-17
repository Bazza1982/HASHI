from __future__ import annotations

import html

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from orchestrator.runtime_jobs import mint_callback_token, resolve_callback_token


def _max_label(value: int) -> str:
    return "∞" if int(value or 0) <= 0 else str(int(value))


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


def build_nudge_with_buttons(skill_manager, agent_name: str, runtime=None):
    """Build nudge panel with inline control buttons, jobs list, and creation hint.

    Returns (text: str, markup: InlineKeyboardMarkup | None).
    """
    if not skill_manager:
        return "Skill manager not available.", None

    jobs = [j for j in skill_manager.list_jobs("nudge", agent_name=agent_name) if j.get("nudge_meta")]

    lines = ["🫧 <b>Nudge — Idle Continuation Manager</b>"]
    buttons: list = []

    if not jobs:
        lines.append("\nNo nudge jobs for this agent.")
        lines.append("\n<i>Create one: <code>/nudge &lt;minutes&gt; &lt;exit condition&gt;</code></i>")
        lines.append("<i>e.g. <code>/nudge 5 until the scan is done</code></i>")
        return "\n".join(lines), None

    for job in jobs:
        meta = job.get("nudge_meta", {})
        enabled = job.get("enabled", False)
        status = "🟢" if enabled else "🔴"
        count = int(meta.get("count", 0) or 0)
        max_count = int(meta.get("max", 0) or 0)
        max_label = "∞" if max_count <= 0 else str(max_count)
        interval = int(job.get("interval_seconds", 0) or 0)
        minutes = max(1, interval // 60) if interval else "?"
        reason = meta.get("stopped_reason", "")
        exit_condition = html.escape(str(job.get("exit_condition") or job.get("note") or "")[:120])
        jid = job["id"]
        short_id = jid[:24]

        lines.append(f"\n{status} <code>{html.escape(jid)}</code>")
        lines.append(f"   every {minutes} min · fired {count}/{max_label}")
        if exit_condition:
            lines.append(f"   until: {exit_condition}")
        if reason:
            lines.append(f"   ⚠️ {html.escape(str(reason))}")

        toggle_mode = "off" if enabled else "on"
        toggle_label = "⏸ Pause" if enabled else "▶ Resume"
        if runtime is None:
            trigger_callback = "noop"
            toggle_callback = "noop"
            delete_callback = "noop"
            max_plus_callback = "noop"
            max_minus_callback = "noop"
            max_unlimited_callback = "noop"
        else:
            trigger_token = mint_callback_token(runtime, "nudgejob_action", {"task_id": jid, "action": "trigger"}, prefix="nj")
            toggle_token = mint_callback_token(
                runtime,
                "nudgejob_action",
                {"task_id": jid, "action": "toggle", "value": toggle_mode},
                prefix="nj",
            )
            delete_token = mint_callback_token(runtime, "nudgejob_action", {"task_id": jid, "action": "delete"}, prefix="nj")
            max_plus_token = mint_callback_token(
                runtime,
                "nudgejob_action",
                {"task_id": jid, "action": "max_delta", "value": "100"},
                prefix="nj",
            )
            max_minus_token = mint_callback_token(
                runtime,
                "nudgejob_action",
                {"task_id": jid, "action": "max_delta", "value": "-100"},
                prefix="nj",
            )
            max_unlimited_token = mint_callback_token(
                runtime,
                "nudgejob_action",
                {"task_id": jid, "action": "max_set", "value": "0"},
                prefix="nj",
            )
            trigger_callback = f"nudgejob:key:{trigger_token}:trigger"
            toggle_callback = f"nudgejob:key:{toggle_token}:toggle"
            delete_callback = f"nudgejob:key:{delete_token}:delete"
            max_plus_callback = f"nudgejob:key:{max_plus_token}:max_delta"
            max_minus_callback = f"nudgejob:key:{max_minus_token}:max_delta"
            max_unlimited_callback = f"nudgejob:key:{max_unlimited_token}:max_set"

        buttons.append([InlineKeyboardButton(f"🫧 {short_id}", callback_data="noop")])
        buttons.append([
            InlineKeyboardButton("⚡ Trigger", callback_data=trigger_callback),
            InlineKeyboardButton(toggle_label, callback_data=toggle_callback),
            InlineKeyboardButton("🗑 Delete", callback_data=delete_callback),
        ])
        buttons.append([
            InlineKeyboardButton("Max -100", callback_data=max_minus_callback),
            InlineKeyboardButton("Max +100", callback_data=max_plus_callback),
            InlineKeyboardButton("Max ∞", callback_data=max_unlimited_callback),
        ])

    lines.append("\n<i>Add: <code>/nudge &lt;minutes&gt; &lt;exit condition&gt;</code></i>")
    lines.append("<i>Adjust max: <code>/nudge max &lt;id&gt; +100</code> or <code>/nudge max &lt;id&gt; unlimited</code></i>")
    markup = InlineKeyboardMarkup(buttons) if buttons else None
    return "\n".join(lines), markup


def build_nudge_list_text(skill_manager, agent_name: str) -> str:
    text, _ = build_nudge_with_buttons(skill_manager, agent_name)
    return text


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


async def handle_nudge_callback(runtime, query, data: str) -> bool:
    """Handle nudgejob: callback_data from inline buttons."""
    if not data.startswith("nudgejob:"):
        return False

    parts = data.split(":", 3)
    if len(parts) != 4:
        await query.answer("Malformed nudge callback.", show_alert=True)
        return True

    _, action, task_id, value = parts
    if action == "key":
        selection = resolve_callback_token(runtime, "nudgejob_action", task_id)
        if not selection:
            await query.answer("This nudge action expired. Open /nudge again.", show_alert=True)
            return True
        if selection.get("action") != value:
            await query.answer("Invalid nudge action. Open /nudge again.", show_alert=True)
            return True
        task_id = selection["task_id"]
        action = selection["action"]
        value = str(selection.get("value", ""))

    if action == "toggle":
        enabled = value == "on"
        ok, message = runtime.skill_manager.set_job_enabled("nudge", task_id, enabled=enabled)
        await query.answer(message, show_alert=not ok)
        await _refresh_nudge_view(runtime, query)
        return True

    if action == "delete":
        ok, message = runtime.skill_manager.delete_job("nudge", task_id)
        await query.answer(message, show_alert=not ok)
        await _refresh_nudge_view(runtime, query)
        return True

    if action == "max_delta":
        try:
            delta = int(value)
        except ValueError:
            await query.answer("Invalid max adjustment.", show_alert=True)
            return True
        ok, message = runtime.skill_manager.adjust_nudge_max(task_id, delta)
        await query.answer(message, show_alert=not ok)
        await _refresh_nudge_view(runtime, query)
        return True

    if action == "max_set":
        try:
            max_value = int(value)
        except ValueError:
            await query.answer("Invalid max value.", show_alert=True)
            return True
        ok, message = runtime.skill_manager.set_nudge_max(task_id, max_value)
        await query.answer(message, show_alert=not ok)
        await _refresh_nudge_view(runtime, query)
        return True

    if action == "trigger":
        job = runtime.skill_manager.get_job("nudge", task_id)
        if not job:
            await query.answer("Nudge job not found.", show_alert=True)
            return True
        await query.answer("Triggering nudge now…")
        prompt = job.get("prompt", "SYSTEM: Idle nudge continuation.")
        message = getattr(query, "message", None)
        chat_id = getattr(message, "chat_id", None)
        if chat_id is None:
            chat_id = runtime._primary_chat_id()
        await runtime.enqueue_request(
            chat_id=chat_id,
            prompt=prompt,
            source="scheduler",
            summary=f"Nudge Manual Trigger [{task_id}]",
        )
        return True

    await query.answer()
    return True


async def _refresh_nudge_view(runtime, query):
    text, markup = build_nudge_with_buttons(runtime.skill_manager, runtime.name, runtime=runtime)
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)


def _resolve_nudge_selector(skill_manager, agent_name: str, selector: str) -> tuple[dict | None, str | None]:
    selector = (selector or "").strip()
    jobs = [job for job in skill_manager.list_jobs("nudge", agent_name=agent_name) if job.get("nudge_meta")]
    if not jobs:
        return None, "No nudge jobs for this agent."
    if not selector:
        if len(jobs) == 1:
            return jobs[0], None
        return None, "Multiple nudges exist. Specify a job id fragment."
    matches = [job for job in jobs if selector in str(job.get("id", ""))]
    if len(matches) == 1:
        return matches[0], None
    if not matches:
        return None, f"No nudge matching '{html.escape(selector)}' found."
    return None, f"Multiple nudges match '{html.escape(selector)}'. Use a longer id fragment."


def _parse_nudge_max_value(raw_value: str) -> tuple[str, int]:
    value = (raw_value or "").strip().lower()
    if value in {"∞", "inf", "infinite", "unlimited", "none"}:
        return "set", 0
    if value.startswith(("+", "-")):
        return "delta", int(value)
    return "set", max(0, int(value))


def set_nudge_max_from_command(skill_manager, agent_name: str, args_text: str) -> str:
    raw = (args_text or "").strip()
    parts = raw.split()
    if not parts:
        return "Usage: /nudge max <id> <+100|-100|number|unlimited>"
    if len(parts) == 1:
        selector = ""
        raw_value = parts[0]
    else:
        selector = parts[0]
        raw_value = parts[1]
    try:
        mode, value = _parse_nudge_max_value(raw_value)
    except ValueError:
        return "Max must be +100, -100, a whole number, or unlimited."
    job, error = _resolve_nudge_selector(skill_manager, agent_name, selector)
    if error:
        return error
    if not job:
        return "Nudge job not found."
    if mode == "delta":
        ok, message = skill_manager.adjust_nudge_max(job["id"], value)
    else:
        ok, message = skill_manager.set_nudge_max(job["id"], value)
    if not ok:
        return message
    updated = skill_manager.get_job("nudge", job["id"]) or job
    meta = updated.get("nudge_meta", {})
    return f"{message} Current fired/max: {int(meta.get('count', 0) or 0)}/{_max_label(int(meta.get('max', 0) or 0))}."


async def handle_nudge_command(runtime, update, args_text: str) -> None:
    if not runtime.skill_manager:
        await runtime._reply_text(update, "Skill manager not available.")
        return

    raw = (args_text or "").strip()
    if not raw or raw.lower() == "list":
        text, markup = build_nudge_with_buttons(runtime.skill_manager, runtime.name, runtime=runtime)
        await runtime._reply_text(update, text, parse_mode="HTML", reply_markup=markup)
        return

    lowered = raw.lower()

    if lowered.startswith("stop"):
        stop_arg = lowered[4:].strip()
        result = stop_nudges(runtime.skill_manager, runtime.name, stop_arg)
        text, markup = build_nudge_with_buttons(runtime.skill_manager, runtime.name, runtime=runtime)
        await runtime._reply_text(update, result + "\n\n" + text, parse_mode="HTML", reply_markup=markup)
        return

    if lowered.startswith("max"):
        result = set_nudge_max_from_command(runtime.skill_manager, runtime.name, raw[3:].strip())
        text, markup = build_nudge_with_buttons(runtime.skill_manager, runtime.name, runtime=runtime)
        await runtime._reply_text(update, result + "\n\n" + text, parse_mode="HTML", reply_markup=markup)
        return

    try:
        minutes, exit_condition = parse_nudge_create_args(raw)
    except ValueError as exc:
        await runtime._reply_text(update, f"⚠️ {html.escape(str(exc))}", parse_mode="HTML")
        return

    job = runtime.skill_manager.create_nudge_job(
        agent_name=runtime.name,
        interval_minutes=minutes,
        exit_condition=exit_condition,
    )
    created_text = (
        "🫧 <b>Nudge created</b>\n\n"
        f"Job: <code>{html.escape(job['id'])}</code>\n"
        f"Every: <b>{minutes} min</b> when idle\n"
        f"Exit: {html.escape(exit_condition)}\n"
    )
    list_text, markup = build_nudge_with_buttons(runtime.skill_manager, runtime.name, runtime=runtime)
    await runtime._reply_text(
        update,
        created_text + "\n" + list_text,
        parse_mode="HTML",
        reply_markup=markup,
    )
