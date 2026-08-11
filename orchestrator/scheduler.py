import asyncio
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from orchestrator.job_ownership import ownership_mismatch_label
from orchestrator import scheduler_recovery
from orchestrator.superloop_scheduler import advance_superloops_once

scheduler_logger = logging.getLogger("BridgeU.Scheduler")

SCHEDULER_JOB_TIMEOUT_S = 30
SCHEDULER_SKILL_TIMEOUT_S = 1860  # Keep longer than the action-skill watchdog so the skill layer owns timeout/cleanup.
PARKED_FOLLOWUP_TIMEOUT_S = 15
CRON_CATCHUP_THRESHOLD_S = 3600
LEGACY_RECOVERY_MIGRATION_MAX_AGE_S = 24 * 60 * 60

try:
    from croniter import croniter
    HAS_CRONITER = True
except ImportError:
    HAS_CRONITER = False
    print("[Scheduler] croniter not installed — using HH:MM fallback (this is fine)")


def _time_to_cron(hm: str) -> str:
    """Convert legacy 'HH:MM' time string to a cron expression '0 H * * *' or 'M H * * *'."""
    parts = hm.strip().split(":")
    if len(parts) == 2:
        hour = parts[0].lstrip("0") or "0"
        minute = parts[1].lstrip("0") or "0"
        return f"{minute} {hour} * * *"
    return hm  # already a cron expression or unrecognised — pass through


def _resolve_schedule(task: dict) -> str | None:
    """Return a cron expression for a task, supporting both new 'schedule' and legacy 'time' fields."""
    schedule = task.get("schedule")
    if schedule:
        return schedule.strip()
    legacy_time = task.get("time")
    if legacy_time:
        return _time_to_cron(legacy_time)
    return None


def _fallback_supports_schedule(schedule: str) -> bool:
    """Return True only for fixed HH:MM-style daily cron schedules.

    In fallback mode we intentionally support a very small subset:
    `M H * * *` where both minute and hour are literal integers.
    Interval-style cron such as `*/15 * * * *` must use heartbeats instead.
    """
    parts = schedule.split()
    if len(parts) != 5 or parts[2] != "*" or parts[3] != "*" or parts[4] != "*":
        return False
    try:
        int(parts[0])
        int(parts[1])
        return True
    except (TypeError, ValueError):
        return False


def _should_fire(schedule: str, last_run_ts: float, now_dt: datetime) -> float | None:
    """Check whether *schedule* has a fire time between *last_run_ts* (exclusive) and *now_dt* (inclusive).

    Return the number of seconds the task is late when a fire time is due.
    Return None when there is no due fire time.

    Uses croniter to iterate forward from last_run. If any scheduled time falls within
    (last_run, now], the task should fire.
    """
    if not HAS_CRONITER:
        # Graceful fallback: match HH:MM only (legacy behaviour).
        # This handles simple "M H * * *" patterns.
        parts = schedule.split()
        if len(parts) == 5 and parts[2] == "*" and parts[3] == "*" and parts[4] == "*":
            try:
                minute = int(parts[0])
                hour = int(parts[1])
                current_hm = now_dt.strftime("%H:%M")
                target_hm = f"{hour:02d}:{minute:02d}"
                if current_hm != target_hm:
                    return None
                # Ensure not already fired today.
                # If never run (last_run_ts=0), use today's date so it does NOT
                # fire immediately — it waits for the next scheduled occurrence.
                last_dt = datetime.fromtimestamp(last_run_ts) if last_run_ts else now_dt
                if last_dt.date() < now_dt.date():
                    return 0.0
                return None
            except (ValueError, TypeError):
                return None
        return None

    try:
        # If last_run_ts is 0 (never run), use now_dt as the base so the next
        # scheduled time is calculated forward from *now*, not from year 2000.
        # This prevents new cron jobs from firing immediately on first scheduler tick.
        last_dt = datetime.fromtimestamp(last_run_ts) if last_run_ts else now_dt
        cron = croniter(schedule, last_dt)
        next_fire = cron.get_next(datetime)
        if next_fire <= now_dt:
            return (now_dt - next_fire).total_seconds()
        return None
    except (ValueError, KeyError) as e:
        scheduler_logger.error(f"Invalid cron expression '{schedule}': {e}")
        return None


def _runtime_busy(runtime) -> bool:
    checker = getattr(runtime, "_backend_busy", None)
    if callable(checker):
        try:
            return bool(checker())
        except Exception:
            pass
    queue = getattr(runtime, "queue", None)
    queue_busy = bool(queue is not None and hasattr(queue, "empty") and not queue.empty())
    return bool(getattr(runtime, "is_generating", False) or queue_busy)


