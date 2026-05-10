from __future__ import annotations

from typing import Any


async def cmd_debug(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return

    raw_args = list(context.args or [])
    args = [arg.strip().lower() for arg in raw_args if arg.strip()]

    if args and args[0] in {"on", "off"}:
        enabled = args[0] == "on"
        if runtime.skill_manager:
            _, message = runtime.skill_manager.set_toggle_state(runtime.workspace_dir, "debug", enabled=enabled)
            state = "ON 🔴" if enabled else "OFF"
            await runtime._reply_text(update, f"🐛 Debug mode: {state}\n{message}")
        else:
            await runtime._reply_text(update, "Skill manager not available.")
        return

    if not runtime.skill_manager:
        await runtime._reply_text(update, "Skill system is not configured.")
        return

    skill = runtime.skill_manager.get_skill("debug")
    if skill is None:
        await runtime._reply_text(update, "Unknown skill: debug")
        return

    prompt_text = " ".join(raw_args).strip()
    if not prompt_text:
        await runtime._reply_text(update, "Usage: /debug <prompt> or /debug on|off")
        return

    prompt = runtime.skill_manager.build_prompt_for_skill(skill, prompt_text)
    await runtime._reply_text(update, f"Running skill {skill.id}...")
    await runtime.enqueue_request(
        update.effective_chat.id,
        prompt,
        f"skill:{skill.id}",
        f"Skill {skill.id}",
    )
