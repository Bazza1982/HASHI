from __future__ import annotations

from typing import Any

from orchestrator.command_registry import RuntimeCallback
from telegram import Update


async def cmd_skill(runtime, update: Update, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    if not runtime.skill_manager:
        await runtime._reply_text(update, "Skill system is not configured.")
        return

    args = list(context.args or [])
    if not args:
        await runtime._reply_text(update, "Skills", reply_markup=runtime._skill_keyboard())
        return

    sub = args[0].strip().lower()
    if sub == "help":
        grouped = runtime._skills_by_type()
        lines = ["Skills", ""]
        for skill_type in ("action", "toggle", "prompt"):
            entries = grouped.get(skill_type, [])
            if not entries:
                continue
            lines.append(skill_type.upper())
            for skill in entries:
                lines.append(f"- {skill.id}: {skill.description}")
            lines.append("")
        await runtime._reply_text(update, "\n".join(lines).strip())
        return

    skill = runtime.skill_manager.get_skill(sub)
    if skill is None:
        await runtime._reply_text(update, f"Unknown skill: {sub}")
        return

    rest = " ".join(args[1:]).strip()
    if skill.id == "habits" and not rest:
        text, markup = runtime._build_habit_browser_view()
        await runtime._reply_text(update, text, parse_mode="HTML", reply_markup=markup)
        return
    if skill.id in {"cron", "heartbeat"} and not rest:
        await runtime._render_skill_jobs(update, skill.id)
        return

    if skill.type == "toggle":
        if rest.lower() in {"on", "off"}:
            _, message = runtime.skill_manager.set_toggle_state(
                runtime.workspace_dir,
                skill.id,
                enabled=(rest.lower() == "on"),
            )
            await runtime._reply_text(update, message, reply_markup=runtime._skill_action_keyboard(skill))
            return
        await runtime._reply_text(
            update,
            runtime.skill_manager.describe_skill(skill, runtime.workspace_dir),
            reply_markup=runtime._skill_action_keyboard(skill),
        )
        return

    if skill.type == "action":
        _, message = await runtime.skill_manager.run_action_skill(
            skill,
            runtime.workspace_dir,
            args=rest,
            extra_env={
                "BRIDGE_ACTIVE_BACKEND": runtime.config.active_backend,
                "BRIDGE_ACTIVE_MODEL": runtime.get_current_model(),
            },
        )
        await runtime.send_long_message(
            chat_id=update.effective_chat.id,
            text=message,
            request_id=f"skill-{skill.id}",
            purpose="skill-action",
        )
        return

    if not rest:
        await runtime._reply_text(
            update,
            runtime.skill_manager.describe_skill(skill, runtime.workspace_dir),
            reply_markup=runtime._skill_action_keyboard(skill),
        )
        return

    if skill.backend:
        allowed = [b["engine"] for b in runtime.config.allowed_backends]
        if skill.backend not in allowed:
            await runtime._reply_text(
                update,
                f"Skill '{skill.id}' targets {skill.backend}, which is not allowed for this flex agent.",
            )
            return
        if runtime.config.active_backend != skill.backend:
            await runtime._reply_text(update, f"Switching backend to {skill.backend} for skill {skill.id}...")
            success, message = await runtime._switch_backend_mode(
                update.effective_chat.id,
                skill.backend,
                with_context=bool(runtime._get_active_skill_sections()),
            )
            if not success:
                await runtime._send_text(update.effective_chat.id, message)
                return
    prompt = runtime.skill_manager.build_prompt_for_skill(skill, rest)
    await runtime._reply_text(update, f"Running skill {skill.id}...")
    await runtime.enqueue_request(
        update.effective_chat.id,
        prompt,
        f"skill:{skill.id}",
        f"Skill {skill.id}",
    )


async def callback_skill(runtime, update: Update, context: Any) -> None:
    query = update.callback_query
    if not runtime._is_authorized_user(query.from_user.id):
        return
    data = query.data or ""
    if data == "skill:noop:none":
        await query.answer()
        return
    if data.startswith("skill:habits:"):
        parts = data.split(":", 5)
        action = parts[2] if len(parts) > 2 else "list"
        if action == "list":
            offset = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
            text, markup = runtime._build_habit_browser_view(offset=offset)
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
            await query.answer()
            return
        if action == "view":
            habit_id = parts[3] if len(parts) > 3 else ""
            offset = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0
            text, markup = runtime._build_habit_browser_view(offset=offset, selected_habit_id=habit_id)
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
            await query.answer()
            return
        if action == "set":
            habit_id = parts[3] if len(parts) > 3 else ""
            target = parts[4] if len(parts) > 4 else ""
            offset = int(parts[5]) if len(parts) > 5 and parts[5].isdigit() else 0
            ok, message = runtime._set_local_habit_status(habit_id, target)
            text, markup = runtime._build_habit_browser_view(
                offset=offset,
                selected_habit_id=habit_id if ok else None,
                notice=message,
            )
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
            await query.answer(message, show_alert=not ok)
            return
        if action == "queue":
            await query.edit_message_text(runtime._build_habit_governance_view(), parse_mode="HTML")
            await query.answer()
            return
    if data.startswith("skilljob:"):
        _, kind, action, task_id, value = data.split(":", 4)
        if action == "toggle":
            ok, message = runtime.skill_manager.set_job_enabled(kind, task_id, enabled=(value == "on"))
            await query.answer(message, show_alert=not ok)
            await runtime._render_skill_jobs(query, kind)
            return
        if action == "delete":
            ok, message = runtime.skill_manager.delete_job(kind, task_id)
            await query.answer(message, show_alert=not ok)
            await runtime._render_skill_jobs(query, kind)
            return
        if action == "run":
            job = runtime.skill_manager.get_job(kind, task_id)
            if not job:
                await query.answer("Unknown job", show_alert=True)
                return
            await query.answer("Running job now")
            await runtime._run_job_now(job)
            return
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
            return
        if action == "xfer_to":
            target_agent = value
            job = runtime.skill_manager.get_job(kind, task_id)
            if not job:
                await query.answer("Job not found", show_alert=True)
                return
            ok, message, _ = runtime.skill_manager.transfer_job(kind, task_id, target_agent)
            await query.answer(message, show_alert=not ok)
            if ok:
                await query.edit_message_text(
                    f"✅ Job transferred to <b>{target_agent}</b> (disabled — review before enabling).",
                    parse_mode="HTML",
                )
            return
        if action == "xfer_remote":
            parts = value.split(":", 1)
            if len(parts) != 2:
                await query.answer("Invalid target", show_alert=True)
                return
            target_agent, instance_id = parts
            job = runtime.skill_manager.get_job(kind, task_id)
            if not job:
                await query.answer("Job not found", show_alert=True)
                return
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
            return
    if data.startswith("skill:"):
        _, action, skill_id, *rest = data.split(":")
        skill = runtime.skill_manager.get_skill(skill_id)
        if skill is None:
            await query.answer("Unknown skill", show_alert=True)
            return
        if action == "show":
            if skill.id in {"cron", "heartbeat"}:
                await runtime._render_skill_jobs(query, skill.id)
            elif skill.id == "habits":
                text, markup = runtime._build_habit_browser_view()
                await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
            else:
                await query.edit_message_text(
                    runtime.skill_manager.describe_skill(skill, runtime.workspace_dir),
                    reply_markup=runtime._skill_action_keyboard(skill),
                )
            await query.answer()
            return
        if action == "toggle" and rest:
            enabled = rest[0] == "on"
            _, message = runtime.skill_manager.set_toggle_state(runtime.workspace_dir, skill.id, enabled=enabled)
            await query.edit_message_text(message, reply_markup=runtime._skill_action_keyboard(skill))
            await query.answer()
            return
        if action == "run":
            ok, message = await runtime.skill_manager.run_action_skill(
                skill,
                runtime.workspace_dir,
                extra_env={
                    "BRIDGE_ACTIVE_BACKEND": runtime.config.active_backend,
                    "BRIDGE_ACTIVE_MODEL": runtime.get_current_model(),
                },
            )
            await query.answer("Skill executed" if ok else "Skill failed", show_alert=not ok)
            await runtime.send_long_message(
                chat_id=query.message.chat_id,
                text=message,
                request_id=f"skill-{skill.id}",
                purpose="skill-action",
            )
            return
        if action == "jobs":
            await runtime._render_skill_jobs(query, skill.id)
            await query.answer()
            return
    await query.answer()


CALLBACKS = [
    RuntimeCallback(pattern=r"^(skill|skilljob):", callback=callback_skill),
]
