import asyncio
import json
import logging
import time
from datetime import datetime
from pathlib import Path

scheduler_logger = logging.getLogger("BridgeU.Scheduler")

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

    async def run(self):
        scheduler_logger.info("Task Scheduler started.")
        while True:
            try:
                tasks = self._load_tasks()
                now = time.time()
                now_dt = datetime.now()
                current_hm = now_dt.strftime("%H:%M")

                state_changed = False

                # Process heartbeats
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
                            await rt.invoke_scheduler_skill(
                                skill_id=skill_id,
                                args=args,
                                task_id=task_id,
                            )
                        else:
                            await rt.enqueue_request(
                                chat_id=self.authorized_id,
                                prompt=prompt,
                                source="scheduler",
                                summary=f"Heartbeat Task [{task_id}]"
                            )
                        self.state["heartbeats"][task_id] = now
                        state_changed = True

                # Process crons
                for cron in tasks.get("crons", []):
                    if not cron.get("enabled", False):
                        continue
                    task_id = cron["id"]
                    agent_name = cron["agent"]
                    target_time = cron["time"]
                    prompt = cron.get("prompt", "")
                    action = cron.get("action", "enqueue_prompt")

                    if agent_name not in runtime_map:
                        continue

                    last_run_date = self.state["crons"].get(task_id, "")
                    today_date = now_dt.strftime("%Y-%m-%d")

                    if current_hm == target_time and last_run_date != today_date:
                        scheduler_logger.info(f"Triggering cron {task_id} for {agent_name}")
                        rt = runtime_map[agent_name]
                        if action == "export_transcript":
                            exported = rt.export_daily_transcript(now_dt)
                            if not exported:
                                scheduler_logger.info(f"No transcript entries to export for {agent_name}")
                        elif action.startswith("skill:"):
                            skill_id = action.split(":", 1)[1]
                            args = cron.get("args", "") or cron.get("prompt", "")
                            await rt.invoke_scheduler_skill(
                                skill_id=skill_id,
                                args=args,
                                task_id=task_id,
                            )
                        else:
                            if not prompt or not prompt.strip():
                                scheduler_logger.error(
                                    f"Cron {task_id} for {agent_name} has an empty prompt. Skipping."
                                )
                                self.state["crons"][task_id] = today_date
                                state_changed = True
                                continue
                            await rt.enqueue_request(
                                chat_id=self.authorized_id,
                                prompt=prompt,
                                source="scheduler",
                                summary=f"Cron Task [{task_id}]"
                            )
                        self.state["crons"][task_id] = today_date
                        state_changed = True

                # Process parked-topic follow-ups without creating ad hoc task rows.
                for rt in runtime_map.values():
                    handler = getattr(rt, "process_parked_topic_followups", None)
                    if handler is None:
                        continue
                    await handler(now_dt)

                if state_changed:
                    self._save_state()

            except Exception as e:
                scheduler_logger.error(f"Scheduler error: {e}")

            await asyncio.sleep(15)
