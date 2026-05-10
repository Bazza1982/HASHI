from __future__ import annotations

from typing import Any


async def cmd_loop(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return

    raw = (update.message.text or "").strip()
    parts = raw.split(None, 1)
    args_text = parts[1].strip() if len(parts) > 1 else ""

    if not args_text:
        await runtime._reply_text(
            update,
            "🔄 <b>Loop — Recurring Task Manager</b>\n\n"
            "<code>/loop &lt;task&gt;</code> — create a loop\n"
            "<code>/loop list</code> — list active loops\n"
            "<code>/loop stop [id]</code> — stop loop(s)",
            parse_mode="HTML",
        )
        return

    sub_lower = args_text.lower().strip()
    if sub_lower == "list":
        if not runtime.skill_manager:
            await runtime._reply_text(update, "Skill manager not available.")
            return
        jobs = (
            [("heartbeat", job) for job in runtime.skill_manager.list_jobs("heartbeat", agent_name=runtime.name)]
            + [("cron", job) for job in runtime.skill_manager.list_jobs("cron", agent_name=runtime.name)]
        )
        loops = [(job_kind, job) for job_kind, job in jobs if job.get("loop_meta")]
        if not loops:
            await runtime._reply_text(update, "No active loops for this agent.")
            return
        lines = ["🔄 <b>Loops</b>\n"]
        for job_kind, job in loops:
            meta = job.get("loop_meta", {})
            status = "🟢 ON" if job.get("enabled") else "🔴 OFF"
            count = meta.get("count", 0)
            max_count = meta.get("max", 100)
            reason = meta.get("stopped_reason", "")
            schedule = f"every {job.get('interval_seconds')}s" if job_kind == "heartbeat" else job.get("schedule", "?")
            summary = meta.get("task_summary", job.get("note", ""))[:60]
            lines.append(f"<code>{job['id']}</code> [{status}] [{job_kind}] {schedule} ({count}/{max_count})")
            if summary:
                lines.append(f"  {summary}")
            if reason:
                lines.append(f"  ⚠️ {reason}")
        await runtime._reply_text(update, "\n".join(lines), parse_mode="HTML")
        return

    if sub_lower.startswith("stop"):
        stop_arg = sub_lower[4:].strip()
        if not runtime.skill_manager:
            await runtime._reply_text(update, "Skill manager not available.")
            return
        jobs = (
            [("heartbeat", job) for job in runtime.skill_manager.list_jobs("heartbeat", agent_name=runtime.name)]
            + [("cron", job) for job in runtime.skill_manager.list_jobs("cron", agent_name=runtime.name)]
        )
        loops = [(job_kind, job) for job_kind, job in jobs if job.get("loop_meta") and job.get("enabled")]
        if not loops:
            await runtime._reply_text(update, "No active loops to stop.")
            return
        stopped = []
        for job_kind, job in loops:
            if not stop_arg or stop_arg in job["id"]:
                runtime.skill_manager.set_job_enabled(job_kind, job["id"], enabled=False)
                stopped.append(job["id"])
        if stopped:
            await runtime._reply_text(update, f"⏹ Stopped: {', '.join(stopped)}")
        else:
            await runtime._reply_text(update, f"No loop matching '{stop_arg}' found.")
        return

    tasks_path = str(runtime.skill_manager.tasks_path) if runtime.skill_manager else "tasks.json"
    loop_skill_prompt = (
        "--- SKILL CONTEXT [loop] ---\n"
        "The user wants to create a recurring loop task. Your job is to UNDERSTAND their request "
        "and set up the correct recurring job in tasks.json.\n\n"
        "## What you must figure out from the user's message:\n"
        "1. **WHAT** to do each iteration (the task)\n"
        "2. **HOW OFTEN** (the interval — e.g., every 10 min, every 30 min, hourly)\n"
        "3. **WHEN TO STOP** (the completion condition — e.g., after N times, when all items done, etc.)\n\n"
        "## Job type rule: read this carefully\n"
        "- Use a `heartbeat` for interval-based loops: every N minutes, every N hours, repeated polling, recurring progress checks, retries, watchdogs.\n"
        "- Use a `cron` only for fixed wall-clock times: e.g. every day at 08:00, every Monday at 09:30.\n"
        "- Do NOT use cron expressions like `*/15 * * * *` for loop-style interval jobs.\n"
        "- If the user's request sounds like 'check every 15 min' or 'run every hour until done', this needs `heartbeat`.\n\n"
        "## How to create the recurring job:\n"
        f"1. Read `{tasks_path}` to see the current `heartbeats` and `crons` arrays\n"
        f"2. Generate a unique ID: `{runtime.name}-loop-<6char_hash>`\n"
        "3. Choose the correct structure:\n"
        "For interval-based loops, append a heartbeat entry like this:\n"
        "```json\n"
        "{\n"
        f'  "id": "{runtime.name}-loop-XXXXXX",\n'
        f'  "agent": "{runtime.name}",\n'
        '  "enabled": true,\n'
        '  "interval_seconds": 600,\n'
        '  "action": "enqueue_prompt",\n'
        '  "prompt": "<clear instructions for each iteration — include the task, progress tracking method, and stop condition>",\n'
        '  "note": "Loop: <brief summary>",\n'
        '  "loop_meta": {\n'
        '    "max": 100,\n'
        '    "count": 0,\n'
        '    "created": "<current ISO datetime>",\n'
        '    "task_summary": "<user request summary>"\n'
        '  }\n'
        "}\n"
        "```\n"
        "For fixed-time schedules, append a cron entry like this:\n"
        "```json\n"
        "{\n"
        f'  "id": "{runtime.name}-loop-XXXXXX",\n'
        f'  "agent": "{runtime.name}",\n'
        '  "enabled": true,\n'
        '  "schedule": "<cron expression>",\n'
        '  "action": "enqueue_prompt",\n'
        '  "prompt": "<clear instructions for each iteration — include the task, progress tracking method, and stop condition>",\n'
        '  "note": "Loop: <brief summary>",\n'
        '  "loop_meta": {\n'
        '    "max": 100,\n'
        '    "count": 0,\n'
        '    "created": "<current ISO datetime>",\n'
        '    "task_summary": "<user request summary>"\n'
        '  }\n'
        "}\n"
        "```\n"
        f"4. Save `{tasks_path}`\n\n"
        "## Heartbeat interval examples:\n"
        "- Every 5 min: `interval_seconds = 300`\n"
        "- Every 10 min: `interval_seconds = 600`\n"
        "- Every 15 min: `interval_seconds = 900`\n"
        "- Every hour: `interval_seconds = 3600`\n\n"
        "## Cron examples for fixed clock times only:\n"
        "- Daily at midnight: `0 0 * * *`\n"
        "- Daily at 08:30: `30 8 * * *`\n"
        "- Every Monday at 09:00: `0 9 * * 1`\n\n"
        "## The prompt you write into the job entry must tell the future iteration:\n"
        "- What to do\n"
        "- How to track progress (use workspace files if needed)\n"
        f'- When done: read `{tasks_path}`, find the job by ID in the correct array, set `"enabled": false`, save\n'
        "- If unrecoverable error: disable the same job and report\n\n"
        "## Safety net:\n"
        "- `loop_meta.max` is a hard cap (default 100). The scheduler auto-disables when count exceeds max.\n"
        "- The agent should still stop EARLIER when the task is semantically complete.\n\n"
        "## IMPORTANT:\n"
        "- Do NOT ask the user for clarification. Infer reasonable defaults from their message.\n"
        "- If interval is unclear, default to 10 minutes.\n"
        "- For interval loops, this means a heartbeat unless the user explicitly asks for a fixed wall-clock time.\n"
        "- After creating the job, confirm to the user: the job ID, whether it is a heartbeat or cron, its schedule/interval, and what each iteration will do.\n\n"
        "--- USER REQUEST ---\n"
        f"{args_text}"
    )

    await runtime.enqueue_request(
        chat_id=update.effective_chat.id,
        prompt=loop_skill_prompt,
        source="loop_skill",
        summary="Loop setup",
    )
    await runtime._reply_text(update, "🔄 收到！正在理解任务并设置循环…")
