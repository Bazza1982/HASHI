from __future__ import annotations


async def handle_skill_callback(runtime, query, data: str) -> bool:
    if not data.startswith("skill:"):
        return False

    _, action, skill_id, *rest = data.split(":")
    skill = runtime.skill_manager.get_skill(skill_id)
    if skill is None:
        await query.answer("Unknown skill", show_alert=True)
        return True
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
        return True
    if action == "toggle" and rest:
        enabled = rest[0] == "on"
        _, message = runtime.skill_manager.set_toggle_state(runtime.workspace_dir, skill.id, enabled=enabled)
        await query.edit_message_text(message, reply_markup=runtime._skill_action_keyboard(skill))
        await query.answer()
        return True
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
        return True
    if action == "jobs":
        await runtime._render_skill_jobs(query, skill.id)
        await query.answer()
        return True

    return False
