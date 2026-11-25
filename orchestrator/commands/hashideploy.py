"""Owner-operated source preparation; live adoption is always a later action."""
from __future__ import annotations

import asyncio
import html
import logging
import re
import sys
from pathlib import Path
from typing import Any

from orchestrator import ui_language
from orchestrator.bootstrap_logging import redact_log_text
from orchestrator.command_registry import RuntimeCommand
from orchestrator.command_ui import card_title
from tools.release_update import (
    ReleaseUpdateError,
    release_failure,
    run_staged_source_release,
)

_LOG = logging.getLogger(__name__)

# Each user-visible class covers the real point of failure, not an internal
# phase name.  The source line below the message always states whether the
# checkout had already moved when the failure happened.
_ERROR_KEYS = {
    "instance_source": "hashideploy.error.instance_source",
    "local_changes": "hashideploy.error.local_changes",
    "untracked_only": "hashideploy.error.untracked_only",
    "fetch_failed": "hashideploy.error.fetch",
    "not_fast_forward": "hashideploy.error.not_fast_forward",
    "protected_gate": "hashideploy.error.protected_gate",
    "runtime_unavailable": "hashideploy.error.stage_runtime",
    "qualification_failed": "hashideploy.error.stage_runtime",
    "commit_mismatch": "hashideploy.error.commit_mismatch",
    "receipt_failed": "hashideploy.error.receipt",
    "state_io": "hashideploy.error.receipt",
    "stash_failed": "hashideploy.error.force_recovery",
    "stash_incomplete": "hashideploy.error.force_recovery",
    "backup_failed": "hashideploy.error.force_recovery",
    "checkout_failed": "hashideploy.error.force_recovery",
    "internal": "hashideploy.error.internal",
}

_MAX_DETAIL = 240


def _clip(value: Any, limit: int = _MAX_DETAIL) -> str:
    text = " ".join(redact_log_text(str(value or "")).split())
    return html.escape(text[:limit], quote=False)


def _escape(value: Any) -> str:
    return html.escape(str(value or ""), quote=False)


def _details(exc: ReleaseUpdateError) -> dict[str, Any]:
    return dict(exc.details) if isinstance(exc.details, dict) else {}


def _clean_status(value: Any) -> str:
    """Drop porcelain prefixes and collapse a tool error to one readable line."""

    lines = []
    for raw in str(value or "").splitlines():
        line = raw.rstrip()
        if not line:
            continue
        if len(line) >= 3 and line[2] == " ":
            line = line[3:]
        elif len(line) >= 2 and line[1] == " ":
            line = line[2:]
        lines.append(line)
    return _clip(" / ".join(lines[:3]))


def _path_list(values: Any, limit: int = 10) -> str:
    paths = [str(item) for item in values or [] if str(item)]
    text = ", ".join(_escape(item) for item in paths[:limit])
    if len(paths) > limit:
        text += " …"
    return text


def _failure_detail(exc: ReleaseUpdateError, failure: dict[str, Any]) -> str:
    details = _details(exc)
    code = str(failure.get("code") or "")
    if code == "instance_source":
        return _clip(failure.get("root") or exc)
    if code == "stash_incomplete":
        return _path_list(failure.get("dirty_paths") or failure.get("untracked_paths"))
    if code in {"fetch_failed", "stash_failed",
                "backup_failed", "checkout_failed"}:
        return _clean_status(details.get("status") or details.get("cause") or exc)
    if code in {"runtime_unavailable", "qualification_failed", "internal"}:
        cause = details.get("cause")
        if isinstance(cause, dict):
            cause = cause.get("status") or cause.get("cause")
        return _clip(cause or exc)
    return ""


def _source_line(failure: dict[str, Any], details: dict[str, Any]) -> str:
    source = details.get("source") if isinstance(details.get("source"), dict) else {}
    state = failure.get("source_changed")
    previous = str(failure.get("previous_commit") or source.get("old_commit") or "")
    target = str(failure.get("target_commit") or source.get("target_commit") or "")
    root = str(failure.get("root") or "")
    if state is True:
        return ui_language.tr(
            "hashideploy.source.advanced",
            commit=_escape((target or previous)[:12]),
        )
    if state is False:
        return ui_language.tr(
            "hashideploy.source.unchanged",
            commit=_escape(previous[:12] or "—"),
        )
    return ui_language.tr(
        "hashideploy.source.unknown", root=_escape(root or "—"),
    )


