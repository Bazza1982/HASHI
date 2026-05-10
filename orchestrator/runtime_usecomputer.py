from __future__ import annotations

from typing import Any

from orchestrator.usecomputer_mode import (
    build_usecomputer_task_prompt,
    get_usecomputer_examples_text,
    get_usecomputer_status,
    set_usecomputer_mode,
)


async def cmd_usecomputer(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return

    args = [arg.strip() for arg in (context.args or []) if arg.strip()]
    if not args:
        await runtime._reply_text(
            update,
            "Usage:\n"
            "/usecomputer on - enable managed GUI-aware mode\n"
            "/usecomputer off - disable it and clear the managed /sys slot\n"
            "/usecomputer status - show current state\n"
            "/usecomputer examples - show example prompts\n"
            "/usecomputer <task> - run a task with computer-use guidance loaded",
        )
        return

    subcommand = args[0].lower()
    if subcommand == "on":
        await runtime._reply_text(update, set_usecomputer_mode(runtime.sys_prompt_manager, True))
        return
    if subcommand == "off":
        await runtime._reply_text(update, set_usecomputer_mode(runtime.sys_prompt_manager, False))
        return
    if subcommand == "status":
        await runtime._reply_text(update, get_usecomputer_status(runtime.sys_prompt_manager))
        return
    if subcommand == "examples":
        await runtime._reply_text(update, get_usecomputer_examples_text())
        return

    task = " ".join(args).strip()
    set_usecomputer_mode(runtime.sys_prompt_manager, True)
    await runtime._reply_text(update, "Running in /usecomputer mode...")
    await runtime.enqueue_request(
        update.effective_chat.id,
        build_usecomputer_task_prompt(task),
        "usecomputer",
        "Computer-use task",
    )


async def cmd_usercomputer(runtime: Any, update: Any, context: Any) -> None:
    await cmd_usecomputer(runtime, update, context)
