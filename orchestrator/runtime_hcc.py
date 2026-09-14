"""Slash-command surface for HASHI Context Cache."""
from __future__ import annotations

from orchestrator.hcc import is_hcc_enabled, set_hcc_enabled


async def cmd_hcc(runtime, update, context):
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    arg = " ".join(context.args).strip().casefold() if context.args else ""
    workspace = runtime.config.workspace_dir
    if not arg or arg == "status":
        enabled = is_hcc_enabled(workspace)
        await runtime._reply_text(update, f"HCC is {'ON' if enabled else 'OFF'}.")
        return
    if arg not in {"on", "off"}:
        await runtime._reply_text(update, "Usage: /hcc [on|off]")
        return
    try:
        enabled = set_hcc_enabled(workspace, arg == "on")
    except (OSError, ValueError, RuntimeError):
        await runtime._reply_text(update, "HCC setting could not be saved safely; the previous setting was kept.")
        return
    await runtime._reply_text(update, f"HCC is now {'ON' if enabled else 'OFF'}.")
