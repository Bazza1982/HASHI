from __future__ import annotations

from typing import Any

from orchestrator.command_registry import RuntimeCommand
from orchestrator.exp_mode import build_exp_task_prompt, get_exp_usage_text
from telegram import Update


async def cmd_exp(runtime, update: Update, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    task = " ".join(context.args or []).strip()
    if not task:
        await runtime._reply_text(update, get_exp_usage_text())
        return
    prompt = build_exp_task_prompt(task)
    await runtime._reply_text(update, "Running with EXP guidebook...")
    await runtime.enqueue_request(
        update.effective_chat.id,
        prompt,
        "exp",
        "EXP-guided task",
    )


COMMANDS = [
    RuntimeCommand(
        name="exp",
        description="Run a task with the EXP guidebook",
        callback=cmd_exp,
    )
]