def render_failure(exc: ReleaseUpdateError) -> str:
    """Render one human failure class plus the exact source state."""

    failure = release_failure(exc)
    code = str(failure.get("code") or "internal")
    source = _details(exc).get("source")
    source = dict(source) if isinstance(source, dict) else {}
    previous_branch = str(
        failure.get("previous_branch")
        or source.get("previous_branch")
        or ""
    )
    target_commit = str(
        failure.get("target_commit") or source.get("target_commit") or ""
    )
    actual_commit = str(failure.get("actual_commit") or "")
    dirty_count = failure.get("dirty_count")
    path_lines: list[str] = []
    if code in {"local_changes", "untracked_only"}:
        modified_count = failure.get("modified_count")
        untracked_count = failure.get("untracked_count")
        if modified_count is None and untracked_count is None:
            path_lines.append(_path_list(failure.get("dirty_paths")))
        else:
            if modified_count:
                path_lines.append(ui_language.tr(
                    "hashideploy.paths.modified",
                    count=str(modified_count),
                    paths=_path_list(failure.get("modified_paths")),
                ))
            if untracked_count:
                path_lines.append(ui_language.tr(
                    "hashideploy.paths.untracked",
                    count=str(untracked_count),
                    paths=_path_list(failure.get("untracked_paths")),
                ))
            if code == "local_changes" and modified_count:
                dirty_count = modified_count
            if code == "untracked_only" and untracked_count:
                dirty_count = untracked_count
    path_text = "\n".join(line for line in path_lines if line)
    values = {
        "branch": _escape(previous_branch or "HEAD"),
        "remote_branch": _escape(failure.get("branch") or ""),
        "target": _escape(target_commit[:12]),
        "actual": _escape((actual_commit or "—")[:12]),
        "count": _escape(dirty_count if dirty_count is not None else ""),
        "paths": path_text,
        "detail": _failure_detail(exc, failure),
    }
    text = ui_language.tr(_ERROR_KEYS.get(code, _ERROR_KEYS["internal"]), **values)
    lines = [text.rstrip(), "", _source_line(failure, _details(exc))]
    stash_commit = str(failure.get("stash_commit") or source.get("stash_commit") or "")
    stash_message = str(failure.get("stash_message") or source.get("stash_message") or "")
    if stash_commit:
        stash_count = failure.get("stashed_dirty_count")
        if stash_count is None:
            stash_count = source.get("stashed_dirty_count")
        lines.append(ui_language.tr(
            "hashideploy.force.stash_line",
            stash=_escape(stash_commit[:12]),
            count=_escape(stash_count if stash_count is not None else ""),
        ))
    elif stash_message:
        lines.append(ui_language.tr(
            "hashideploy.force.stash_named", message=_escape(stash_message),
        ))
    backup_ref = str(failure.get("backup_ref") or source.get("backup_ref") or "")
    if backup_ref:
        lines.append(ui_language.tr(
            "hashideploy.force.backup_line", ref=_escape(backup_ref),
        ))
    target_backup_ref = str(
        failure.get("target_branch_backup_ref")
        or source.get("target_branch_backup_ref")
        or ""
    )
    if target_backup_ref:
        lines.append(ui_language.tr(
            "hashideploy.force.target_branch_backup_line",
            branch=_escape(failure.get("branch") or source.get("branch") or "main"),
            ref=_escape(target_backup_ref),
        ))
    lines.append(ui_language.tr("hashideploy.failed.footer"))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(line for line in lines if line is not None)).strip()


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


