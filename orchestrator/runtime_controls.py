from __future__ import annotations

from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def _toggle_keyboard(target: str, enabled: bool) -> InlineKeyboardMarkup:
    on_label = "✅ ON" if enabled else "ON"
    off_label = "✅ OFF" if not enabled else "OFF"
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(on_label, callback_data=f"tgl:{target}:on"),
            InlineKeyboardButton(off_label, callback_data=f"tgl:{target}:off"),
        ]]
    )


async def cmd_verbose(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    args = [a.strip().lower() for a in (context.args or []) if a.strip()]
    if args and args[0] in {"on", "true", "1"}:
        runtime._verbose = True
    elif args and args[0] in {"off", "false", "0"}:
        runtime._verbose = False
    else:
        runtime._verbose = not runtime._verbose

    verbose_file = runtime.workspace_dir / ".verbose_off"
    if runtime._verbose:
        verbose_file.unlink(missing_ok=True)
    else:
        verbose_file.touch()

    state = "ON 🔍" if runtime._verbose else "OFF"
    await runtime._reply_text(
        update,
        f"Verbose mode: {state}\n"
        f"{'Long-task placeholders will show engine, elapsed, idle time and output events.' if runtime._verbose else 'Placeholders will show concise status only.'}",
        reply_markup=_toggle_keyboard("verbose", runtime._verbose),
    )


async def cmd_think(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    args = [a.strip().lower() for a in (context.args or []) if a.strip()]
    if args and args[0] in {"on", "true", "1"}:
        runtime._think = True
    elif args and args[0] in {"off", "false", "0"}:
        runtime._think = False
    else:
        runtime._think = not runtime._think

    think_file = runtime.workspace_dir / ".think_off"
    if runtime._think:
        think_file.unlink(missing_ok=True)
    else:
        think_file.touch()

    state = "ON 💭" if runtime._think else "OFF"
    await runtime._reply_text(
        update,
        f"Thinking display: {state}\n"
        f"{'Thinking traces will be sent as permanent italic messages every ~60s during generation.' if runtime._think else 'Thinking traces will not be displayed.'}",
        reply_markup=_toggle_keyboard("think", runtime._think),
    )
