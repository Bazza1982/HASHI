from __future__ import annotations

from typing import Any


async def cmd_sys(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return

    args = [arg.strip() for arg in (context.args or []) if arg.strip()]
    manager = runtime.sys_prompt_manager

    if not args:
        await update.message.reply_text(manager.display_all(), parse_mode="Markdown")
        return

    if args[0].lower() == "output":
        slot = args[1] if len(args) > 1 else ""
        if slot not in manager.SLOTS:
            await update.message.reply_text("Usage: /sys output <1-10>")
            return
        text = manager._slot(slot).get("text", "")
        await update.message.reply_text(text if text else "(empty)", parse_mode=None)
        return

    slot = args[0]
    if slot not in manager.SLOTS:
        await update.message.reply_text(f"Invalid slot '{slot}'. Use 1-10.")
        return

    if len(args) == 1:
        await update.message.reply_text(manager.display_slot(slot))
        return

    subcommand = args[1].lower()
    if subcommand == "on":
        await update.message.reply_text(manager.activate(slot))
        return
    if subcommand == "off":
        await update.message.reply_text(manager.deactivate(slot))
        return
    if subcommand == "delete":
        await update.message.reply_text(manager.delete(slot))
        return
    if subcommand == "save":
        text = " ".join(args[2:])
        if not text:
            await update.message.reply_text("Usage: /sys <slot> save <message>")
            return
        await update.message.reply_text(manager.save(slot, text))
        return
    if subcommand == "replace":
        text = " ".join(args[2:])
        if not text:
            await update.message.reply_text("Usage: /sys <slot> replace <message>")
            return
        await update.message.reply_text(manager.replace(slot, text))
        return

    await update.message.reply_text(
        "Usage:\n/sys - show all slots\n/sys <n> - show slot\n"
        "/sys <n> on|off|delete\n/sys <n> save <msg>\n/sys <n> replace <msg>\n"
        "/sys output <n> - return raw content of slot"
    )
