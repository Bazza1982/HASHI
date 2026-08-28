from __future__ import annotations

import re
import uuid
from collections import deque
from datetime import datetime
from typing import Any

from orchestrator import ui_language


MAX_OCCURRENCE_SCAN = 10_000
MAX_STORED_DUE_TIMES = 100
HARD_MAX_REPLAY = 100
DEFAULT_MAX_REPLAY = 1
RECENT_RESOLVED_CONTEXT_SECONDS = 7 * 24 * 60 * 60


def collect_cron_occurrences(
    schedule: str,
    last_run_ts: float,
    now_dt: datetime,
    *,
    croniter_cls,
    fallback_missed_by_seconds: float | None = None,
) -> dict[str, Any]:
    """Return bounded occurrence evidence for a due cron window.

    Exact counts are retained up to ``MAX_OCCURRENCE_SCAN``.  Replay timestamps
    are deliberately bounded independently so a long outage cannot inflate the
    scheduler state or create an unbounded catch-up queue.
    """
    now_ts = now_dt.timestamp()
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
        base_dt = datetime.fromtimestamp(float(last_run_ts))
        iterator = croniter_cls(schedule, base_dt)
        while count < MAX_OCCURRENCE_SCAN:
            due_dt = iterator.get_next(datetime)
            if due_dt > now_dt:
                break
            due_ts = due_dt.timestamp()
            if first_due is None:
                first_due = due_ts
            last_due = due_ts
            latest.append(due_ts)
            count += 1
        if count == MAX_OCCURRENCE_SCAN:
            capped = iterator.get_next(datetime) <= now_dt
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
            reverse = croniter_cls(schedule, now_dt)
            while len(reverse_times) < MAX_STORED_DUE_TIMES:
                due_dt = reverse.get_prev(datetime)
                if due_dt.timestamp() < first_due:
                    break
                reverse_times.append(due_dt.timestamp())
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
    stamp = datetime.fromtimestamp(now_ts).strftime("%Y%m%d-%H%M%S")
    return f"recovery-{stamp}-{safe_agent}-{uuid.uuid4().hex[:6]}"


