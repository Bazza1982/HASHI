from __future__ import annotations

import re
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any

from orchestrator import ui_language
from orchestrator.timezone_policy import (
    UTC_TIMEZONE_NAME,
    aware_in_timezone,
    canonical_timezone_name,
    format_epoch,
    resolve_local_wall_time,
    timezone_for_name,
    utc_datetime_from_epoch,
)


MAX_OCCURRENCE_SCAN = 10_000
MAX_STORED_DUE_TIMES = 100
HARD_MAX_REPLAY = 100
DEFAULT_MAX_REPLAY = 1
RECENT_RESOLVED_CONTEXT_SECONDS = 7 * 24 * 60 * 60
RECOVERY_CONVERSATION_SOURCE = "scheduler:recovery-conversation"
RECOVERY_CONVERSATION_SURFACE = "hashi.internal"
RECOVERY_CONVERSATION_CHANNEL = "scheduler-recovery"


def collect_cron_occurrences(
    schedule: str,
    last_run_ts: float,
    now_dt: datetime,
    *,
    croniter_cls,
    fallback_missed_by_seconds: float | None = None,
    timezone_name: str = UTC_TIMEZONE_NAME,
) -> dict[str, Any]:
    """Return bounded occurrence evidence for a due cron window.

    Exact counts are retained up to ``MAX_OCCURRENCE_SCAN``.  Replay timestamps
    are deliberately bounded independently so a long outage cannot inflate the
    scheduler state or create an unbounded catch-up queue.
    """
    zone_name = canonical_timezone_name(timezone_name)
    local_now = aware_in_timezone(now_dt, zone_name)
    now_ts = local_now.astimezone(timezone.utc).timestamp()
    if croniter_cls is None:
        if fallback_missed_by_seconds is None:
            return {}
        first_due = max(float(last_run_ts), now_ts - max(0.0, float(fallback_missed_by_seconds)))
        return {
            "missed_count": 1,
            "missed_count_capped": False,
            "first_due_at": first_due,
            "last_due_at": first_due,
            "due_at": [first_due],
            "missed_by_seconds": max(0.0, now_ts - first_due),
        }

    first_due: float | None = None
    last_due: float | None = None
    latest: deque[float] = deque(maxlen=MAX_STORED_DUE_TIMES)
    count = 0
    capped = False
    try:
        base_dt = utc_datetime_from_epoch(float(last_run_ts)).astimezone(
            timezone_for_name(zone_name)
        )
        iterator = croniter_cls(schedule, base_dt.replace(tzinfo=None))
        while count < MAX_OCCURRENCE_SCAN:
            wall_due = iterator.get_next(datetime)
            due_dt = resolve_local_wall_time(wall_due, zone_name)
            due_ts = due_dt.astimezone(timezone.utc).timestamp()
            if due_ts <= float(last_run_ts):
                # A fold=0 wall occurrence can precede a fold=1 base instant.
                # It is not a new due occurrence for this recovery window.
                continue
            if due_dt > local_now:
                break
            if first_due is None:
                first_due = due_ts
            last_due = due_ts
            latest.append(due_ts)
            count += 1
        if count == MAX_OCCURRENCE_SCAN:
            capped_wall = iterator.get_next(datetime)
            capped = resolve_local_wall_time(capped_wall, zone_name) <= local_now
    except (ValueError, KeyError, TypeError):
        count = 0

    # Preserve compatibility with callers/tests that identify a due cron via
    # _should_fire even when occurrence enumeration cannot represent it.
    if count == 0 and fallback_missed_by_seconds is not None:
        first_due = now_ts - max(0.0, float(fallback_missed_by_seconds))
        last_due = first_due
        latest.append(first_due)
        count = 1

    if count == 0 or first_due is None or last_due is None:
        return {}

    if capped:
        # The forward scan is capped, but recovery always needs the most recent
        # bounded timestamps.  Rebuild that tail backwards from now.
        reverse_times: list[float] = []
        try:
            reverse = croniter_cls(
                schedule,
                local_now.replace(tzinfo=None),
            )
            while len(reverse_times) < MAX_STORED_DUE_TIMES:
                wall_due = reverse.get_prev(datetime)
                due_dt = resolve_local_wall_time(wall_due, zone_name)
                due_ts = due_dt.astimezone(timezone.utc).timestamp()
                if due_ts < first_due:
                    break
                reverse_times.append(due_ts)
            latest = deque(reversed(reverse_times), maxlen=MAX_STORED_DUE_TIMES)
            if reverse_times:
                last_due = reverse_times[0]
        except (ValueError, KeyError, TypeError):
            pass

    return {
        "missed_count": count,
        "missed_count_capped": capped,
        "first_due_at": first_due,
        "last_due_at": last_due,
        "due_at": list(latest),
        "missed_by_seconds": max(0.0, now_ts - first_due),
    }