def _success_text(result: dict[str, Any]) -> str:
    instance = _escape(result.get("instance_id"))
    commit = _escape(str(result.get("target_commit") or "")[:12])
    source = result.get("source") if isinstance(result.get("source"), dict) else {}
    if result.get("forced") is True or source.get("forced") is True:
        force = result.get("force") if isinstance(result.get("force"), dict) else {}
        stash = str(force.get("stash_commit") or source.get("stash_commit") or "")
        backup = str(force.get("backup_ref") or source.get("backup_ref") or "")
        count = force.get("stashed_dirty_count")
        if count is None:
            count = source.get("stashed_dirty_count")
        previous_branch = str(
            force.get("previous_branch") or source.get("previous_branch") or "HEAD"
        )
        previous_commit = str(
            force.get("previous_commit") or source.get("old_commit") or ""
        )
        text = ui_language.tr(
            "hashideploy.force.prepared", instance=instance, commit=commit,
            previous_branch=_escape(previous_branch),
            previous_commit=_escape(previous_commit[:12] or "—"),
            stash=_escape(stash[:12] or "—"),
            count=_escape(count if count is not None else "0"),
            backup=_escape(backup or "—"),
        )
        target_backup = str(
            force.get("target_branch_backup_ref")
            or source.get("target_branch_backup_ref")
            or ""
        )
        if target_backup:
            text += "\n" + ui_language.tr(
                "hashideploy.force.target_branch_backup_line",
                branch=_escape(source.get("branch") or "main"),
                ref=_escape(target_backup),
            )
        return text
    if source.get("switched_to_branch") is True:
        return ui_language.tr(
            "hashideploy.prepared_switched", instance=instance, commit=commit,
            branch=_escape(source.get("previous_branch") or "HEAD"),
        )
    return ui_language.tr(
        "hashideploy.prepared", instance=instance, commit=commit,
    )


async def hashideploy_command(runtime: Any, update: Any, context: Any) -> None:
    user_id = getattr(getattr(update, "effective_user", None), "id", None)
    checker = getattr(runtime, "_is_authorized_user", None)
    if user_id is None or not callable(checker) or not checker(user_id):
        await runtime._reply_text(update, ui_language.tr("hashideploy.denied"))
        return
    args = list(getattr(context, "args", None) or [])
    if args not in ([], ["force"]):
        await runtime._reply_text(update, ui_language.tr("hashideploy.usage"), parse_mode="HTML")
        return
    force = args == ["force"]
    channel = str(getattr(context, "source_channel", "") or "").strip().lower()
    if force and channel.startswith("hal"):
        await runtime._reply_text(
            update, ui_language.tr("hashideploy.force.not_automated"), parse_mode="HTML",
        )
        return
    if getattr(runtime, "_hashideploy_inflight", False):
        await runtime._reply_text(update, ui_language.tr("hashideploy.busy"))
        return
    config = getattr(runtime, "global_config", None)
    root = getattr(config, "project_root", None)
    home = getattr(config, "bridge_home", None)
    instance = str(getattr(config, "instance_id", "") or "").strip()
    if not root or not home or not instance:
        await runtime._reply_text(update, ui_language.tr("hashideploy.target_missing"), parse_mode="HTML")
        return

    # The thread outlives command/notification cancellation. Its task owns the
    # busy state and observes the final result even when the caller has gone.
    preparation = asyncio.create_task(asyncio.to_thread(
        run_staged_source_release,
        code_root=Path(root), bridge_home=Path(home), instance_id=instance,
        runtime_command=[sys.executable, "-B"], force=force,
    ))
    runtime._hashideploy_inflight = preparation
    preparation.add_done_callback(lambda task: _preparation_done(runtime, task))
    try:
        await runtime._reply_text(
            update,
            ui_language.tr(
                "hashideploy.force.preparing" if force else "hashideploy.preparing",
                instance=html.escape(instance),
            ),
            parse_mode="HTML",
        )
        result = await asyncio.shield(preparation)
        await runtime._reply_text(
            update,
            card_title("✅", ui_language.tr("hashideploy.ready")) + "\n\n"
            + _success_text(result),
            parse_mode="HTML",
        )
    except ReleaseUpdateError as exc:
        _LOG.warning("Source preparation failed: %s", redact_log_text(str(exc.to_payload())))
        await runtime._reply_text(
            update, render_failure(exc), parse_mode="HTML",
        )


COMMANDS = [
    RuntimeCommand(
        name="hashideploy",
        description="Prepare the latest HASHI source without restarting",
        callback=hashideploy_command,
    ),
]
# HASHI-Fingerprint: hfp_f68785ddcc7e4d52806f09a0f08baa46
