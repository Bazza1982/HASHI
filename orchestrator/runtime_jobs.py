from __future__ import annotations

from typing import Any


async def cmd_jobs(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return

    from orchestrator.agent_runtime import _build_jobs_with_buttons

    arg = (context.args[0].strip().lower() if context.args else "")
    if arg == "all":
        filter_agent = None
    elif arg:
        filter_agent = arg
    else:
        filter_agent = runtime.name

    text, markup = _build_jobs_with_buttons(runtime.name, runtime.skill_manager, filter_agent=filter_agent)
    await runtime._reply_text(update, text, parse_mode="HTML", reply_markup=markup)
