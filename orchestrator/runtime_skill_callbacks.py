from __future__ import annotations

import html

from orchestrator import runtime_menu_views


async def handle_skill_callback(runtime, query, data: str) -> bool:
    if not data.startswith("skill:"):
        return False

    if data == "skill:back:menu":
        grouped = runtime._skills_by_type()
        count = sum(len(items) for items in grouped.values())
        await query.edit_message_text(
            runtime_menu_views.skills_menu_text(count=count, agent_name=runtime.name),
            parse_mode="HTML",
            reply_markup=runtime._skill_keyboard(),
        )
        await query.answer()
        return True

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
                runtime_menu_views.skill_detail_text(
                    skill,
                    runtime.workspace_dir,
                    manager=runtime.skill_manager,
                ),
                parse_mode="HTML",
                reply_markup=runtime._skill_action_keyboard(skill),
            )
        await query.answer()
        return True
    if action == "toggle" and rest:
        enabled = rest[0] == "on"
        _, message = runtime.skill_manager.set_toggle_state(runtime.workspace_dir, skill.id, enabled=enabled)
        await query.edit_message_text(
            f"✅ {html.escape(message)}\n\n"
            + runtime_menu_views.skill_detail_text(
                skill,
                runtime.workspace_dir,
                manager=runtime.skill_manager,
            ),
            parse_mode="HTML",
            reply_markup=runtime._skill_action_keyboard(skill),
        )
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
