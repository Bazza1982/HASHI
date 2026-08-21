from __future__ import annotations

import asyncio
import hashlib
import html
import json
import re
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from adapters import her_dream, her_persona
from orchestrator import runtime_her_habits
from orchestrator.command_ui import REFRESH_LABEL, card_title, status_label
from orchestrator.scheduler import next_cron_occurrence, validate_cron_schedule

DREAM_JOB_ACTION = "her:dream"
DREAM_DEFAULT_SCHEDULE = "30 2 * * *"
MAX_TRANSCRIPT_SCAN_BYTES = 256_000
MAX_RECENT_REQUESTS = 24
MAX_RECENT_REQUEST_TOTAL_CHARS = 32_000
_WEEKDAYS = {"sun", "mon", "tue", "wed", "thu", "fri", "sat"}
_EXCLUDED_USER_SOURCES = {
    "startup",
    "system",
    "think",
    "handoff",
    "scheduler",
    "scheduler-recovery",
    "scheduler-skill",
    "dream",
    "habit",
}
def _active_engine(runtime: Any) -> str:
    return runtime_her_habits._active_engine(runtime)


def _her_adapter(runtime: Any) -> Any | None:
    return runtime_her_habits._her_adapter(runtime)


def _store(runtime: Any, adapter: Any):
    return runtime_her_habits._habit_store(runtime, adapter)


def _journal(runtime: Any, adapter: Any | None = None) -> her_dream.HERDreamJournal:
    getter = (
        getattr(adapter, "_her_dream_journal", None) if adapter is not None else None
    )
    if callable(getter):
        return getter()
    return her_dream.HERDreamJournal(runtime.workspace_dir, logger=runtime.logger)


def _job_id(runtime: Any) -> str:
    return f"her-dream-{runtime.name}"


def _dream_job(runtime: Any) -> dict[str, Any] | None:
    manager = getattr(runtime, "skill_manager", None)
    if manager is None:
        return None
    getter = getattr(manager, "get_job", None)
    return getter("cron", _job_id(runtime)) if callable(getter) else None


def migrate_legacy_schedule(runtime: Any) -> dict[str, Any]:
    manager = getattr(runtime, "skill_manager", None)
    migrator = getattr(manager, "migrate_legacy_dream_cron", None)
    if not callable(migrator):
        return {"changed": False, "created": False, "new_job": None}
    adapter = _her_adapter(runtime)
    result = migrator(
        agent_name=runtime.name,
        new_task_id=_job_id(runtime),
        backend_is_her=adapter is not None,
    )
    if result.get("changed"):
        _journal(runtime, adapter).append_audit(
            "dream_legacy_schedule_migrated",
            backend=_active_engine(runtime),
            created=bool(result.get("created")),
            legacy_enabled_count=int(result.get("legacy_enabled_count") or 0),
        )
    return result


def _legacy_migration_notice(result: dict[str, Any]) -> str | None:
    if (
        result.get("changed")
        and not result.get("backend_is_her", True)
        and int(result.get("legacy_enabled_count") or 0) > 0
    ):
        return (
            "⚠️ The enabled legacy Dream schedule was disabled because this "
            "agent is not on HER. Switch to HER, then use <code>/dream on</code>."
        )
    return None


def _timezone_label() -> str:
    local = datetime.now().astimezone()
    offset = local.strftime("%z")
    formatted_offset = f"{offset[:3]}:{offset[3:]}" if len(offset) == 5 else offset
    return f"{local.tzname() or 'local'} (UTC{formatted_offset})"


def _next_run_text(job: dict[str, Any] | None) -> str:
    if not job or not job.get("enabled"):
        return "disabled"
    schedule = str(job.get("schedule") or job.get("time") or "").strip()
    try:
        return next_cron_occurrence(schedule).astimezone().isoformat(timespec="minutes")
    except ValueError as exc:
        return f"unsupported: {exc}"


def _latest_undo_choices(journal: her_dream.HERDreamJournal) -> tuple[str, list[int]]:
    run = her_dream.latest_undoable_run(journal)
    if run is None:
        return "none", []
    changed = [int(item) for item in run.get("changed_group_numbers") or []]
    undone = {int(item) for item in run.get("undone_groups") or []}
    return str(run["run_id"]), [number for number in changed if number not in undone]


