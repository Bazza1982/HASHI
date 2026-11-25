"""Owner-operated source preparation; live adoption is always a later action."""
from __future__ import annotations

import asyncio
import html
import logging
import sys
from pathlib import Path
from typing import Any

from orchestrator import ui_language
from orchestrator.bootstrap_logging import redact_log_text
from orchestrator.command_registry import RuntimeCommand
from orchestrator.command_ui import card_title
from tools.release_update import ReleaseUpdateError, run_staged_source_release

_LOG = logging.getLogger(__name__)

_PHASE_COPY = {
    "preflight": "源码检查未通过",
    "fetch": "拉取最新源码失败",
    "build": "构建失败",
    "test": "验证未通过",
    "record": "保存更新记录失败",
    "prepare": "准备更新失败",
    "adopt": "启用新版本失败",
}


def _preparation_done(runtime: Any, task: asyncio.Task[dict[str, Any]]) -> None:
    if getattr(runtime, "_hashideploy_inflight", None) is task:
        runtime._hashideploy_inflight = None
    if task.cancelled():
        return
    try:
        result = task.result()
    except Exception as exc:
        details = exc.to_payload() if isinstance(exc, ReleaseUpdateError) else str(exc)
        _LOG.warning("Source preparation failed: %s", redact_log_text(str(details)))
    else:
        _LOG.info(
            "Source preparation completed: commit=%s receipt=%s",
            result.get("target_commit"), redact_log_text(str(result.get("receipt_path", ""))),
        )


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

    # The thread outlives command/notification cancellation. Its task owns the
    # busy state and observes the final result even when the caller has gone.
    preparation = asyncio.create_task(asyncio.to_thread(
        run_staged_source_release,
        code_root=Path(root), bridge_home=Path(home), instance_id=instance,
        runtime_command=[sys.executable, "-B"],
    ))
    runtime._hashideploy_inflight = preparation
    preparation.add_done_callback(lambda task: _preparation_done(runtime, task))
    try:
        await runtime._reply_text(
            update, ui_language.tr("hashideploy.preparing", instance=html.escape(instance)),
            parse_mode="HTML",
        )
        result = await asyncio.shield(preparation)
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
        phase = str(getattr(exc, "phase", "") or "").strip()
        reason = _PHASE_COPY.get(phase, "更新准备失败")
        # Raw provider/build text stays in the log; the chat surface gets one
        # actionable Chinese sentence and the target version.
        _LOG.warning("Source preparation failed (%s): %s", phase or "unknown", redact_log_text(str(exc)))
        await runtime._reply_text(
            update,
            ui_language.tr(
                "hashideploy.failed", reason=html.escape(reason),
                commit=html.escape(str(source.get("target_commit") or "?")[:12]),
            ),
            parse_mode="HTML",
        )


COMMANDS = [
    RuntimeCommand(
        name="hashideploy",
        description="Prepare the latest HASHI source without restarting",
        callback=hashideploy_command,
    ),
]
