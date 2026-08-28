from __future__ import annotations

import html
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestrator import ui_language
from orchestrator.command_registry import RuntimeCommand
from orchestrator.context_compaction import (
    compact_status_text,
    coordinator_for,
    estimate_effective_context_tokens,
    load_policy,
)
from orchestrator.flexible_backend_registry import HER_V2_ENGINE
from orchestrator.her_v2.wip_journal import WIPJournal

logger = logging.getLogger("HASHI.Commands.Compact")


@dataclass(frozen=True)
class _WIPRecoveryResult:
    status: str
    code: str
    journal_count: int = 0
    record_count: int = 0
    source_bytes: int = 0
    capsule_chars: int = 0
    message: str = ""

    @property
    def changed(self) -> bool:
        return self.status == "completed" and self.journal_count > 0


def _wip_journals(runtime: Any, session_workspace: Path) -> list[WIPJournal]:
    candidates = [
        Path(session_workspace) / "backend_state" / "her_v2" / "wip_journal.jsonl",
        Path(getattr(runtime, "workspace_dir", session_workspace))
        / "backend_state"
        / "her_v2"
        / "wip_journal.jsonl",
    ]
    result: list[WIPJournal] = []
    seen: set[str] = set()
    for path in candidates:
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        journal = WIPJournal(path)
        if journal.snapshot().active:
            result.append(journal)
    return result


def _wip_status_text(runtime: Any, session_workspace: Path) -> str:
    journals = _wip_journals(runtime, session_workspace)
    if not journals:
        return (
            f"<b>{html.escape(ui_language.tr('compact.wip'))}</b> · "
            f"<code>{html.escape(ui_language.tr('compact.wip.clear'))}</code>"
        )
    snapshots = [journal.snapshot() for journal in journals]
    return (
        f"<b>{html.escape(ui_language.tr('compact.wip'))}</b> · "
        f"<code>{html.escape(ui_language.tr('compact.wip.active'))}</code>\n"
        f"<b>{html.escape(ui_language.tr('compact.recovery_records'))}</b> · <code>{sum(row.record_count for row in snapshots):,}</code>\n"
        f"<b>{html.escape(ui_language.tr('compact.journal_bytes'))}</b> · <code>{sum(row.size_bytes for row in snapshots):,}</code>\n"
        f"{ui_language.tr('compact.wip_help')}"
    )


def _coordinator_running(coordinator: Any) -> bool:
    task = getattr(coordinator, "_active_task", None)
    done = getattr(task, "done", None)
    return bool(task is not None and callable(done) and not done())