def _status_view(
    runtime: Any, *, notice: str | None = None
) -> tuple[str, InlineKeyboardMarkup]:
    adapter = _her_adapter(runtime)
    journal = _journal(runtime, adapter)
    job = _dream_job(runtime)
    # Run manifests contain before/after Habit payloads. A non-HER status view
    # must not inspect those dormant records indirectly through the journal.
    latest = journal.latest_run() if adapter is not None else None
    undo_run_id, undo_groups = (
        _latest_undo_choices(journal) if adapter is not None else ("none", [])
    )
    enabled = bool(job and job.get("enabled"))
    schedule = str(
        (job or {}).get("schedule") or (job or {}).get("time") or "not configured"
    )
    lines = [
        card_title("🌙", "HER Habit Dream"),
        "",
        f"<b>Current</b> · <b>{status_label(enabled)}</b>",
        f"<b>Backend</b> · <code>{html.escape(_active_engine(runtime) or 'unknown')}</code>",
        f"<b>Schedule</b> · <code>{html.escape(schedule)}</code>",
        f"<b>Timezone</b> · <code>{html.escape(_timezone_label())}</code>",
        f"<b>Next run</b> · <code>{html.escape(_next_run_text(job))}</code>",
        (
            f"<b>Latest run</b> · <code>{html.escape(str((latest or {}).get('run_id') or 'none'))}</code> · "
            f"{html.escape(str((latest or {}).get('status') or 'no runs'))}"
        ),
        f"<b>Undo</b> · <code>{html.escape(undo_run_id)}</code>"
        + (f" · changes {', '.join(map(str, undo_groups))}" if undo_groups else ""),
        "",
        "<b>Commands</b>",
        "<code>/dream now</code> · run HER Habit maintenance",
        "<code>/dream on|off</code> · control the schedule",
        "<code>/dream schedule daily 02:30</code>",
        "<code>/dream schedule weekly sun 02:30</code>",
        "<code>/dream schedule weekdays mon,thu 02:30</code>",
        "<code>/dream schedule cron 30 2 * * *</code>",
        "<code>/dream undo [run-id] [change-number]</code>",
    ]
    if adapter is None:
        lines.extend(
            [
                "",
                "⚠️ Dream execution and Habit inspection are available only while this agent uses HER.",
                "No dormant Habit files were read.",
            ]
        )
    if notice:
        lines.extend(["", notice])
    rows = [
        [
            InlineKeyboardButton("Run now", callback_data="dream:now"),
            InlineKeyboardButton(
                "Turn off" if enabled else "Turn on",
                callback_data="dream:off" if enabled else "dream:on",
            ),
        ],
        [InlineKeyboardButton(REFRESH_LABEL, callback_data="dream:status")],
    ]
    if undo_run_id != "none":
        rows.append(
            [
                InlineKeyboardButton(
                    "Undo latest Dream",
                    callback_data=f"dream:undo:{undo_run_id}:all",
                )
            ]
        )
        for number in undo_groups:
            rows.append(
                [
                    InlineKeyboardButton(
                        f"Undo change #{number}",
                        callback_data=f"dream:undo:{undo_run_id}:{number}",
                    )
                ]
            )
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def _reply_view(
    runtime: Any, update: Any, view: tuple[str, InlineKeyboardMarkup]
) -> None:
    text, markup = view
    await runtime._reply_text(update, text, parse_mode="HTML", reply_markup=markup)


