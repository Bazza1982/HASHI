"""Owner-operated source preparation; live adoption is always a later action."""
from __future__ import annotations

import asyncio
import html
import sys
from pathlib import Path
from typing import Any

from orchestrator import ui_language
from orchestrator.bootstrap_logging import redact_log_text
from orchestrator.command_registry import RuntimeCommand
from orchestrator.command_ui import card_title
from tools.release_update import ReleaseUpdateError, run_staged_source_release


async def hashideploy_command(runtime: Any, update: Any, context: Any) -> None:
    user_id = getattr(getattr(update, "effective_user", None), "id", None)
    checker = getattr(runtime, "_is_authorized_user", None)
    if user_id is None or not callable(checker) or not checker(user_id):
        await runtime._reply_text(update, ui_language.tr("hashideploy.denied"))
        return
    if getattr(context, "args", None):
        await runtime._reply_text(update, ui_language.tr("hashideploy.usage"), parse_mode="HTML")
        return
    if getattr(runtime, "_hashideploy_inflight", False):
        await runtime._reply_text(update, ui_language.tr("hashideploy.busy"))
        return
    config = getattr(runtime, "global_config", None)
    root = getattr(config, "project_root", None)
    home = getattr(config, "bridge_home", None)
    instance = str(getattr(config, "instance_id", "") or "").strip()
    if not root or not home or not instance:
        await runtime._reply_text(update, ui_language.tr("hashideploy.target_missing"))
        return

    runtime._hashideploy_inflight = True
    try:
        await runtime._reply_text(
            update, ui_language.tr("hashideploy.preparing", instance=html.escape(instance)),
            parse_mode="HTML",
        )
        result = await asyncio.to_thread(
            run_staged_source_release,
            code_root=Path(root), bridge_home=Path(home), instance_id=instance,
            runtime_command=[sys.executable, "-B"],
        )
        await runtime._reply_text(
            update,
            card_title("✅", ui_language.tr("hashideploy.ready")) + "\n\n"
            + ui_language.tr(
                "hashideploy.prepared", instance=html.escape(instance),
                commit=html.escape(result["target_commit"][:12]),
            ),
            parse_mode="HTML",
        )
    except ReleaseUpdateError as exc:
        details = exc.details if isinstance(exc.details, dict) else {}
        source = details.get("source") or {}
        reason = html.escape(redact_log_text(str(exc)))
        cause = details.get("cause")
        if cause:
            reason += "\n" + html.escape(redact_log_text(str(cause))[:1200])
        await runtime._reply_text(
            update,
            ui_language.tr(
                "hashideploy.failed", phase=html.escape(exc.phase), reason=reason,
                commit=html.escape(str(source.get("target_commit") or "?")[:12]),
            ),
            parse_mode="HTML",
        )
    finally:
        runtime._hashideploy_inflight = False


COMMANDS = [
    RuntimeCommand(
        name="hashideploy",
        description="Prepare the latest HASHI source without restarting",
        callback=hashideploy_command,
    ),
]