def collect_heartbeat_occurrences(last_run_ts: float, interval_seconds: int, now_ts: float) -> dict[str, Any]:
    interval = max(1, int(interval_seconds))
    elapsed = max(0.0, float(now_ts) - float(last_run_ts))
    count = max(1, int(elapsed // interval))
    first_due = float(last_run_ts) + interval
    last_due = float(last_run_ts) + count * interval
    stored_count = min(count, MAX_STORED_DUE_TIMES)
    stored_start = count - stored_count + 1
    due_at = [float(last_run_ts) + index * interval for index in range(stored_start, count + 1)]
    return {
        "missed_count": count,
        "missed_count_capped": False,
        "first_due_at": first_due,
        "last_due_at": last_due,
        "due_at": due_at,
        "missed_by_seconds": max(0.0, float(now_ts) - first_due),
    }


def recovery_limit(job: dict[str, Any], kind: str) -> int:
    recovery = job.get("recovery") if isinstance(job.get("recovery"), dict) else {}
    raw = recovery.get("max_replay", DEFAULT_MAX_REPLAY)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = DEFAULT_MAX_REPLAY
    # Heartbeats are state checks rather than wall-clock events.  They remain
    # coalesced unless the task explicitly opts into repeated recovery.
    if kind == "heartbeat" and "max_replay" not in recovery:
        value = 1
    return max(1, min(value, HARD_MAX_REPLAY))


def task_description(job: dict[str, Any], *, limit: int = 240) -> str:
    raw = str(job.get("note") or job.get("prompt") or job.get("args") or job.get("id") or "").strip()
    compact = " ".join(raw.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3].rstrip() + "..."


def new_batch_id(agent_name: str, now_ts: float) -> str:
    safe_agent = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(agent_name)).strip("-") or "agent"
    stamp = utc_datetime_from_epoch(now_ts).strftime("%Y%m%d-%H%M%S")
    return f"recovery-{stamp}-{safe_agent}-{uuid.uuid4().hex[:6]}"


def format_local_time(
    timestamp: float | int | None,
    *,
    timezone_name: str = UTC_TIMEZONE_NAME,
) -> str:
    if timestamp is None:
        return "unknown"
    try:
        return format_epoch(
            float(timestamp),
            timezone_name=timezone_name,
            timespec="minutes",
        )
    except (TypeError, ValueError, OSError):
        return "unknown"


def _count_label(item: dict[str, Any]) -> str:
    count = int(item.get("missed_count", 1) or 1)
    return f"{count}+" if item.get("missed_count_capped") else str(count)


def replayable_count(item: dict[str, Any]) -> int:
    return min(
        max(1, int(item.get("missed_count", 1) or 1)),
        max(1, int(item.get("replay_limit", DEFAULT_MAX_REPLAY) or DEFAULT_MAX_REPLAY)),
        len(item.get("due_at") or []) or 1,
    )


def render_notice(
    batch: dict[str, Any],
    *,
    locale: str | None = None,
) -> str:
    selected = ui_language.normalize_locale(locale or ui_language.DEFAULT_LOCALE)
    items = list(batch.get("items") or [])
    affected = len(items)
    total_missed = sum(int(item.get("missed_count", 1) or 1) for item in items)
    total_missed_label = f"{total_missed}+" if any(item.get("missed_count_capped") for item in items) else str(total_missed)
    separator = "：" if selected == "zh-CN" else ": "
    lines = [
        "⏰ " + ui_language.tr("scheduler.title", locale=selected),
        "",
        ui_language.tr(
            "scheduler.summary",
            locale=selected,
            affected=affected,
            missed=total_missed_label,
        ),
        ui_language.tr(
            "scheduler.batch",
            locale=selected,
            batch_id=batch.get("batch_id", "?"),
        ),
        "",
    ]
    for item in items:
        task_id = item.get("task_id", "?")
        kind = item.get("kind", "job")
        timezone_name = canonical_timezone_name(
            item.get("timezone") or UTC_TIMEZONE_NAME
        )
        if kind == "cron":
            schedule_text = f"cron {item.get('schedule', '?')}"
        else:
            schedule_text = ui_language.tr(
                "scheduler.every_seconds",
                locale=selected,
                seconds=int(item.get("interval_seconds", 0) or 0),
            )
        replay_count = replayable_count(item)
        missed_value = ui_language.tr(
            "scheduler.missed_value",
            locale=selected,
            count=_count_label(item),
            first=format_local_time(
                item.get("first_due_at"), timezone_name=timezone_name
            ),
            last=format_local_time(
                item.get("last_due_at"), timezone_name=timezone_name
            ),
        )
        lines.extend(
            [
                f"• {task_id}",
                f"  {ui_language.tr('scheduler.content', locale=selected)}{separator}"
                f"{item.get('description') or task_id}",
                f"  {ui_language.tr('scheduler.schedule', locale=selected)}{separator}{schedule_text}",
                f"  {ui_language.tr('scheduler.missed', locale=selected)}{separator}{missed_value}",
                f"  {ui_language.tr('scheduler.replay_limit', locale=selected)}{separator}"
                + ui_language.tr(
                    "scheduler.replay_value",
                    locale=selected,
                    count=replay_count,
                ),
                "",
            ]
        )
    lines.extend(
        [
            ui_language.tr("scheduler.natural_reply", locale=selected),
            "",
            ui_language.tr("scheduler.safety", locale=selected),
        ]
    )
    return "\n".join(lines).strip()


def render_agent_request(
    batch: dict[str, Any],
    *,
    locale: str | None = None,
) -> str:
    """Build the internal Run that asks the Agent to discuss one recovery batch."""

    notice = render_notice(batch, locale=locale)
    return (
        "[HASHI Scheduler recovery conversation]\n"
        "A durable missed-trigger recovery batch now needs the user's decision.\n\n"
        f"{notice}\n\n"
        "Ask the user what they want to do and answer any questions about what was "
        "missed. The user's later reply will arrive as ordinary conversation through "
        "the Frontend Connector. Interpret that reply naturally; do not require menu "
        "numbers, keywords, or exact phrases.\n"
        "Do not resolve, skip, or rerun this batch in this turn. Wait for a later "
        "human/client reply. When that reply is unambiguous, use the typed "
        "hashi_scheduler_recovery_resolve tool for the exact batch. If it is "
        "ambiguous, ask a clarifying question instead."
    )


def render_context(batches: list[dict[str, Any]], *, now_ts: float) -> str:
    pending = [batch for batch in batches if batch.get("status") in {"pending", "running"}]
    recent = [
        batch
        for batch in batches
        if batch.get("status") not in {"pending", "running"}
        and now_ts - float(batch.get("resolved_at") or batch.get("created_at") or 0) <= RECENT_RESOLVED_CONTEXT_SECONDS
    ]
    if not pending and not recent:
        return ""

    lines = [
        "HASHI maintains this scheduler-recovery context directly. Do not search logs for these facts.",
        "The user's reply is ordinary conversation delivered through the Frontend Connector. Use this context to answer questions about what was missed, what was run, and what remains.",
    ]
    if pending:
        lines.extend(["", "PENDING RECOVERY BATCHES"])
        for batch in sorted(pending, key=lambda value: float(value.get("created_at") or 0)):
            lines.append(
                f"- Batch {batch.get('batch_id')} · status={batch.get('status')} · notice already sent={batch.get('notice_status') == 'sent'}"
            )
            for item in batch.get("items") or []:
                timezone_name = canonical_timezone_name(
                    item.get("timezone") or UTC_TIMEZONE_NAME
                )
                due_times = ", ".join(
                    format_local_time(value, timezone_name=timezone_name)
                    for value in (item.get("due_at") or [])
                )
                description = str(item.get("description") or item.get("task_id"))
                item_lines = [
                    f"  - task_id={item.get('task_id')} kind={item.get('kind')} missed_count={_count_label(item)} replayable_count={replayable_count(item)}",
                    f"    purpose={description}",
                ]
                prompt_excerpt = str(item.get("prompt_excerpt") or "")
                if prompt_excerpt and prompt_excerpt != description:
                    item_lines.append(f"    task_prompt={prompt_excerpt}")
                item_lines.extend(
                    [
                        f"    first_due={format_local_time(item.get('first_due_at'), timezone_name=timezone_name)}; last_due={format_local_time(item.get('last_due_at'), timezone_name=timezone_name)}",
                        f"    stored_due_times={due_times or 'none'}",
                    ]
                )
                lines.extend(item_lines)
        lines.extend(
            [
                "Interpret the user's natural-language intent in context; never use a keyword table or menu-token parser.",
                "If the user clearly chooses an action, call hashi_scheduler_recovery_resolve for each exact batch. If they only ask a question or remain ambiguous, answer or clarify without resolving anything.",
            ]
        )
    if recent:
        lines.extend(["", "RECENTLY RESOLVED RECOVERY BATCHES"])
        for batch in sorted(recent, key=lambda value: float(value.get("resolved_at") or 0))[-3:]:
            resolution = batch.get("resolution") or {}
            lines.append(
                f"- Batch {batch.get('batch_id')} · action={resolution.get('action', batch.get('status'))} · "
                f"executed={resolution.get('executed_total', 0)} · skipped={resolution.get('skipped_total', 0)} · "
                f"resolved_at={format_local_time(batch.get('resolved_at'))}"
            )
            for item in batch.get("items") or []:
                result = (resolution.get("items") or {}).get(str(item.get("task_id")), {})
                lines.append(
                    f"  - task_id={item.get('task_id')} missed={_count_label(item)} executed={result.get('executed', 0)} skipped={result.get('skipped', item.get('missed_count', 1))}"
                )
    return "\n".join(lines).strip()
