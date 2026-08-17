from __future__ import annotations

from orchestrator import runtime_menu_views


async def handle_skill_callback(runtime, query, data: str) -> bool:
    if not data.startswith("skill:"):
        return False

    if data == "skill:back:menu":
        count = len(runtime.skill_manager.list_skills())
        await query.edit_message_text(
            runtime_menu_views.skills_menu_text(count=count, agent_name=runtime.name),
            parse_mode="HTML",
            reply_markup=runtime._skill_keyboard(),
        )
        await query.answer()
        return True

    _, action, skill_id, *rest = data.split(":")
    if action in {"toggle", "run", "jobs"}:
        await query.answer(
            "This legacy Skill action is disabled. Use /jobs or the runtime command.",
            show_alert=True,
        )
        return True
    skill = runtime.skill_manager.get_skill(skill_id)
    if skill is None:
        await query.answer("Unknown skill", show_alert=True)
        return True
    if action == "show":
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
    return False
