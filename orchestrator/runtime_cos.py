from __future__ import annotations

from typing import Any


async def cmd_cos(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return

    if runtime.name == "lily":
        await runtime._reply_text(update, "Lily cannot use /cos — that would go in circles 🌸")
        return

    args = [arg.strip().lower() for arg in (context.args or []) if arg.strip()]
    if not args:
        status = "ON ✅" if runtime._cos_enabled else "OFF"
        await runtime._reply_text(update, f"Chief of Staff routing: {status}\nUse /cos on or /cos off to toggle.")
        return

    if args[0] == "on":
        runtime._cos_enabled = True
        (runtime.workspace_dir / ".cos_on").touch()
        await runtime._reply_text(
            update,
            "Chief of Staff routing enabled ✅\nHuman-in-the-loop decisions will be routed to Lily first.",
        )
        return

    if args[0] == "off":
        runtime._cos_enabled = False
        (runtime.workspace_dir / ".cos_on").unlink(missing_ok=True)
        await runtime._reply_text(
            update,
            "Chief of Staff routing disabled.\nDecisions will go directly to the user.",
        )
        return

    await runtime._reply_text(update, "Usage: /cos [on|off]")
