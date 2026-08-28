from __future__ import annotations

import asyncio
import html
from typing import Any

from orchestrator import runtime_menu_views, ui_language
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
            f"<b>{html.escape(ui_language.tr('common.current'))}</b> · "
            f"<b>{html.escape(ui_language.tr('common.unavailable'))}</b>\n\n"
            f"{ui_language.tr('wol.unavailable')}",
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
        ui_language.tr("wol.sending", target=html.escape(arg)),
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
            output = output[:2500] + "\n" + ui_language.tr("wol.truncated")
        lines = [
            ui_language.tr(
                "wol.completed",
                target=html.escape(str(result.get("label") or arg)),
            )
        ]
        if output:
            lines.extend(["", output])
        await runtime._reply_text(update, "\n".join(lines))
        return

    error = result.get("error") or result.get("stderr") or "unknown error"
    lines = [
        ui_language.tr(
            "wol.failed",
            target=html.escape(arg),
            error=html.escape(str(error)),
        )
    ]
    available = result.get("available_targets") or []
    if available:
        lines.append(
            ui_language.tr(
                "wol.available_targets",
                targets=html.escape(", ".join(available)),
            )
        )
    await runtime._reply_text(update, "\n".join(lines), parse_mode="HTML")