class TaskScheduler:
    def __init__(
        self,
        tasks_path: Path,
        state_path: Path,
        runtimes: list | None,
        authorized_id: int,
        skill_manager=None,
        orchestrator=None,
        enterprise_lease_store=None,
        enterprise_lease_name: str = "superloop-scheduler",
        enterprise_lease_holder: str = "local",
        enterprise_lease_ttl_seconds: int = 60,
    ):
        self.tasks_path = tasks_path
        self.state_path = state_path
        self.active_heartbeats_path = tasks_path.parent / "managed_active_heartbeats.json"
        self.runtimes = {rt.name: rt for rt in (runtimes or [])}
        self.authorized_id = authorized_id
        self.skill_manager = skill_manager
        self.orchestrator = orchestrator
        self.enterprise_lease_store = enterprise_lease_store
        self.enterprise_lease_name = enterprise_lease_name
        self.enterprise_lease_holder = enterprise_lease_holder
        self.enterprise_lease_ttl_seconds = enterprise_lease_ttl_seconds
        self.state = self._load_state()
        self.state.setdefault("heartbeats", {})
        self.state.setdefault("crons", {})
        self.state.setdefault("nudges", {})
        self.state.setdefault("missed_crons", {})
        self.state.setdefault("missed_heartbeats", {})
        self.state.setdefault("recovery_batches", {})
        self._recovery_lock = asyncio.Lock()
        if self._prepare_recovery_state():
            self._save_state()
        # Only the first successful scheduler pass is downtime recovery. Later
        # ticks must keep normal due jobs independent, even when several share
        # the same interval or cron boundary.
        self._startup_recovery_pending = True

    def _runtime_map(self):
        if self.orchestrator is not None:
            return {rt.name: rt for rt in getattr(self.orchestrator, "runtimes", []) if getattr(rt, "startup_success", False)}
        return dict(self.runtimes)

    def _load_state(self):
        if self.state_path.exists():
            try:
                with open(self.state_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                scheduler_logger.error(f"Failed to load state: {e}")
        return {
            "heartbeats": {},
            "crons": {},
            "nudges": {},
            "missed_crons": {},
            "missed_heartbeats": {},
            "recovery_batches": {},
        }

    def _save_state(self):
        try:
            with open(self.state_path, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=2)
        except Exception as e:
            scheduler_logger.error(f"Failed to save state: {e}")

    def _prepare_recovery_state(self) -> bool:
        """Repair interrupted batches and migrate recent pre-batch notices."""
        changed = False
        batches = self.state.setdefault("recovery_batches", {})
        for batch in batches.values():
            if not isinstance(batch, dict):
                continue
            if batch.get("status") == "running":
                batch["status"] = "pending"
                batch["interrupted_while_running"] = True
                changed = True

        represented = {
            (str(batch.get("agent")), str(item.get("kind")), str(item.get("task_id")))
            for batch in batches.values()
            if isinstance(batch, dict) and batch.get("status") in {"pending", "running"}
            for item in (batch.get("items") or [])
            if isinstance(item, dict)
        }
        now = time.time()
        tasks = self._load_tasks()
        task_lookup = {
            (kind, str(job.get("id"))): job
            for kind, key in (("cron", "crons"), ("heartbeat", "heartbeats"))
            for job in tasks.get(key, [])
            if isinstance(job, dict) and job.get("id")
        }
        migrated_by_agent: dict[str, list[dict[str, Any]]] = {}
        migrated_notice_time: dict[str, float] = {}
        for kind, state_key in (("cron", "missed_crons"), ("heartbeat", "missed_heartbeats")):
            records = self.state.get(state_key) or {}
            if not isinstance(records, dict):
                continue
            for task_id, record in records.items():
                if not isinstance(record, dict):
                    continue
                agent_name = str(record.get("agent") or "")
                noticed_at = float(record.get("noticed_at") or 0)
                if (
                    not agent_name
                    or not noticed_at
                    or now - noticed_at > LEGACY_RECOVERY_MIGRATION_MAX_AGE_S
                    or (agent_name, kind, str(task_id)) in represented
                ):
                    continue
                job = task_lookup.get((kind, str(task_id)))
                if not job:
                    continue
                missed_by = max(0.0, float(record.get("missed_by_seconds") or 0))
                if kind == "cron":
                    schedule = str(record.get("schedule") or _resolve_schedule(job) or "")
                    first_due = noticed_at - missed_by
                    occurrences = scheduler_recovery.collect_cron_occurrences(
                        schedule,
                        first_due - 1.0,
                        datetime.fromtimestamp(noticed_at),
                        croniter_cls=croniter if HAS_CRONITER else None,
                        fallback_missed_by_seconds=missed_by,
                    )
                else:
                    interval = int(record.get("interval_seconds") or job.get("interval_seconds") or 1)
                    last_run = noticed_at - missed_by - interval
                    occurrences = scheduler_recovery.collect_heartbeat_occurrences(last_run, interval, noticed_at)
                migrated_by_agent.setdefault(agent_name, []).append(
                    self._build_recovery_item(job, kind=kind, occurrences=occurrences)
                )
                migrated_notice_time[agent_name] = max(migrated_notice_time.get(agent_name, 0), noticed_at)

        for agent_name, items in migrated_by_agent.items():
            noticed_at = migrated_notice_time[agent_name]
            batch_id = scheduler_recovery.new_batch_id(agent_name, noticed_at)
            batch = {
                "batch_id": batch_id,
                "agent": agent_name,
                "status": "pending",
                "created_at": noticed_at,
                "notice_status": "sent",
                "notified_at": noticed_at,
                "legacy_migrated": True,
                "items": items,
            }
            batch["notice_text"] = scheduler_recovery.render_notice(batch)
            batches[batch_id] = batch
            changed = True
            scheduler_logger.info(
                "Migrated legacy scheduler recovery state for %s into batch %s (%s task(s)).",
                agent_name,
                batch_id,
                len(items),
            )
        return changed

    def _build_recovery_item(
        self,
        job: dict[str, Any],
        *,
        kind: str,
        occurrences: dict[str, Any],
    ) -> dict[str, Any]:
        occurrence_fields = {
            key: occurrences.get(key)
            for key in (
                "missed_count",
                "missed_count_capped",
                "first_due_at",
                "last_due_at",
                "due_at",
                "missed_by_seconds",
            )
        }
        item = {
            "task_id": str(job.get("id") or "?"),
            "kind": kind,
            "agent": str(job.get("agent") or ""),
            "action": str(job.get("action") or "enqueue_prompt"),
            "description": scheduler_recovery.task_description(job),
            "prompt_excerpt": scheduler_recovery.task_description(
                {"prompt": job.get("prompt") or job.get("args") or ""},
                limit=800,
            ),
            "replay_limit": scheduler_recovery.recovery_limit(job, kind),
            **occurrence_fields,
        }
        if kind == "cron":
            item["schedule"] = _resolve_schedule(job)
        else:
            item["interval_seconds"] = int(job.get("interval_seconds") or 0)
        return item

    def _load_tasks(self):
        # Mind the gap:
        # - Heartbeats are for interval loops ("every 10 minutes until done").
        # - Crons are only for fixed wall-clock schedules.
        # We validate this when loading tasks so unsupported interval crons do
        # not silently seed and then never fire in fallback mode.
        def is_managed_active_heartbeat(job: dict) -> bool:
            return (
                isinstance(job, dict)
                and (
                    job.get("managed_by") == "active-command"
                    or str(job.get("id", "")).endswith("-active-heartbeat")
                )
            )

        if not self.tasks_path.exists():
            tasks = {"heartbeats": [], "crons": [], "nudges": []}
        else:
            try:
                with open(self.tasks_path, "r", encoding="utf-8") as f:
                    tasks = json.load(f)
            except Exception as e:
                scheduler_logger.error(f"Failed to load tasks: {e}")
                tasks = {"heartbeats": [], "crons": [], "nudges": []}
        tasks.setdefault("heartbeats", [])
        tasks.setdefault("crons", [])
        tasks.setdefault("nudges", [])

        valid_crons = []
        for job in tasks.get("crons", []):
            schedule = _resolve_schedule(job)
            if not schedule:
                valid_crons.append(job)
                continue
            if HAS_CRONITER or _fallback_supports_schedule(schedule):
                valid_crons.append(job)
                continue
            scheduler_logger.error(
                "Rejecting cron %s for agent %s: fallback mode only supports fixed daily HH:MM schedules. "
                "Use a heartbeat for interval loops such as every 10 or 15 minutes. Unsupported schedule: %s",
                job.get("id", "<unknown>"),
                job.get("agent", "<unknown>"),
                schedule,
            )
        tasks["crons"] = valid_crons

        heartbeats = [
            hb for hb in tasks.get("heartbeats", [])
            if not is_managed_active_heartbeat(hb)
        ]
        if self.active_heartbeats_path.exists():
            try:
                with open(self.active_heartbeats_path, "r", encoding="utf-8") as f:
                    payload = json.load(f)
                managed = payload if isinstance(payload, list) else payload.get("heartbeats", [])
                heartbeats.extend(
                    hb for hb in managed
                    if isinstance(hb, dict) and is_managed_active_heartbeat(hb)
                )
            except Exception as e:
                scheduler_logger.error(f"Failed to load managed active heartbeats: {e}")

        tasks["heartbeats"] = heartbeats
        return tasks

    def _save_tasks(self, tasks: dict):
        try:
            with open(self.tasks_path, "w", encoding="utf-8") as f:
                json.dump(tasks, f, indent=2, ensure_ascii=False)
        except Exception as e:
            scheduler_logger.error(f"Failed to save tasks: {e}")

    def _get_cron_last_run(self, task_id: str) -> float:
        """Get last run timestamp for a cron task, handling both old date-string and new timestamp formats."""
        raw = self.state["crons"].get(task_id)
        if raw is None:
            return 0.0
        if isinstance(raw, (int, float)):
            return float(raw)
        # Legacy: stored as "YYYY-MM-DD" string — convert to midnight timestamp
        if isinstance(raw, str):
            try:
                dt = datetime.strptime(raw, "%Y-%m-%d")
                return dt.timestamp()
            except ValueError:
                return 0.0
        return 0.0

    def _disable_nudge(self, task_id: str, *, reason: str) -> bool:
        tasks = self._load_tasks()
        changed = False
        for job in tasks.get("nudges", []):
            if job.get("id") != task_id:
                continue
            job["enabled"] = False
            meta = job.setdefault("nudge_meta", {})
            meta["stopped_reason"] = reason
            changed = True
            break
        if changed:
            self._save_tasks(tasks)
        return changed

    def _register_nudge_completion_listener(self, runtime, task_id: str, request_id: str | None) -> None:
        if not request_id:
            return
        register = getattr(runtime, "register_request_listener", None)
        if not callable(register):
            return

        marker = f"NUDGE_COMPLETE:{task_id}"

        def _on_result(payload: dict) -> None:
            text = str((payload or {}).get("text") or "")
            if not any(line.strip() == marker for line in text.splitlines()):
                return
            if self._disable_nudge(task_id, reason="exit_condition_met"):
                scheduler_logger.info("Nudge %s completed by response marker.", task_id)

        register(request_id, _on_result)

    async def _run_scheduler_action(self, action_coro, *, task_kind: str, task_id: str, agent_name: str, timeout_s: int = SCHEDULER_JOB_TIMEOUT_S) -> bool:
        try:
            await asyncio.wait_for(action_coro, timeout=timeout_s)
            return True
        except asyncio.TimeoutError:
            scheduler_logger.error(
                f"{task_kind} {task_id} for {agent_name} timed out after {timeout_s}s; scheduler will continue."
            )
            return False
        except asyncio.CancelledError:
            raise
        except Exception as e:
            scheduler_logger.error(
                f"{task_kind} {task_id} for {agent_name} failed: {e}",
                exc_info=True,
            )
            return False

    def _acquire_enterprise_scheduler_lease(self) -> bool:
        if self.enterprise_lease_store is None:
            return True
        try:
            attempt = self.enterprise_lease_store.acquire(
                self.enterprise_lease_name,
                holder_id=self.enterprise_lease_holder,
                ttl_seconds=self.enterprise_lease_ttl_seconds,
                metadata={"component": "task-scheduler"},
            )
        except Exception as e:
            scheduler_logger.error("Enterprise scheduler lease acquire failed: %s", e, exc_info=True)
            return False
        if not attempt.acquired:
            scheduler_logger.info(
                "Skipping scheduler tick because enterprise lease %s is held by %s.",
                self.enterprise_lease_name,
                attempt.current_holder_id,
            )
            return False
        return True

    def _release_enterprise_scheduler_lease(self) -> None:
        if self.enterprise_lease_store is None:
            return
        try:
            self.enterprise_lease_store.release(
                self.enterprise_lease_name,
                holder_id=self.enterprise_lease_holder,
            )
        except Exception as e:
            scheduler_logger.error("Enterprise scheduler lease release failed: %s", e, exc_info=True)

    def _job_owner_mismatch(self, job: dict, *, task_kind: str, task_id: str, agent_name: str) -> str | None:
        label = ownership_mismatch_label(job)
        if not label:
            return None
        scheduler_logger.error(
            "Blocking %s %s for %s: %s. Review the task owner before enabling it.",
            task_kind,
            task_id,
            agent_name,
            label,
        )
        return label

    def _create_recovery_batch(
        self,
        *,
        agent_name: str,
        items: list[dict[str, Any]],
        now_ts: float | None = None,
    ) -> dict[str, Any]:
        created_at = float(now_ts if now_ts is not None else time.time())
        batch_id = scheduler_recovery.new_batch_id(agent_name, created_at)
        serialized_items = [
            self._build_recovery_item(
                item["job"],
                kind=str(item.get("kind") or "job"),
                occurrences=item,
            )
            for item in items
        ]
        batch = {
            "batch_id": batch_id,
            "agent": agent_name,
            "status": "pending",
            "created_at": created_at,
            "notice_status": "pending",
            "items": serialized_items,
        }
        batch["notice_text"] = scheduler_recovery.render_notice(batch)
        self.state.setdefault("recovery_batches", {})[batch_id] = batch
        # Persist before delivery so a crash after sending cannot lose the
        # actionable context or create a duplicate logical batch.
        self._save_state()
        return batch

    async def _deliver_recovery_notice(self, runtime, batch: dict[str, Any]) -> bool:
        sender = getattr(runtime, "send_long_message", None)
        if not callable(sender):
            scheduler_logger.error(
                "Cannot deliver scheduler recovery batch %s: runtime %s has no direct sender.",
                batch.get("batch_id"),
                batch.get("agent"),
            )
            return False
        try:
            result = await asyncio.wait_for(
                sender(
                    chat_id=self.authorized_id,
                    text=str(batch.get("notice_text") or scheduler_recovery.render_notice(batch)),
                    request_id=f"scheduler-{batch.get('batch_id')}",
                    purpose="scheduler-recovery",
                ),
                timeout=SCHEDULER_JOB_TIMEOUT_S,
            )
            delivered = not (
                isinstance(result, tuple)
                and len(result) >= 2
                and int(result[1] or 0) == 0
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            delivered = False
            scheduler_logger.error(
                "Direct scheduler recovery notice %s failed: %s",
                batch.get("batch_id"),
                exc,
                exc_info=True,
            )
        batch["notice_attempted_at"] = time.time()
        if delivered:
            batch["notice_status"] = "sent"
            batch["notified_at"] = time.time()
        else:
            batch["notice_status"] = "retry"
        self._save_state()
        return delivered

    async def _notify_missed_jobs_grouped(
        self,
        *,
        runtime_map: dict,
        agent_name: str,
        items: list[dict],
    ) -> dict[str, Any] | None:
        if not items:
            return None
        rt = runtime_map.get(agent_name)
        if rt is None:
            return None
        batch = self._create_recovery_batch(agent_name=agent_name, items=items)
        await self._deliver_recovery_notice(rt, batch)
        return batch

    def _agent_recovery_batches(self, agent_name: str) -> list[dict[str, Any]]:
        return [
            batch
            for batch in (self.state.get("recovery_batches") or {}).values()
            if isinstance(batch, dict) and batch.get("agent") == agent_name
        ]

    def build_recovery_context(self, agent_name: str) -> str:
        return scheduler_recovery.render_context(
            self._agent_recovery_batches(agent_name),
            now_ts=time.time(),
        )

    async def _retry_pending_recovery_notices(
        self,
        runtime_map: dict[str, Any],
        *,
        now_ts: float,
    ) -> None:
        for batch in (self.state.get("recovery_batches") or {}).values():
            if not isinstance(batch, dict) or batch.get("notice_status") == "sent":
                continue
            last_attempt = float(batch.get("notice_attempted_at") or 0)
            if now_ts - last_attempt < 60:
                continue
            runtime = runtime_map.get(str(batch.get("agent") or ""))
            if runtime is not None:
                await self._deliver_recovery_notice(runtime, batch)

    @staticmethod
    def _find_job(tasks: dict[str, Any], item: dict[str, Any]) -> dict[str, Any] | None:
        key = "crons" if item.get("kind") == "cron" else "heartbeats"
        for job in tasks.get(key, []):
            if str(job.get("id")) == str(item.get("task_id")):
                return job
        return None

    async def _resolve_recovery_batch(
        self,
        batch: dict[str, Any],
        *,
        action: str,
        counts: dict[str, int] | None,
        runtime_map: dict[str, Any],
    ) -> dict[str, Any]:
        if batch.get("status") not in {"pending", "running"}:
            return dict(batch.get("resolution") or {})
        tasks = self._load_tasks()
        batch["status"] = "running"
        batch["resolution_requested_at"] = time.time()
        batch["requested_action"] = action
        self._save_state()

        resolution_items: dict[str, dict[str, Any]] = {}
        executed_total = 0
        failed_total = 0
        missed_total = sum(int(item.get("missed_count", 1) or 1) for item in batch.get("items") or [])
        for item in batch.get("items") or []:
            task_id = str(item.get("task_id"))
            replayable = scheduler_recovery.replayable_count(item)
            requested = 0
            if action == "all":
                requested = replayable
            elif action == "partial":
                requested = max(0, int((counts or {}).get(task_id, 0)))
                requested = min(requested, replayable)

            executed = 0
            failed = 0
            job = self._find_job(tasks, item) if requested else None
            if requested and (
                job is None
                or not job.get("enabled", False)
                or self._job_owner_mismatch(
                    job,
                    task_kind="Scheduler recovery",
                    task_id=task_id,
                    agent_name=str(batch.get("agent") or ""),
                )
            ):
                failed = requested
            elif requested and job is not None:
                due_at = list(item.get("due_at") or [])[-requested:]
                if not due_at:
                    due_at = [float(item.get("last_due_at") or time.time())] * requested
                for due_ts in due_at:
                    scheduled_for = datetime.fromtimestamp(float(due_ts))
                    if item.get("kind") == "heartbeat":
                        ok = await self._fire_heartbeat_job(
                            job,
                            runtime_map=runtime_map,
                            scheduled_for=scheduled_for,
                            recovery_batch_id=str(batch.get("batch_id")),
                        )
                    else:
                        ok = await self._fire_cron_job(
                            job,
                            runtime_map=runtime_map,
                            tasks=tasks,
                            now_dt=scheduled_for,
                            scheduled_for=scheduled_for,
                            recovery_batch_id=str(batch.get("batch_id")),
                        )
                    if ok:
                        executed += 1
                    else:
                        failed += 1
                    item["recovery_executed"] = int(item.get("recovery_executed", 0)) + int(ok)
                    self._save_state()
            executed_total += executed
            failed_total += failed
            resolution_items[task_id] = {
                "requested": requested,
                "executed": executed,
                "failed": failed,
                "skipped": max(0, int(item.get("missed_count", 1) or 1) - executed),
            }

        resolution = {
            "action": action,
            "executed_total": executed_total,
            "failed_total": failed_total,
            "skipped_total": max(0, missed_total - executed_total),
            "items": resolution_items,
        }
        batch["resolution"] = resolution
        batch["status"] = "resolved" if not failed_total else "resolved_with_errors"
        batch["resolved_at"] = time.time()
        self._save_state()
        return resolution

    async def handle_recovery_reply(
        self,
        *,
        agent_name: str,
        text: str,
        runtime_map: dict[str, Any],
    ) -> str | None:
        pending = [
            batch
            for batch in self._agent_recovery_batches(agent_name)
            if batch.get("status") in {"pending", "running"}
        ]
        parsed = scheduler_recovery.parse_reply(text, pending)
        if parsed is None:
            return None
        if parsed.get("action") == "help":
            return (
                "请回复“任务ID=次数”，例如 task-id=3。次数表示选择最近 N 次，"
                "执行时仍按原计划时间从早到晚排列。"
            )
        if parsed.get("action") == "ambiguous":
            return "同一任务存在多个待处理恢复批次，请先回复“全部补跑”或“全部跳过”；我不会猜测执行范围。"

        async with self._recovery_lock:
            executed_total = 0
            failed_total = 0
            skipped_total = 0
            for batch in pending:
                resolution = await self._resolve_recovery_batch(
                    batch,
                    action=str(parsed.get("action")),
                    counts=dict(parsed.get("counts") or {}),
                    runtime_map=runtime_map,
                )
                executed_total += int(resolution.get("executed_total", 0))
                failed_total += int(resolution.get("failed_total", 0))
                skipped_total += int(resolution.get("skipped_total", 0))
        if parsed.get("action") == "skip":
            return f"✅ 已跳过 {len(pending)} 个恢复批次，共 {skipped_total} 次错过触发；原计划不受影响。"
        suffix = f"，失败 {failed_total} 次" if failed_total else ""
        return (
            f"✅ 已处理 {len(pending)} 个恢复批次：补跑 {executed_total} 次，"
            f"跳过 {skipped_total} 次{suffix}；原计划不受影响。"
        )

    async def _fire_heartbeat_job(
        self,
        hb: dict,
        *,
        runtime_map: dict,
        scheduled_for: datetime | None = None,
        recovery_batch_id: str | None = None,
    ) -> bool:
        """Run one due heartbeat. Returns True when last_run should advance."""
        task_id = hb["id"]
        agent_name = hb["agent"]
        prompt = hb.get("prompt", "")
        action = hb.get("action", "enqueue_prompt")
        rt = runtime_map[agent_name]
        scheduler_logger.info(f"Triggering heartbeat {task_id} for {agent_name}")
        recovery_header = ""
        if scheduled_for is not None:
            recovery_header = (
                "[HASHI scheduler recovery]\n"
                f"This is a missed occurrence originally due at {scheduled_for.astimezone().isoformat(timespec='minutes')}.\n"
                f"Recovery batch: {recovery_batch_id or 'unknown'}\n\n"
            )
        if action.startswith("skill:"):
            skill_id = action.split(":", 1)[1]
            args = recovery_header + (hb.get("args", "") or prompt)
            return await self._run_scheduler_action(
                rt.invoke_scheduler_skill(
                    skill_id=skill_id,
                    args=args,
                    task_id=task_id,
                ),
                task_kind="Heartbeat",
                task_id=task_id,
                agent_name=agent_name,
                timeout_s=SCHEDULER_SKILL_TIMEOUT_S,
            )
        return await self._run_scheduler_action(
            rt.enqueue_request(
                chat_id=self.authorized_id,
                prompt=recovery_header + prompt,
                source="scheduler-recovery" if scheduled_for is not None else "scheduler",
                summary=f"Heartbeat Recovery [{task_id}]" if scheduled_for is not None else f"Heartbeat Task [{task_id}]",
            ),
            task_kind="Heartbeat",
            task_id=task_id,
            agent_name=agent_name,
        )

    async def _fire_cron_job(
        self,
        cron: dict,
        *,
        runtime_map: dict,
        tasks: dict,
        now_dt: datetime,
        scheduled_for: datetime | None = None,
        recovery_batch_id: str | None = None,
    ) -> bool:
        """Run one due cron while preserving loop and action semantics."""
        task_id = cron["id"]
        agent_name = cron["agent"]
        action = cron.get("action", "enqueue_prompt")

        loop_meta = cron.get("loop_meta")
        if loop_meta is not None:
            count = loop_meta.get("count", 0) + 1
            max_count = loop_meta.get("max", 100)
            if count > max_count:
                scheduler_logger.info(f"Loop {task_id} reached max ({max_count}). Auto-disabling.")
                cron["enabled"] = False
                loop_meta["count"] = count - 1
                loop_meta["stopped_reason"] = "max_reached"
                self._save_tasks(tasks)
                return False
            loop_meta["count"] = count
            self._save_tasks(tasks)

        scheduler_logger.info(f"Triggering cron {task_id} for {agent_name} (schedule: {_resolve_schedule(cron)})")
        rt = runtime_map[agent_name]
        recovery_header = ""
        if scheduled_for is not None:
            recovery_header = (
                "[HASHI scheduler recovery]\n"
                f"This is a missed occurrence originally due at {scheduled_for.astimezone().isoformat(timespec='minutes')}.\n"
                f"Recovery batch: {recovery_batch_id or 'unknown'}\n\n"
            )
        if action == "export_transcript":
            exported = rt.export_daily_transcript(now_dt)
            if not exported:
                scheduler_logger.info(f"No transcript entries to export for {agent_name}")
            return True
        if action.startswith("skill:"):
            skill_id = action.split(":", 1)[1]
            args = recovery_header + (cron.get("args", "") or cron.get("prompt", ""))
            return await self._run_scheduler_action(
                rt.invoke_scheduler_skill(
                    skill_id=skill_id,
                    args=args,
                    task_id=task_id,
                ),
                task_kind="Cron",
                task_id=task_id,
                agent_name=agent_name,
                timeout_s=SCHEDULER_SKILL_TIMEOUT_S,
            )

        prompt = cron.get("prompt", "")
        if not prompt or not prompt.strip():
            scheduler_logger.error(f"Cron {task_id} for {agent_name} has an empty prompt. Skipping.")
            return False
        scheduler_logger.info(
            "Cron %s dispatching through owning agent runtime %s "
            "(backend=%s, access_scope=%s).",
            task_id,
            agent_name,
            getattr(getattr(rt, "config", None), "active_backend", "unknown"),
            getattr(getattr(rt, "config", None), "access_scope", "unknown"),
        )
        return await self._run_scheduler_action(
            rt.enqueue_request(
                chat_id=self.authorized_id,
                prompt=recovery_header + prompt,
                source="scheduler-recovery" if scheduled_for is not None else "scheduler",
                summary=f"Cron Recovery [{task_id}]" if scheduled_for is not None else f"Cron Task [{task_id}]",
            ),
            task_kind="Cron",
            task_id=task_id,
            agent_name=agent_name,
        )

    async def run(self):
        scheduler_logger.info("Task Scheduler started%s.", " (croniter available)" if HAS_CRONITER else " (croniter NOT available, fallback mode)")
        while True:
            lease_held = False
            try:
                if self.enterprise_lease_store is not None:
                    lease_held = self._acquire_enterprise_scheduler_lease()
                    if not lease_held:
                        await asyncio.sleep(15)
                        continue

                tasks = self._load_tasks()
                now = time.time()
                now_dt = datetime.now()

                state_changed = False

                # During the first successful pass, collect all previously-run
                # jobs that became due while HASHI was unavailable. Cron and
                # heartbeat candidates share this collection so each agent gets
                # one recovery decision instead of one prompt per job.
                runtime_map = self._runtime_map()
                await self._retry_pending_recovery_notices(runtime_map, now_ts=now)
                recovery_jobs_by_agent: dict[str, list[dict]] = {}
                for hb in tasks.get("heartbeats", []):
                    if not hb.get("enabled", False):
                        continue
                    task_id = hb["id"]
                    agent_name = hb["agent"]
                    interval = hb["interval_seconds"]
                    prompt = hb.get("prompt", "")
                    action = hb.get("action", "enqueue_prompt")

                    if agent_name not in runtime_map:
                        continue
                    if action == "enqueue_prompt" and (not prompt or not prompt.strip()):
                        scheduler_logger.error(
                            f"Heartbeat {task_id} for {agent_name} has an empty prompt. Skipping."
                        )
                        continue

                    last_run = self.state["heartbeats"].get(task_id, 0)
                    if now - last_run < interval:
                        continue
                    if self._job_owner_mismatch(hb, task_kind="Heartbeat", task_id=task_id, agent_name=agent_name):
                        self.state["heartbeats"][task_id] = now
                        state_changed = True
                        continue
                    if self._startup_recovery_pending and last_run:
                        occurrences = scheduler_recovery.collect_heartbeat_occurrences(
                            float(last_run),
                            int(interval),
                            now,
                        )
                        recovery_jobs_by_agent.setdefault(agent_name, []).append(
                            {
                                "job": hb,
                                "task_id": task_id,
                                "kind": "heartbeat",
                                "interval_seconds": interval,
                                **occurrences,
                                "requires_prompt": False,
                            }
                        )
                        continue
                    ok = await self._fire_heartbeat_job(hb, runtime_map=runtime_map)
                    if ok:
                        self.state["heartbeats"][task_id] = now
                        state_changed = True

                # Process nudges (idle-bound continuation prompts)
                for nudge in tasks.get("nudges", []):
                    if not nudge.get("enabled", False):
                        continue
                    task_id = nudge["id"]
                    agent_name = nudge["agent"]
                    interval = int(nudge.get("interval_seconds") or 60)
                    prompt = nudge.get("prompt", "")

                    if agent_name not in runtime_map:
                        continue
                    if not prompt or not prompt.strip():
                        scheduler_logger.error(
                            f"Nudge {task_id} for {agent_name} has an empty prompt. Skipping."
                        )
                        continue

                    last_run = self.state.setdefault("nudges", {}).get(task_id, 0)
                    if now - last_run < interval:
                        continue

                    if self._job_owner_mismatch(nudge, task_kind="Nudge", task_id=task_id, agent_name=agent_name):
                        self.state["nudges"][task_id] = now
                        state_changed = True
                        continue

                    rt = runtime_map[agent_name]
                    if _runtime_busy(rt):
                        scheduler_logger.info(f"Skipping nudge {task_id} for {agent_name}: runtime busy.")
                        self.state["nudges"][task_id] = now
                        state_changed = True
                        continue

                    meta = nudge.setdefault("nudge_meta", {})
                    count = int(meta.get("count", 0)) + 1
                    max_count = int(meta.get("max", 0) or 0)
                    if max_count > 0 and count > max_count:
                        scheduler_logger.info("Nudge %s reached max (%s). Auto-disabling.", task_id, max_count)
                        nudge["enabled"] = False
                        meta["count"] = count - 1
                        meta["stopped_reason"] = "max_reached"
                        self._save_tasks(tasks)
                        self.state["nudges"][task_id] = now
                        state_changed = True
                        continue

                    scheduler_logger.info(f"Triggering nudge {task_id} for {agent_name}")
                    request_id = await rt.enqueue_request(
                        chat_id=self.authorized_id,
                        prompt=prompt,
                        source="scheduler",
                        summary=f"Nudge Task [{task_id}]",
                    )
                    self._register_nudge_completion_listener(rt, task_id, request_id)
                    meta["count"] = count
                    self._save_tasks(tasks)
                    self.state["nudges"][task_id] = now
                    state_changed = True

                # Process crons (upgraded — cron expression support). Startup
                # catch-up candidates join heartbeat candidates in the same
                # per-agent recovery batch. Outside startup, the historical
                # stale-miss threshold still produces a single-job notice.
                for cron in tasks.get("crons", []):
                    if not cron.get("enabled", False):
                        continue
                    task_id = cron["id"]
                    agent_name = cron["agent"]

                    if agent_name not in runtime_map:
                        continue

                    schedule = _resolve_schedule(cron)
                    if not schedule:
                        scheduler_logger.error(f"Cron {task_id} has no 'schedule' or 'time' field. Skipping.")
                        continue

                    last_run_ts = self._get_cron_last_run(task_id)

                    # Seed new cron jobs: record current time so they fire at the
                    # next scheduled boundary instead of never (see _should_fire
                    # which treats last_run_ts=0 as now_dt, causing get_next to
                    # always return future).
                    if last_run_ts == 0:
                        scheduler_logger.info(f"Seeding new cron {task_id} for {agent_name} — will fire at next scheduled boundary.")
                        self.state["crons"][task_id] = now
                        state_changed = True
                        continue

                    missed_by = _should_fire(schedule, last_run_ts, now_dt)
                    if missed_by is None:
                        continue
                    if self._job_owner_mismatch(cron, task_kind="Cron", task_id=task_id, agent_name=agent_name):
                        self.state["crons"][task_id] = now
                        state_changed = True
                        continue

                    requires_prompt = missed_by > CRON_CATCHUP_THRESHOLD_S
                    if self._startup_recovery_pending or requires_prompt:
                        occurrences = scheduler_recovery.collect_cron_occurrences(
                            schedule,
                            last_run_ts,
                            now_dt,
                            croniter_cls=croniter if HAS_CRONITER else None,
                            fallback_missed_by_seconds=missed_by,
                        )
                        scheduler_logger.info(
                            "Cron %s for %s is a recovery candidate (%s missed occurrence(s), first missed by %sm).",
                            task_id,
                            agent_name,
                            occurrences.get("missed_count", 1),
                            int(missed_by // 60),
                        )
                        recovery_jobs_by_agent.setdefault(agent_name, []).append(
                            {
                                "job": cron,
                                "task_id": task_id,
                                "kind": "cron",
                                "schedule": schedule,
                                **occurrences,
                                "requires_prompt": requires_prompt,
                            }
                        )
                        continue

                    await self._fire_cron_job(
                        cron,
                        runtime_map=runtime_map,
                        tasks=tasks,
                        now_dt=now_dt,
                    )
                    # Cron state advances even when execution fails so the next
                    # tick does not repeatedly fire the same scheduled boundary.
                    self.state["crons"][task_id] = now
                    state_changed = True

                for agent_name, missed_items in recovery_jobs_by_agent.items():
                    # Preserve historical behaviour when only one recent job was
                    # due: heartbeat and recent cron catch-up still auto-run.
                    if len(missed_items) == 1 and not missed_items[0]["requires_prompt"]:
                        item = missed_items[0]
                        if item["kind"] == "heartbeat":
                            ok = await self._fire_heartbeat_job(item["job"], runtime_map=runtime_map)
                            if ok:
                                self.state["heartbeats"][item["task_id"]] = now
                                state_changed = True
                        else:
                            await self._fire_cron_job(
                                item["job"],
                                runtime_map=runtime_map,
                                tasks=tasks,
                                now_dt=now_dt,
                            )
                            self.state["crons"][item["task_id"]] = now
                            state_changed = True
                        continue

                    scheduler_logger.info(
                        "Scheduler recovery batch for %s: %s missed job(s); notifying once.",
                        agent_name,
                        len(missed_items),
                    )
                    recovery_batch = await self._notify_missed_jobs_grouped(
                        runtime_map=runtime_map,
                        agent_name=agent_name,
                        items=missed_items,
                    )
                    for item in missed_items:
                        task_id = item["task_id"]
                        if item["kind"] == "heartbeat":
                            self.state["missed_heartbeats"][task_id] = {
                                "agent": agent_name,
                                "interval_seconds": item.get("interval_seconds"),
                                "missed_by_seconds": item.get("missed_by_seconds"),
                                "missed_count": item.get("missed_count", 1),
                                "first_due_at": item.get("first_due_at"),
                                "last_due_at": item.get("last_due_at"),
                                "recovery_batch_id": recovery_batch.get("batch_id") if recovery_batch else None,
                                "noticed_at": now,
                            }
                            self.state["heartbeats"][task_id] = now
                        else:
                            self.state["missed_crons"][task_id] = {
                                "agent": agent_name,
                                "schedule": item.get("schedule"),
                                "missed_by_seconds": item.get("missed_by_seconds"),
                                "missed_count": item.get("missed_count", 1),
                                "first_due_at": item.get("first_due_at"),
                                "last_due_at": item.get("last_due_at"),
                                "recovery_batch_id": recovery_batch.get("batch_id") if recovery_batch else None,
                                "noticed_at": now,
                            }
                            self.state["crons"][task_id] = now
                        state_changed = True

                self._startup_recovery_pending = False

                # Process parked-topic follow-ups without creating ad hoc task rows.
                for rt in runtime_map.values():
                    handler = getattr(rt, "process_parked_topic_followups", None)
                    if handler is None:
                        continue
                    try:
                        await asyncio.wait_for(handler(now_dt), timeout=PARKED_FOLLOWUP_TIMEOUT_S)
                    except asyncio.TimeoutError:
                        scheduler_logger.error(
                            f"Parked-topic follow-up for {rt.name} timed out after {PARKED_FOLLOWUP_TIMEOUT_S}s; scheduler will continue."
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        scheduler_logger.error(
                            f"Parked-topic follow-up for {rt.name} failed: {e}",
                            exc_info=True,
                        )

                # Advance long-running superloops from persisted loop state.
                try:
                    superloop_stats = advance_superloops_once(
                        self.tasks_path.parent / "superloops",
                        lease_store=None if lease_held else self.enterprise_lease_store,
                        lease_name=self.enterprise_lease_name,
                        lease_holder=self.enterprise_lease_holder,
                        lease_ttl_seconds=self.enterprise_lease_ttl_seconds,
                    )
                    if superloop_stats.get("waits_satisfied") or superloop_stats.get("loops_advanced"):
                        scheduler_logger.info(
                            "Superloop tick: checked=%s waits_satisfied=%s loops_advanced=%s",
                            superloop_stats.get("loops_checked", 0),
                            superloop_stats.get("waits_satisfied", 0),
                            superloop_stats.get("loops_advanced", 0),
                        )
                except Exception as e:
                    scheduler_logger.error(f"Superloop scheduler tick failed: {e}", exc_info=True)

                if state_changed:
                    self._save_state()

            except Exception as e:
                scheduler_logger.error(f"Scheduler error: {e}")
            finally:
                if lease_held:
                    self._release_enterprise_scheduler_lease()

            await asyncio.sleep(15)
