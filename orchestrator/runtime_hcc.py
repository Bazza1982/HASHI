"""Frontend command for the PCM-owned HCC preference (current Agent only)."""
from __future__ import annotations

import asyncio
import html
from typing import Any

from orchestrator import hcc, ui_language
from orchestrator.command_ui import card_title, status_label
from orchestrator.pcm import canonical_agent_md, load_pcm_document
from tools.token_tracker import estimate_tokens


def _status(workspace_dir) -> tuple[bool, int]:
    document = load_pcm_document(canonical_agent_md(workspace_dir), workspace_dir=workspace_dir)
    body = document.hcc or ""
    return hcc.is_hcc_enabled(workspace_dir), estimate_tokens(body) if body else 0


async def cmd_hcc(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    args = [str(arg).strip().casefold() for arg in (context.args or [])]
    if args == ["status"]:
        args = []
    if args and (len(args) != 1 or args[0] not in {"on", "off"}):
        await update.message.reply_text(ui_language.tr("hcc.usage"), parse_mode="HTML")
        return
    notice = ""
    if args:
        try:
            # File locking may wait; do not block the Agent event loop.
            await asyncio.to_thread(hcc.set_hcc_enabled, runtime.workspace_dir, args[0] == "on")
        except (OSError, ValueError, RuntimeError) as exc:
            await update.message.reply_text(
                ui_language.tr("hcc.save_failed", error=html.escape(type(exc).__name__)),
                parse_mode="HTML",
            )
            return
        notice = ui_language.tr("hcc.saved")
    try:
        enabled, tokens = await asyncio.to_thread(_status, runtime.workspace_dir)
        content = ui_language.tr("hcc.content", tokens=tokens) if tokens else ui_language.tr("hcc.empty")
    except (OSError, ValueError) as exc:
        enabled = hcc.is_hcc_enabled(runtime.workspace_dir)
        content = ui_language.tr("hcc.unreadable", error=html.escape(type(exc).__name__))
    lines = [
        card_title("🗂️", "HCC CONTEXT CACHE"),
        ui_language.tr("hcc.status", status=status_label(enabled)),
        content,
        ui_language.tr("hcc.scope"),
        ui_language.tr("hcc.cron_independent"),
    ]
    if notice:
        lines.append(notice)
    lines.append("<code>/hcc on</code> · <code>/hcc off</code>")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")
