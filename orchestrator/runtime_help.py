from __future__ import annotations

from typing import Any


async def cmd_help(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return

    commands = runtime.get_bot_commands()
    enabled = [command for command in commands if runtime._is_command_allowed(command.command)]
    disabled = sorted({command.command for command in commands if not runtime._is_command_allowed(command.command)})

    lines = [f"Agent {runtime.name} ({getattr(runtime.config, 'type', 'flex')}) Commands", ""]
    for command in enabled:
        lines.append(f"/{command.command} - {command.description}")

    if disabled:
        lines.append("")
        lines.append("Disabled for this agent:")
        lines.append("  " + ", ".join(f"/{name}" for name in disabled))

    await runtime._reply_text(update, "\n".join(lines))
