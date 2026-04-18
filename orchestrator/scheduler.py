import asyncio
import json
import logging
import time
from datetime import datetime
from pathlib import Path

scheduler_logger = logging.getLogger("BridgeU.Scheduler")

SCHEDULER_JOB_TIMEOUT_S = 30
SCHEDULER_SKILL_TIMEOUT_S = 300  # action skills (dream etc.) need much longer — they run subprocesses with LLM calls
PARKED_FOLLOWUP_TIMEOUT_S = 15

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


def _should_fire(schedule: str, last_run_ts: float, now_dt: datetime) -> bool:
    """Check whether *schedule* has a fire time between *last_run_ts* (exclusive) and *now_dt* (inclusive).

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
                    return False
                # Ensure not already fired today.
                # If never run (last_run_ts=0), use today's date so it does NOT
                # fire immediately — it waits for the next scheduled occurrence.
                last_dt = datetime.fromtimestamp(last_run_ts) if last_run_ts else now_dt
                return last_dt.date() < now_dt.date()
            except (ValueError, TypeError):
                return False
        return False

    try:
        # If last_run_ts is 0 (never run), use now_dt as the base so the next
        # scheduled time is calculated forward from *now*, not from year 2000.
        # This prevents new cron jobs from firing immediately on first scheduler tick.
        last_dt = datetime.fromtimestamp(last_run_ts) if last_run_ts else now_dt
        cron = croniter(schedule, last_dt)
        next_fire = cron.get_next(datetime)
        return next_fire <= now_dt
    except (ValueError, KeyError) as e:
        scheduler_logger.error(f"Invalid cron expression '{schedule}': {e}")
        return False


class TaskScheduler:
    def __init__(self, tasks_path: Path, state_path: Path, runtimes: list | None, authorized_id: int, skill_manager=None, orchestrator=None):
        self.tasks_path = tasks_path
        self.state_path = state_path
        self.runtimes = {rt.name: rt for rt in (runtimes or [])}
        self.authorized_id = authorized_id
        self.skill_manager = skill_manager
        self.orchestrator = orchestrator
        self.state = self._load_state()

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
        return {"heartbeats": {}, "crons": {}}

    def _save_state(self):
        try:
            with open(self.state_path, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=2)
        except Exception as e:
            scheduler_logger.error(f"Failed to save state: {e}")

    def _load_tasks(self):
        if not self.tasks_path.exists():
            return {"heartbeats": [], "crons": []}
        try:
            with open(self.tasks_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            scheduler_logger.error(f"Failed to load tasks: {e}")
            return {"heartbeats": [], "crons": []}

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

    async def run(self):
        scheduler_logger.info("Task Scheduler started%s.", " (croniter available)" if HAS_CRONITER else " (croniter NOT available, fallback mode)")
        while True:
            try:
                tasks = self._load_tasks()
                now = time.time()
                now_dt = datetime.now()

                state_changed = False

                # Process heartbeats (unchanged — interval-based)
                runtime_map = self._runtime_map()
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
                    if now - last_run >= interval:
                        scheduler_logger.info(f"Triggering heartbeat {task_id} for {agent_name}")
                        rt = runtime_map[agent_name]
                        if action.startswith("skill:"):
                            skill_id = action.split(":", 1)[1]
                            args = hb.get("args", "") or prompt
                            ok = await self._run_scheduler_action(
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
                        else:
                            ok = await self._run_scheduler_action(
                                rt.enqueue_request(
                                    chat_id=self.authorized_id,
                                    prompt=prompt,
                                    source="scheduler",
                                    summary=f"Heartbeat Task [{task_id}]"
                                ),
                                task_kind="Heartbeat",
                                task_id=task_id,
                                agent_name=agent_name,
                            )
                        if ok:
                            self.state["heartbeats"][task_id] = now
                            state_changed = True

                # Process crons (upgraded — cron expression support)
                for cron in tasks.get("crons", []):
                    if not cron.get("enabled", False):
                        continue
                    task_id = cron["id"]
                    agent_name = cron["agent"]
                    prompt = cron.get("prompt", "")
                    action = cron.get("action", "enqueue_prompt")

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

                    if _should_fire(schedule, last_run_ts, now_dt):
                        # --- Loop safety net: count iterations, auto-disable at max ---
                        loop_meta = cron.get("loop_meta")
                        if loop_meta is not None:
                            count = loop_meta.get("count", 0) + 1
                            max_count = loop_meta.get("max", 100)
                            if count > max_count:
                                scheduler_logger.info(
                                    f"Loop {task_id} reached max ({max_count}). Auto-disabling."
                                )
                                cron["enabled"] = False
                                loop_meta["count"] = count - 1
                                loop_meta["stopped_reason"] = "max_reached"
                                self._save_tasks(tasks)
                                self.state["crons"][task_id] = now
                                state_changed = True
                                continue
                            loop_meta["count"] = count
                            self._save_tasks(tasks)

                        scheduler_logger.info(f"Triggering cron {task_id} for {agent_name} (schedule: {schedule})")
                        rt = runtime_map[agent_name]
                        if action == "export_transcript":
                            exported = rt.export_daily_transcript(now_dt)
                            if not exported:
                                scheduler_logger.info(f"No transcript entries to export for {agent_name}")
                            ok = True
                        elif action.startswith("skill:"):
                            skill_id = action.split(":", 1)[1]
                            args = cron.get("args", "") or cron.get("prompt", "")
                            ok = await self._run_scheduler_action(
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
                        else:
                            if not prompt or not prompt.strip():
                                scheduler_logger.error(
                                    f"Cron {task_id} for {agent_name} has an empty prompt. Skipping."
                                )
                                self.state["crons"][task_id] = now
                                state_changed = True
                                continue
                            ok = await self._run_scheduler_action(
                                rt.enqueue_request(
                                    chat_id=self.authorized_id,
                                    prompt=prompt,
                                    source="scheduler",
                                    summary=f"Cron Task [{task_id}]"
                                ),
                                task_kind="Cron",
                                task_id=task_id,
                                agent_name=agent_name,
                            )
                        # Always update last_run to prevent re-triggering on
                        # timeout/failure.  The cron will fire again at its NEXT
                        # scheduled time, not in the same minute.
                        self.state["crons"][task_id] = now
                        state_changed = True

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

                if state_changed:
                    self._save_state()

            except Exception as e:
                scheduler_logger.error(f"Scheduler error: {e}")

            await asyncio.sleep(15)