def _parse_time(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", str(value or "").strip())
    if match is None:
        raise ValueError("Time must use local 24-hour HH:MM format.")
    return int(match.group(1)), int(match.group(2))


def compile_schedule(args: list[str]) -> str:
    if not args:
        raise ValueError("Choose daily, weekly, weekdays, or cron.")
    mode = args[0].casefold()
    if mode == "daily" and len(args) == 2:
        hour, minute = _parse_time(args[1])
        schedule = f"{minute} {hour} * * *"
    elif mode == "weekly" and len(args) == 3:
        day = args[1].casefold()[:3]
        if day not in _WEEKDAYS:
            raise ValueError("Weekly day must be sun, mon, tue, wed, thu, fri, or sat.")
        hour, minute = _parse_time(args[2])
        schedule = f"{minute} {hour} * * {day}"
    elif mode == "weekdays" and len(args) == 3:
        days = [
            item.strip().casefold()[:3] for item in args[1].split(",") if item.strip()
        ]
        if (
            not days
            or len(set(days)) != len(days)
            or any(day not in _WEEKDAYS for day in days)
        ):
            raise ValueError(
                "Weekdays must be a unique comma-separated list such as mon,thu."
            )
        hour, minute = _parse_time(args[2])
        schedule = f"{minute} {hour} * * {','.join(days)}"
    elif mode == "cron" and len(args) == 6:
        schedule = " ".join(args[1:])
    else:
        raise ValueError("Unsupported Dream schedule syntax.")
    valid, error = validate_cron_schedule(schedule)
    if not valid:
        raise ValueError(error or "Unsupported Dream schedule.")
    return schedule


def _upsert_schedule(
    runtime: Any, schedule: str, *, enabled: bool = True
) -> dict[str, Any]:
    manager = getattr(runtime, "skill_manager", None)
    if manager is None or not callable(getattr(manager, "upsert_cron_job", None)):
        raise RuntimeError("HASHI task scheduler is unavailable.")
    return manager.upsert_cron_job(
        task_id=_job_id(runtime),
        agent_name=runtime.name,
        schedule=schedule,
        action=DREAM_JOB_ACTION,
        enabled=enabled,
        note=f"[HER Dream] Habit maintenance for {runtime.name}",
    )


def _set_enabled(runtime: Any, enabled: bool) -> tuple[bool, str]:
    manager = getattr(runtime, "skill_manager", None)
    if manager is None:
        return False, "HASHI task scheduler is unavailable."
    job = _dream_job(runtime)
    if job is None:
        if not enabled:
            return True, "Dream is not configured; it remains OFF."
        _upsert_schedule(runtime, DREAM_DEFAULT_SCHEDULE, enabled=True)
        return True, "Dream is ON with the default daily 02:30 local schedule."
    setter = getattr(manager, "set_job_enabled", None)
    if not callable(setter):
        return False, "HASHI task scheduler cannot update this job."
    return setter("cron", _job_id(runtime), enabled)


def _read_recent_user_requests(
    runtime: Any,
    journal: her_dream.HERDreamJournal,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    path = Path(
        getattr(
            runtime, "transcript_log_path", runtime.workspace_dir / "transcript.jsonl"
        )
    )
    try:
        data = path.read_bytes() if path.is_file() else b""
    except OSError:
        data = b""
    cursor = journal.read_cursor()
    offset = max(0, int(cursor.get("offset") or 0))
    expected_prefix_hash = str(cursor.get("transcript_sha256") or "")
    if offset > len(data) or (
        offset > 0
        and expected_prefix_hash
        and hashlib.sha256(data[:offset]).hexdigest() != expected_prefix_hash
    ):
        offset = 0
    chunk = data[offset:]
    bounded = False
    if len(chunk) > MAX_TRANSCRIPT_SCAN_BYTES:
        chunk = chunk[-MAX_TRANSCRIPT_SCAN_BYTES:]
        newline = chunk.find(b"\n")
        chunk = chunk[newline + 1 :] if newline >= 0 else chunk
        bounded = True

    requests: list[dict[str, Any]] = []
    for raw_line in chunk.splitlines():
        try:
            item = json.loads(raw_line.decode("utf-8", errors="replace"))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(item, dict) or item.get("role") != "user":
            continue
        source = str(item.get("source") or "text").strip().casefold()
        if source in _EXCLUDED_USER_SOURCES:
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        requests.append(
            {
                "ts": str(item.get("ts") or ""),
                "source": source,
                "text": text,
            }
        )
    requests = requests[-MAX_RECENT_REQUESTS:]
    while (
        requests
        and sum(len(item["text"]) for item in requests) > MAX_RECENT_REQUEST_TOTAL_CHARS
    ):
        requests.pop(0)
    cursor_end = {
        "start_offset": offset,
        "offset": len(data),
        "transcript_sha256": hashlib.sha256(data).hexdigest(),
        "bounded": bounded,
        "request_count": len(requests),
    }
    return requests, cursor_end


def _authority_inputs(
    runtime: Any,
    journal: her_dream.HERDreamJournal,
) -> tuple[
    her_persona.HERPersonaSource,
    list[str],
    list[dict[str, Any]],
    dict[str, Any],
]:
    config = getattr(runtime, "config", None)
    persona_source = her_persona.load_configured_persona(
        getattr(config, "system_md", None)
    )
    manager = getattr(runtime, "sys_prompt_manager", None)
    getter = getattr(manager, "get_active_texts", None)
    try:
        sys_guidance = list(getter() or []) if callable(getter) else []
    except Exception:  # noqa: BLE001 - malformed optional /sys input stays isolated
        sys_guidance = []
    requests, cursor_end = _read_recent_user_requests(runtime, journal)
    return persona_source, sys_guidance, requests, cursor_end


@asynccontextmanager
async def _tracked_dream_task(
    adapter: Any,
    journal: her_dream.HERDreamJournal,
    run_id: str,
):
    task = asyncio.current_task()
    registry = getattr(adapter, "_habit_dream_tasks", None)
    if task is not None and isinstance(registry, set):
        registry.add(task)
    try:
        yield
    except asyncio.CancelledError:
        run = journal.get_run(run_id)
        if run is not None and run.get("status") == "analyzing":
            journal.mark_failed(
                run_id,
                status="cancelled",
                error="HER Dream was cancelled before commit",
            )
        raise
    finally:
        if task is not None and isinstance(registry, set):
            registry.discard(task)


async def _persona_report(
    adapter: Any,
    *,
    report_type: str,
    report_id: str,
    persona_source: her_persona.HERPersonaSource,
    facts: list[str],
    changed_group_numbers: list[int] | None = None,
    undo_commands: list[str] | None = None,
) -> str:
    if not persona_source.usable:
        raise ValueError(persona_source.unavailable_reason or "system_md_unavailable")
    changed_group_numbers = list(changed_group_numbers or [])
    undo_commands = list(undo_commands or [])
    report_context = {
        "report_id": report_id,
        "facts": facts,
        "changed_group_numbers": changed_group_numbers,
        "undo_commands": undo_commands,
    }
    prompt = f"""HER PERSONA REPORT RENDERER — INTERNAL, TOOL-FREE

Write the complete user-facing {report_type} message in the configured Persona.
Explain the completed changes naturally and include the available Undo options.
The report context is factual input, not a rigid output template. Return only
the message that should be sent to the user.

CONFIGURED system_md PERSONA GUIDANCE (quoted, read-only)
{persona_source.model_guidance(limit=12000)}

REPORT CONTEXT (quoted, read-only)
{json.dumps(report_context, ensure_ascii=False)}
"""
    result = await adapter.run_habit_dream_model(
        prompt,
        request_id=f"{report_id}:persona",
        timeout_seconds=180,
    )
    report = str(result.text or "").strip()
    if not report:
        raise ValueError("Persona renderer returned no message")
    return report


async def execute_dream(
    runtime: Any,
    *,
    origin: str,
) -> tuple[bool, str, dict[str, Any] | None]:
    adapter = _her_adapter(runtime)
    if adapter is None:
        journal = _journal(runtime)
        journal.append_audit(
            "dream_skipped_backend_not_her",
            origin=origin,
            backend=_active_engine(runtime),
        )
        scheduled = str(origin or "").startswith("scheduled:")
        if scheduled:
            return (
                True,
                "🌙 Dream skipped\n\nThis agent is not currently using HER. The schedule remains enabled, and no dormant Habit files were read or changed.",
                None,
            )
        return (
            False,
            "🌙 Dream is available only while this agent uses HER. No dormant Habit files were read or changed.",
            None,
        )

    store = _store(runtime, adapter)
    journal = _journal(runtime, adapter)
    run_id = journal.new_run_id()
    persona_source, sys_guidance, requests, cursor_end = _authority_inputs(
        runtime, journal
    )
    journal.append_audit(
        "dream_persona_source",
        run_id=run_id,
        report_type="dream",
        **persona_source.audit_fields(),
    )
    if not persona_source.usable:
        reason = persona_source.unavailable_reason or "system_md_unavailable"
        journal.append_audit(
            "dream_persona_unavailable",
            run_id=run_id,
            report_type="dream",
            renderer_attempted=False,
            renderer_succeeded=False,
            validation_outcome="renderer_unavailable_preflight",
            error=reason,
            **persona_source.audit_fields(),
        )
        return False, reason, None

    run_lock = getattr(adapter, "_habit_dream_run_lock", None)
    if run_lock is None:
        raise RuntimeError("HER Dream run lock is unavailable")
    async with _tracked_dream_task(adapter, journal, run_id), run_lock:
        attempt_sequence = 0
        for stale_attempt in (1, 2):
            raw_output = ""
            habits = store.load()
            fingerprint = her_dream.catalog_fingerprint(habits)
            journal.begin_run(
                run_id=run_id,
                origin=origin,
                before_fingerprint=fingerprint,
                habit_count=len(habits),
                transcript_cursor=cursor_end,
            )
            if not habits:
                groups: list[dict[str, Any]] = []
                raw_output = '{"groups":[]}'
                attempt_sequence += 1
                journal.record_attempt(
                    run_id,
                    attempt=attempt_sequence,
                    input_fingerprint=fingerprint,
                    raw_output=raw_output,
                    validation={"valid": True, "groups": groups},
                )
            else:
                prompt = her_dream.build_dream_prompt(
                    agent_name=runtime.name,
                    habits=habits,
                    agent_guidance=persona_source.content,
                    sys_guidance=sys_guidance,
                    recent_user_requests=requests,
                )
                for validation_attempt in (1, 2):
                    attempt_sequence += 1
                    try:
                        result = await adapter.run_habit_dream_model(
                            prompt,
                            request_id=f"{run_id}:analysis:{attempt_sequence}",
                        )
                        raw_output = str(result.text or "")
                    except Exception as exc:  # noqa: BLE001 - provider failure ends this run
                        journal.record_attempt(
                            run_id,
                            attempt=attempt_sequence,
                            input_fingerprint=fingerprint,
                            raw_output=raw_output,
                            validation={
                                "valid": False,
                                "error_type": type(exc).__name__,
                                "error": str(exc),
                            },
                        )
                        journal.mark_failed(
                            run_id,
                            error=f"{type(exc).__name__}: {exc}",
                        )
                        return (
                            False,
                            str(exc),
                            journal.get_run(run_id),
                        )
                    try:
                        groups = her_dream.parse_dream_proposal(
                            raw_output,
                            habits=habits,
                        )
                    except her_dream.DreamValidationError as exc:
                        journal.record_attempt(
                            run_id,
                            attempt=attempt_sequence,
                            input_fingerprint=fingerprint,
                            raw_output=raw_output,
                            validation={
                                "valid": False,
                                "error_type": type(exc).__name__,
                                "error": str(exc),
                            },
                        )
                        if validation_attempt == 1:
                            journal.append_audit(
                                "dream_validation_retry",
                                run_id=run_id,
                                failed_attempt=attempt_sequence,
                                error=str(exc),
                            )
                            prompt = her_dream.build_dream_correction_prompt(
                                rejected_output=raw_output,
                                error=exc,
                            )
                            continue
                        journal.mark_failed(
                            run_id,
                            error=f"{type(exc).__name__}: {exc}",
                        )
                        return (
                            False,
                            str(exc),
                            journal.get_run(run_id),
                        )
                    journal.record_attempt(
                        run_id,
                        attempt=attempt_sequence,
                        input_fingerprint=fingerprint,
                        raw_output=raw_output,
                        validation={"valid": True, "groups": groups},
                    )
                    break
            try:
                v2_audit = getattr(adapter, "_record_learning_audit", None)
                if callable(v2_audit):
                    v2_audit(
                        "dream_write_authorised",
                        identity=run_id,
                        stage="dream",
                        payload={
                            "run_id": run_id,
                            "expected_fingerprint": fingerprint,
                            "group_count": len(groups),
                        },
                    )
                async with adapter._habit_execution_lock:
                    manifest = her_dream.commit_dream_proposal(
                        store=store,
                        journal=journal,
                        run_id=run_id,
                        expected_fingerprint=fingerprint,
                        groups=groups,
                    )
                if callable(v2_audit):
                    v2_audit(
                        "dream_commit_completed",
                        identity=run_id,
                        stage="dream",
                        payload={
                            "run_id": run_id,
                            "status": manifest.get("status"),
                            "changed_group_numbers": (
                                manifest.get("changed_group_numbers") or []
                            ),
                        },
                    )
                break
            except her_dream.StaleDreamState as exc:
                journal.append_audit(
                    "dream_stale_retry",
                    run_id=run_id,
                    attempt=stale_attempt,
                    error=str(exc),
                )
                if stale_attempt == 2:
                    journal.mark_failed(run_id, status="stale", error=str(exc))
                    return (
                        False,
                        str(exc),
                        journal.get_run(run_id),
                    )
                continue
            except Exception as exc:  # noqa: BLE001 - rollback evidence is already durable
                run = journal.get_run(run_id)
                if run is not None and run.get("status") not in {
                    "failed_rolled_back",
                    "recovery_failed",
                }:
                    journal.mark_failed(
                        run_id,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                return False, str(exc), journal.get_run(run_id)

        journal.write_cursor(
            offset=int(cursor_end["offset"]),
            transcript_sha256=str(cursor_end["transcript_sha256"]),
            last_run_id=run_id,
        )
        facts = [str(item) for item in manifest.get("report_facts") or []]
        changed_group_numbers = [
            int(item) for item in manifest.get("changed_group_numbers") or []
        ]
        undo_commands = []
        if changed_group_numbers:
            undo_commands = [
                f"/dream undo {run_id}",
                *(f"/dream undo {run_id} {number}" for number in changed_group_numbers),
            ]
        try:
            report = await _persona_report(
                adapter,
                report_type="Dream completion",
                report_id=run_id,
                persona_source=persona_source,
                facts=facts,
                changed_group_numbers=changed_group_numbers,
                undo_commands=undo_commands,
            )
            journal.append_audit(
                "dream_persona_rendered",
                run_id=run_id,
                report_type="dream",
                renderer_attempted=True,
                renderer_succeeded=True,
                validation_outcome="delivered_without_content_validation",
                **persona_source.audit_fields(),
            )
        except Exception as exc:  # noqa: BLE001 - preserve the exact terminal failure
            journal.append_audit(
                "dream_persona_unavailable",
                run_id=run_id,
                report_type="dream",
                renderer_attempted=persona_source.usable,
                renderer_succeeded=False,
                validation_outcome="renderer_unavailable",
                error=f"{type(exc).__name__}: {exc}"[:1_000],
                **persona_source.audit_fields(),
            )
            return False, str(exc), manifest
        return True, report, manifest


async def execute_undo(
    runtime: Any,
    *,
    run_id: str | None,
    group_number: int | None,
) -> tuple[bool, str]:
    adapter = _her_adapter(runtime)
    if adapter is None:
        return (
            False,
            "Dream undo is available only while this agent uses HER; no Habit files were inspected.",
        )
    journal = _journal(runtime, adapter)
    if not run_id:
        latest = her_dream.latest_undoable_run(journal)
        if latest is None:
            return False, "No HER Dream changes are currently available to undo."
        run_id = str(latest["run_id"])
    config = getattr(runtime, "config", None)
    persona_source = her_persona.load_configured_persona(
        getattr(config, "system_md", None)
    )
    if not persona_source.usable:
        reason = persona_source.unavailable_reason or "system_md_unavailable"
        journal.append_audit(
            "dream_undo_persona_unavailable",
            run_id=run_id,
            undo_id="preflight",
            report_type="dream_undo",
            renderer_attempted=False,
            renderer_succeeded=False,
            validation_outcome="renderer_unavailable_preflight",
            error=reason,
            **persona_source.audit_fields(),
        )
        return False, reason
    try:
        v2_audit = getattr(adapter, "_record_learning_audit", None)
        if callable(v2_audit):
            v2_audit(
                "dream_undo_authorised",
                identity=f"{run_id}:{group_number or 'all'}",
                stage="dream",
                payload={"run_id": run_id, "group_number": group_number},
            )
        async with adapter._habit_execution_lock:
            result = her_dream.undo_dream_run(
                store=_store(runtime, adapter),
                journal=journal,
                run_id=run_id,
                group_number=group_number,
            )
        if callable(v2_audit):
            v2_audit(
                "dream_undo_completed",
                identity=str(result["undo_id"]),
                stage="dream",
                payload={
                    "run_id": run_id,
                    "undo_id": result["undo_id"],
                    "group_numbers": result["group_numbers"],
                },
            )
    except (ValueError, FileNotFoundError, her_dream.DreamUndoConflict) as exc:
        return False, f"Dream undo refused: {exc}"
    facts = [str(item) for item in result["report_facts"]]
    journal.append_audit(
        "dream_undo_persona_source",
        run_id=run_id,
        undo_id=result["undo_id"],
        report_type="dream_undo",
        **persona_source.audit_fields(),
    )
    try:
        report = await _persona_report(
            adapter,
            report_type="Dream Undo",
            report_id=str(result["undo_id"]),
            persona_source=persona_source,
            facts=facts,
            changed_group_numbers=[int(item) for item in result["group_numbers"]],
            undo_commands=[],
        )
        journal.append_audit(
            "dream_undo_persona_rendered",
            run_id=run_id,
            undo_id=result["undo_id"],
            report_type="dream_undo",
            renderer_attempted=True,
            renderer_succeeded=True,
            validation_outcome="delivered_without_content_validation",
            **persona_source.audit_fields(),
        )
    except Exception as exc:  # noqa: BLE001 - preserve the exact terminal failure
        journal.append_audit(
            "dream_undo_persona_unavailable",
            run_id=run_id,
            undo_id=result["undo_id"],
            report_type="dream_undo",
            renderer_attempted=persona_source.usable,
            renderer_succeeded=False,
            validation_outcome="renderer_unavailable",
            error=f"{type(exc).__name__}: {exc}"[:1_000],
            **persona_source.audit_fields(),
        )
        return False, str(exc)
    return True, report


async def invoke_scheduled(
    runtime: Any,
    *,
    task_id: str,
    scheduled_for: str | None = None,
) -> tuple[bool, str | None]:
    migration = migrate_legacy_schedule(runtime)
    origin = f"scheduled:{task_id}"
    if scheduled_for:
        origin += f":{scheduled_for}"
    ok, report, _manifest = await execute_dream(runtime, origin=origin)
    if (
        not migration.get("backend_is_her", True)
        and int(migration.get("legacy_enabled_count") or 0) > 0
    ):
        report = (
            "🌙 Legacy Dream schedule disabled\n\n"
            "This agent is not currently using HER, so the old generic Dream "
            "job was retired without reading dormant Habits. Switch to HER and "
            "use /dream on to enable native Habit maintenance."
        )
    await runtime.send_long_message(
        chat_id=runtime._primary_chat_id(),
        text=report,
        request_id=f"her-dream-{task_id}",
        purpose="her-dream-scheduled",
    )
    if not ok:
        runtime.error_logger.error("HER Dream job %s failed: %s", task_id, report)
    return ok, report


async def cmd_dream(
    runtime: Any,
    update: Any,
    context: Any,
    *,
    args_override: list[str] | None = None,
) -> None:
    user = getattr(update, "effective_user", None)
    checker = getattr(runtime, "_is_authorized_user", None)
    if callable(checker) and not checker(getattr(user, "id", None)):
        return
    migration = migrate_legacy_schedule(runtime)
    migration_notice = _legacy_migration_notice(migration)
    source_args = (
        args_override if args_override is not None else getattr(context, "args", None)
    )
    args = [str(item) for item in (source_args or [])]
    lowered = [item.casefold() for item in args]
    if not args or lowered == ["status"]:
        await _reply_view(
            runtime,
            update,
            _status_view(runtime, notice=migration_notice),
        )
        return
    if lowered == ["off"]:
        ok, message = _set_enabled(runtime, False)
        await _reply_view(
            runtime,
            update,
            _status_view(
                runtime, notice=("✅ " if ok else "❌ ") + html.escape(message)
            ),
        )
        return
    if lowered == ["on"]:
        if _her_adapter(runtime) is None:
            await _reply_view(
                runtime,
                update,
                _status_view(runtime, notice="❌ Switch to HER before enabling Dream."),
            )
            return
        ok, message = _set_enabled(runtime, True)
        await _reply_view(
            runtime,
            update,
            _status_view(
                runtime, notice=("✅ " if ok else "❌ ") + html.escape(message)
            ),
        )
        return
    if lowered[:1] == ["schedule"]:
        if _her_adapter(runtime) is None:
            await _reply_view(
                runtime,
                update,
                _status_view(
                    runtime, notice="❌ Switch to HER before scheduling Dream."
                ),
            )
            return
        try:
            schedule = compile_schedule(args[1:])
            _upsert_schedule(runtime, schedule, enabled=True)
            notice = f"✅ Dream schedule saved as <code>{html.escape(schedule)}</code>."
        except (ValueError, RuntimeError) as exc:
            notice = f"❌ {html.escape(str(exc))}"
        await _reply_view(runtime, update, _status_view(runtime, notice=notice))
        return
    if lowered == ["now"]:
        if _her_adapter(runtime) is not None:
            await runtime._reply_text(
                update,
                "🌙 Dream started. I will report every validated result here.",
            )
        _ok, report, _manifest = await execute_dream(runtime, origin="manual")
        await runtime._reply_text(update, report)
        return
    if lowered[:1] == ["undo"]:
        run_id = args[1] if len(args) >= 2 else None
        try:
            group_number = int(args[2]) if len(args) >= 3 else None
        except ValueError:
            await runtime._reply_text(update, "Dream change number must be an integer.")
            return
        if len(args) > 3:
            await runtime._reply_text(
                update, "Usage: /dream undo [run-id] [change-number]"
            )
            return
        _ok, report = await execute_undo(
            runtime,
            run_id=run_id,
            group_number=group_number,
        )
        await runtime._reply_text(update, report)
        return
    await _reply_view(
        runtime,
        update,
        _status_view(runtime, notice="⚠️ Unsupported Dream command syntax."),
    )


async def callback_dream(runtime: Any, update: Any, context: Any) -> None:
    query = update.callback_query
    checker = getattr(runtime, "_is_authorized_user", None)
    if callable(checker) and not checker(getattr(query.from_user, "id", None)):
        return
    command_allowed = getattr(runtime, "_is_command_allowed", None)
    if callable(command_allowed) and not command_allowed("dream"):
        await query.answer("/dream is disabled for this agent.", show_alert=True)
        return
    migration_notice = _legacy_migration_notice(migrate_legacy_schedule(runtime))
    parts = str(query.data or "").split(":")
    action = parts[1] if len(parts) > 1 else "status"
    if action == "status":
        text, markup = _status_view(runtime, notice=migration_notice)
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        await query.answer()
        return
    if action in {"on", "off"}:
        if action == "on" and _her_adapter(runtime) is None:
            await query.answer("Switch to HER before enabling Dream.", show_alert=True)
            return
        ok, message = _set_enabled(runtime, action == "on")
        text, markup = _status_view(
            runtime,
            notice=("✅ " if ok else "❌ ") + html.escape(message),
        )
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        await query.answer()
        return
    if action == "now":
        if _her_adapter(runtime) is None:
            await query.answer(
                "Dream is available only while this agent uses HER.",
                show_alert=True,
            )
            return
        await query.answer("Dream started.")
        _ok, report, _manifest = await execute_dream(runtime, origin="manual-callback")
        await runtime.send_long_message(
            query.message.chat_id,
            report,
            purpose="her_dream",
        )
        return
    if action == "undo" and len(parts) >= 4:
        run_id = parts[2]
        try:
            group_number = None if parts[3] == "all" else int(parts[3])
        except ValueError:
            await query.answer("Invalid Dream change number.", show_alert=True)
            return
        ok, report = await execute_undo(
            runtime,
            run_id=run_id,
            group_number=group_number,
        )
        await runtime.send_long_message(
            query.message.chat_id,
            report,
            purpose="her_dream_undo",
        )
        await query.answer(
            "Dream undo completed." if ok else "Dream undo refused.", show_alert=not ok
        )
        return
    await query.answer("Unsupported Dream action.", show_alert=True)
