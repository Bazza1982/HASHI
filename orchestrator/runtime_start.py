from __future__ import annotations

from typing import Any

from orchestrator.command_registry import RuntimeCallback


async def cmd_start(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return

    orchestrator = getattr(runtime, "orchestrator", None)
    if orchestrator is None:
        await runtime._reply_text(update, "Dynamic lifecycle control is unavailable.")
        return

    arg = " ".join(context.args).strip().lower() if context.args else ""
    if arg == "all":
        names = orchestrator.get_startable_agent_names(exclude_name=runtime.name)
        if not names:
            await runtime._reply_text(update, "All agents are running.")
            return
        lines = []
        for name in names:
            _ok, message = await orchestrator.start_agent(name)
            lines.append(message)
        await runtime._reply_text(update, "\n".join(lines))
        return

    keyboard = runtime._startable_agent_keyboard()
    if keyboard is None:
        await runtime._reply_text(update, "All agents are running.")
        return

    await runtime._reply_text(update, "Start another agent:", reply_markup=keyboard)


async def callback_start_agent(runtime: Any, update: Any, context: Any) -> None:
    query = update.callback_query
    if not runtime._is_authorized_user(query.from_user.id):
        return

    orchestrator = getattr(runtime, "orchestrator", None)
    if orchestrator is None:
        await query.answer("Lifecycle control unavailable", show_alert=True)
        return

    _, agent_name = (query.data or "").split(":", 1)
    if agent_name == "__all__":
        await query.answer("Starting all agents...")
        names = orchestrator.get_startable_agent_names(exclude_name=runtime.name)
        lines = []
        for name in names:
            _ok, message = await orchestrator.start_agent(name)
            lines.append(message)
        result_text = "\n".join(lines) if lines else "All agents are already running."
        await query.edit_message_text(result_text)
        return

    await query.answer(f"Starting {agent_name}...")
    _ok, message = await orchestrator.start_agent(agent_name)
    await query.edit_message_text(message, reply_markup=runtime._startable_agent_keyboard())


CALLBACKS = [
    RuntimeCallback(pattern=r"^startagent:", callback=callback_start_agent),
]
