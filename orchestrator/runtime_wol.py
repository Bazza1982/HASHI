from __future__ import annotations

import asyncio
import html
from typing import Any

from orchestrator import runtime_menu_views
from orchestrator.command_ui import card_title
from orchestrator.private_wol import (
    describe_wol_targets,
    private_wol_available,
    run_private_wol,
)


async def cmd_wol(runtime: Any, update: Any, context: Any) -> None:
    """Run the optional instance-aware local Wake-on-LAN adapter."""
    if not runtime._is_authorized_user(update.effective_user.id):
        return

    project_root = runtime.global_config.project_root
    instance_id = getattr(runtime.global_config, "instance_id", None)
    if not private_wol_available(project_root, instance_id):
        await runtime._reply_text(
            update,
            f"{card_title('🪄', 'Wake-on-LAN')}\n\n"
            "<b>Current</b> · <b>UNAVAILABLE</b>\n\n"
            "Wake-on-LAN is not configured for this instance.",
            parse_mode="HTML",
        )
        return

    arg = context.args[0].strip().lower() if context.args else ""
    if not arg or arg in {"list", "status", "help"}:
        targets = describe_wol_targets(project_root)
        await runtime._reply_text(
            update,
            runtime_menu_views.wol_targets_text(targets, instance_id=instance_id),
            parse_mode="HTML",
        )
        return

    await runtime._reply_text(
        update,
        f"🪄 Sending Wake-on-LAN packet for <code>{html.escape(arg)}</code>…",
        parse_mode="HTML",
    )
    result = await asyncio.to_thread(
        run_private_wol,
        project_root,
        arg,
        configured_instance_id=instance_id,
    )
    if result.get("ok"):
        output = str(result.get("stdout") or "").strip()
        if len(output) > 2500:
            output = output[:2500] + "\n...[truncated]"
        lines = [f"✅ WoL completed for {result.get('label') or arg}."]
        if output:
            lines.extend(["", output])
        await runtime._reply_text(update, "\n".join(lines))
        return

    error = result.get("error") or result.get("stderr") or "unknown error"
    lines = [f"❌ WoL failed for {arg}: {error}"]
    available = result.get("available_targets") or []
    if available:
        lines.append(f"Available targets: {', '.join(available)}")
    await runtime._reply_text(update, "\n".join(lines))