def format_local_time(timestamp: float | int | None) -> str:
    if timestamp is None:
        return "unknown"
    try:
        return datetime.fromtimestamp(float(timestamp)).astimezone().strftime("%Y-%m-%d %H:%M %Z")
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
    total_replayable = sum(replayable_count(item) for item in items)
    example_id = str(items[0].get("task_id") or "task-id") if items else "task-id"
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
            first=format_local_time(item.get("first_due_at")),
            last=format_local_time(item.get("last_due_at")),
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
            ui_language.tr("scheduler.choose", locale=selected),
            "1. "
            + ui_language.tr(
                "scheduler.run_all",
                locale=selected,
                count=total_replayable,
            ),
            "2. "
            + ui_language.tr(
                "scheduler.run_partial",
                locale=selected,
                example=example_id,
            ),
            "3. " + ui_language.tr("scheduler.skip_all", locale=selected),
            "",
            ui_language.tr("scheduler.safety", locale=selected),
        ]
    )
    return "\n".join(lines).strip()


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
        "Clear execution replies are handled by HASHI before they reach the agent. Use this context to answer questions about what was missed, what was run, and what remains.",
    ]
    if pending:
        lines.extend(["", "PENDING RECOVERY BATCHES"])
        for batch in sorted(pending, key=lambda value: float(value.get("created_at") or 0)):
            lines.append(
                f"- Batch {batch.get('batch_id')} · status={batch.get('status')} · notice already sent={batch.get('notice_status') == 'sent'}"
            )
            for item in batch.get("items") or []:
                due_times = ", ".join(format_local_time(value) for value in (item.get("due_at") or []))
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
                        f"    first_due={format_local_time(item.get('first_due_at'))}; last_due={format_local_time(item.get('last_due_at'))}",
                        f"    stored_due_times={due_times or 'none'}",
                    ]
                )
                lines.extend(item_lines)
        lines.extend(
            [
                "Accepted direct choices: 全部补跑 / run all; 全部跳过 / skip all; task_id=N; or 补跑 N 次 when only one task is pending.",
                "Never execute a pending recovery batch without an explicit user choice.",
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


def parse_reply(text: str, pending_batches: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not pending_batches:
        return None
    raw = " ".join(str(text or "").strip().split())
    if not raw or "?" in raw or "？" in raw:
        return None
    lowered = raw.casefold().strip(" .!。！")
    if lowered in {
        "1",
        "all",
        "run all",
        "run them all",
        "all run",
        "execute all",
        "execute them all",
        "all execute",
        "replay all",
        "do all",
        "do them all",
        "let's run all",
        "全部执行",
        "全部补跑",
        "全部都执行",
        "补跑全部",
        "执行全部",
        "全都补跑",
        "都执行",
        "全跑",
    }:
        return {"action": "all"}
    if re.fullmatch(
        r"(?:yes[, ]+)?(?:please\s+)?(?:let'?s\s+)?(?:run|execute|replay|do)\s+"
        r"(?:all|all\s+of\s+them|them\s+all)(?:\s+(?:missed\s+)?(?:runs?|turns?|jobs?))?",
        lowered,
        flags=re.IGNORECASE,
    ) or re.fullmatch(r"(?:请)?(?:把)?(?:全部|全都)(?:错过的)?(?:都)?(?:执行|补跑|跑)(?:一遍)?", lowered):
        return {"action": "all"}
    if lowered in {
        "3",
        "skip",
        "skip all",
        "skip them all",
        "全部跳过",
        "都跳过",
        "不用补跑",
        "不补跑",
    }:
        return {"action": "skip"}
    if re.fullmatch(
        r"(?:yes[, ]+)?(?:please\s+)?(?:skip|discard)\s+(?:all|all\s+of\s+them|them\s+all)",
        lowered,
        flags=re.IGNORECASE,
    ) or re.fullmatch(r"(?:请)?(?:把)?(?:全部|全都)(?:都)?(?:跳过|忽略)", lowered):
        return {"action": "skip"}
    if lowered == "2":
        return {"action": "help"}

    task_id_counts: dict[str, int] = {}
    for batch in pending_batches:
        for item in batch.get("items") or []:
            task_id = str(item.get("task_id") or "")
            if task_id:
                task_id_counts[task_id] = task_id_counts.get(task_id, 0) + 1
    known_ids = {
        task_id
        for task_id in task_id_counts
        if task_id
    }
    counts: dict[str, int] = {}
    for task_id in sorted(known_ids, key=len, reverse=True):
        match = re.search(rf"(?<![\w-]){re.escape(task_id)}\s*=\s*(\d+)", raw, flags=re.IGNORECASE)
        if match:
            if task_id_counts.get(task_id, 0) > 1:
                return {"action": "ambiguous"}
            counts[task_id] = int(match.group(1))
    if counts:
        return {"action": "partial", "counts": counts}

    if len(known_ids) == 1 and task_id_counts.get(next(iter(known_ids)), 0) == 1:
        task_id = next(iter(known_ids))
        patterns = (
            r"^(?:please\s+)?(?:run|execute|replay|do)\s+(?:the\s+)?(?:last|latest)?\s*(\d+)\s*(?:times?)?(?:\s+of\s+them)?$",
            r"^(?:只)?(?:补跑|执行|跑)\s*(?:最近)?\s*(\d+)\s*次?$",
            r"^(?:最近)\s*(\d+)\s*次$",
        )
        for pattern in patterns:
            match = re.match(pattern, lowered, flags=re.IGNORECASE)
            if match:
                return {"action": "partial", "counts": {task_id: int(match.group(1))}}
        if lowered == task_id.casefold():
            return {"action": "partial", "counts": {task_id: 1}}
    return None