def _compact_wip_journals(
    runtime: Any,
    *,
    session_workspace: Path,
    memory_store: Any,
    coordinator: Any,
) -> _WIPRecoveryResult:
    journals = _wip_journals(runtime, session_workspace)
    if not journals:
        return _WIPRecoveryResult(status="not_needed", code="NO_ACTIVE_WIP")
    committed = 0
    record_count = 0
    source_bytes = 0
    capsule_chars = 0
    for journal in journals:
        snapshot = journal.snapshot()
        if not snapshot.active:
            continue
        compaction_id = f"wip-{snapshot.file_sha256.removeprefix('sha256:')[:24]}"
        try:
            coordinator.store.append_audit(
                "wip_recovery_started",
                compaction_id=compaction_id,
                payload={
                    "source_sha256": snapshot.file_sha256,
                    "source_record_count": snapshot.record_count,
                    "source_bytes": snapshot.size_bytes,
                    "model_invoked": False,
                    "session_workspace": str(Path(session_workspace).resolve()),
                },
            )
            capsule = journal.recovery_capsule(
                snapshot.records,
                source_sha256=snapshot.file_sha256,
            )
            encoded = json.dumps(
                capsule,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            recorder = getattr(memory_store, "record_recovery_capsule", None)
            if not callable(recorder):
                raise RuntimeError(ui_language.tr("compact.wip.store_missing"))
            turn_id = recorder(encoded, origin_ref=snapshot.file_sha256)
            if not turn_id:
                raise RuntimeError(ui_language.tr("compact.wip.commit_missing"))
            coordinator.store.append_audit(
                "wip_recovery_capsule_committed",
                compaction_id=compaction_id,
                payload={
                    "source_sha256": snapshot.file_sha256,
                    "recovery_turn_id": int(turn_id),
                    "capsule_chars": len(encoded),
                    "model_invoked": False,
                },
            )
            if not journal.clear_if_unchanged(snapshot.file_sha256):
                raise RuntimeError(ui_language.tr("compact.wip.changed"))
            try:
                coordinator.store.append_audit(
                    "wip_recovery_completed",
                    compaction_id=compaction_id,
                    payload={
                        "source_sha256": snapshot.file_sha256,
                        "recovery_turn_id": int(turn_id),
                        "journal_cleared": True,
                        "raw_source_retained": False,
                        "model_invoked": False,
                    },
                )
            except Exception:
                # The capsule and compare-and-swap clear are already durable.
                # A trailing diagnostic write cannot truthfully turn that
                # completed transaction into a "preserved" failure.
                logger.warning(
                    "WIP recovery completion audit failed after commit: %s",
                    compaction_id,
                )
        except Exception as exc:
            try:
                coordinator.store.append_audit(
                    "wip_recovery_failed_preserved",
                    compaction_id=compaction_id,
                    payload={
                        "source_sha256": snapshot.file_sha256,
                        "journal_cleared": False,
                        "error_type": type(exc).__name__,
                    },
                )
            except Exception:
                pass
            logger.warning(
                "WIP recovery Compact failed safely: %s", type(exc).__name__
            )
            return _WIPRecoveryResult(
                status="failed",
                code="WIP_RECOVERY_FAILED_PRESERVED",
                journal_count=committed,
                record_count=record_count,
                source_bytes=source_bytes,
                capsule_chars=capsule_chars,
                message=ui_language.tr("compact.wip.failed_message"),
            )
        committed += 1
        record_count += snapshot.record_count
        source_bytes += snapshot.size_bytes
        capsule_chars += len(encoded)
    return _WIPRecoveryResult(
        status="completed",
        code="WIP_RECOVERY_COMPACTED",
        journal_count=committed,
        record_count=record_count,
        source_bytes=source_bytes,
        capsule_chars=capsule_chars,
        message=ui_language.tr("compact.wip.completed_message"),
    )


def _is_authorized(runtime: Any, update: Any) -> bool:
    checker = getattr(runtime, "_is_authorized_user", None)
    user_id = getattr(getattr(update, "effective_user", None), "id", None)
    if callable(checker):
        return bool(checker(user_id))
    authorized_id = getattr(
        getattr(runtime, "global_config", None), "authorized_id", None
    )
    return authorized_id is None or user_id == authorized_id


async def _send(runtime: Any, update: Any, text: str) -> None:
    chat_id = getattr(getattr(update, "effective_chat", None), "id", None)
    reply = getattr(runtime, "_reply_text", None)
    if callable(reply):
        await reply(update, text, parse_mode="HTML")
        return
    if chat_id is not None and hasattr(runtime, "send_long_message"):
        await runtime.send_long_message(
            chat_id,
            text,
            request_id="compact-command",
            purpose="command",
        )
        return
    message = getattr(update, "effective_message", None) or getattr(
        update, "message", None
    )
    if message is not None and hasattr(message, "reply_text"):
        await message.reply_text(text, parse_mode="HTML")


def _outcome_text(
    outcome: Any,
    *,
    wip: _WIPRecoveryResult | None = None,
) -> str:
    lines = []
    if wip is not None and wip.changed:
        lines.extend(
            [
                f"<b>{ui_language.tr('compact.wip.completed')}</b>",
                f"<b>{html.escape(ui_language.tr('compact.code'))}</b> · <code>{html.escape(wip.code)}</code>",
                f"<b>{html.escape(ui_language.tr('compact.recovery_records'))}</b> · <code>{wip.record_count:,}</code>",
                f"<b>{html.escape(ui_language.tr('compact.journal_bytes'))}</b> · <code>{wip.source_bytes:,}</code>",
                f"<b>{html.escape(ui_language.tr('compact.recovery_capsule'))}</b> · <code>{wip.capsule_chars:,} {html.escape(ui_language.tr('compact.chars'))}</code>",
                "",
            ]
        )
    elif wip is not None and wip.status == "failed":
        lines.extend(
            [
                f"<b>{ui_language.tr('compact.wip.failed')}</b>",
                f"<b>{html.escape(ui_language.tr('compact.code'))}</b> · <code>{html.escape(wip.code)}</code>",
                "",
                html.escape(wip.message),
            ]
        )
        return "\n".join(lines)
    title = {
        "completed": ui_language.tr("compact.outcome.completed"),
        "not_needed": ui_language.tr("compact.outcome.not_needed"),
        "locked": ui_language.tr("compact.outcome.locked"),
        "failed": ui_language.tr("compact.outcome.failed"),
    }.get(str(outcome.status), ui_language.tr("compact.outcome.result"))
    lines.append(f"<b>{title}</b>")
    if outcome.code:
        lines.append(
            f"<b>{html.escape(ui_language.tr('compact.code'))}</b> · "
            f"<code>{html.escape(str(outcome.code))}</code>"
        )
    if outcome.changed:
        saved = max(0, int(outcome.before_tokens) - int(outcome.after_tokens))
        lines.extend(
            [
                f"<b>{html.escape(ui_language.tr('compact.history_before'))}</b> · <code>{int(outcome.before_tokens):,} {html.escape(ui_language.tr('compact.tokens'))}</code>",
                f"<b>{html.escape(ui_language.tr('compact.history_after'))}</b> · <code>{int(outcome.after_tokens):,} {html.escape(ui_language.tr('compact.tokens'))}</code>",
                f"<b>{html.escape(ui_language.tr('compact.reduced_by'))}</b> · <code>{saved:,} {html.escape(ui_language.tr('compact.tokens'))}</code>",
            ]
        )
    elif int(getattr(outcome, "before_tokens", 0) or 0) > 0:
        lines.append(
            f"<b>{html.escape(ui_language.tr('compact.current_context'))}</b> · "
            f"<code>{int(outcome.before_tokens):,} {html.escape(ui_language.tr('compact.tokens'))}</code>"
        )
    if outcome.message:
        lines.extend(["", html.escape(str(outcome.message))])
    if outcome.changed:
        lines.extend(["", ui_language.tr("compact.raw_retained")])
    return "\n".join(lines)


async def compact_command(runtime: Any, update: Any, context: Any) -> None:
    if not _is_authorized(runtime, update):
        return
    if str(getattr(runtime.config, "active_backend", "")) != HER_V2_ENGINE:
        await _send(
            runtime,
            update,
            ui_language.tr("compact.active_only"),
        )
        return

    action = str((getattr(context, "args", None) or [""])[0]).strip().lower()
    from orchestrator import runtime_session
    from orchestrator.bridge_memory import BridgeMemoryStore

    session = runtime_session.current_session_for_update(runtime, update)
    workspace = runtime_session.ensure_store(runtime).session_workspace(
        session["session_id"], int(session["context_generation"])
    )
    coordinator = coordinator_for(
        runtime,
        workspace_dir=workspace,
        memory_store=BridgeMemoryStore(workspace),
    )
    if action in {"status", "show", "info"}:
        try:
            context_status = compact_status_text(runtime, coordinator=coordinator)
        except Exception as exc:
            logger.warning(
                "Conversation Compact status unavailable while WIP status remains usable: %s",
                type(exc).__name__,
            )
            context_status = (
                f"{ui_language.tr('compact.status_unavailable')}\n\n"
                f"{ui_language.tr('compact.status_unavailable_effect')}"
            )
        await _send(
            runtime,
            update,
            context_status + "\n\n" + _wip_status_text(runtime, workspace),
        )
        return
    if action in {"cancel", "stop"}:
        cancelled = await coordinator.cancel()
        await _send(
            runtime,
            update,
            (
                f"{ui_language.tr('compact.cancelled')}\n\n"
                f"{ui_language.tr('compact.cancelled_effect')}"
                if cancelled
                else ui_language.tr("compact.none_running")
            ),
        )
        return
    if action and action not in {"run", "now", "force"}:
        await _send(
            runtime,
            update,
            ui_language.tr("compact.usage"),
        )
        return
    if _coordinator_running(coordinator):
        await _send(
            runtime,
            update,
            f"{ui_language.tr('compact.already_running')}\n\n"
            f"{ui_language.tr('compact.running_help')}",
        )
        return

    wip = _compact_wip_journals(
        runtime,
        session_workspace=workspace,
        memory_store=coordinator.memory_store,
        coordinator=coordinator,
    )
    if wip.status == "failed":
        await _send(runtime, update, _outcome_text(None, wip=wip))
        return

    policy = load_policy(runtime)
    current_tokens = estimate_effective_context_tokens(
        runtime,
        coordinator=coordinator,
        use_last_runtime_measurement=False,
    )
    if current_tokens < policy.manual_min_tokens:
        outcome = await coordinator.compact(
            trigger="manual_command",
            request_ref=f"compact-command:{getattr(update, 'update_id', 'unknown')}",
            force=True,
        )
        await _send(runtime, update, _outcome_text(outcome, wip=wip))
        return

    await _send(
        runtime,
        update,
        f"{ui_language.tr('compact.started')}\n\n"
        f"{html.escape(ui_language.tr('compact.current_context'))} · <code>{current_tokens:,} {html.escape(ui_language.tr('compact.tokens'))}</code>\n"
        f"{html.escape(ui_language.tr('compact.manual_threshold'))} · <code>{policy.manual_min_tokens:,} {html.escape(ui_language.tr('compact.tokens'))}</code>\n"
        f"{html.escape(ui_language.tr('compact.automatic_trigger'))} · <code>&gt; {policy.auto_trigger_tokens:,} {html.escape(ui_language.tr('compact.tokens'))}</code>",
    )
    outcome = await coordinator.compact(
        trigger="manual_command",
        request_ref=f"compact-command:{getattr(update, 'update_id', 'unknown')}",
        force=True,
    )
    await _send(runtime, update, _outcome_text(outcome, wip=wip))


COMMANDS = [
    RuntimeCommand(
        name="compact",
        description="Compact eligible HER v2 history [status|cancel]",
        callback=compact_command,
    )
]
