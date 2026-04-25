from __future__ import annotations
import asyncio
import json
import os
import sys
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass
class SkillDefinition:
    id: str
    name: str
    type: str
    description: str
    body: str
    skill_dir: Path
    run: str | None = None
    backend: str | None = None


class SkillManager:
    ACTIVE_HEARTBEAT_DEFAULT_MINUTES = 10
    ACTION_SKILL_TIMEOUT_S = 360

    def __init__(self, project_root: Path, tasks_path: Path):
        self.project_root = project_root
        self.skills_dir = project_root / "skills"
        self.tasks_path = tasks_path
        self.active_heartbeats_path = project_root / "managed_active_heartbeats.json"

    def _now(self) -> str:
        return datetime.now().isoformat(timespec="seconds")

    def _read_text(self, path: Path) -> str:
        return path.read_text(encoding="utf-8-sig")

    def _parse_frontmatter(self, text: str) -> tuple[dict[str, str], str]:
        raw = (text or "").strip()
        if not raw.startswith("---"):
            return {}, raw
        parts = raw.split("---", 2)
        if len(parts) < 3:
            return {}, raw
        frontmatter = {}
        for line in parts[1].splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            frontmatter[key.strip()] = value.strip()
        return frontmatter, parts[2].strip()

    def _skill_state_path(self, workspace_dir: Path) -> Path:
        return workspace_dir / "skill_state.json"

    def _load_json(self, path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default

    def _save_json(self, path: Path, payload: Any):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")

    def list_skills(self) -> list[SkillDefinition]:
        if not self.skills_dir.exists():
            return []
        skills: list[SkillDefinition] = []
        for skill_dir in sorted(p for p in self.skills_dir.iterdir() if p.is_dir()):
            skill_md = skill_dir / "skill.md"
            if not skill_md.exists():
                continue
            try:
                frontmatter, body = self._parse_frontmatter(self._read_text(skill_md))
            except Exception:
                continue
            skill_id = (frontmatter.get("id") or skill_dir.name).strip()
            skill_type = (frontmatter.get("type") or "").strip().lower()
            if skill_type not in {"action", "prompt", "toggle"}:
                continue
            skills.append(
                SkillDefinition(
                    id=skill_id,
                    name=(frontmatter.get("name") or skill_id).strip(),
                    type=skill_type,
                    description=(frontmatter.get("description") or "").strip(),
                    body=body,
                    skill_dir=skill_dir,
                    run=frontmatter.get("run"),
                    backend=frontmatter.get("backend"),
                )
            )
        return skills

    def get_skill(self, skill_id: str) -> SkillDefinition | None:
        wanted = (skill_id or "").strip().lower()
        for skill in self.list_skills():
            if skill.id.lower() == wanted or skill.name.lower() == wanted:
                return skill
        return None

    def list_skills_by_type(self) -> dict[str, list[SkillDefinition]]:
        grouped = {"action": [], "toggle": [], "prompt": []}
        for skill in self.list_skills():
            grouped.setdefault(skill.type, []).append(skill)
        return grouped

    def get_active_toggle_ids(self, workspace_dir: Path) -> set[str]:
        state = self._load_json(self._skill_state_path(workspace_dir), {})
        active = state.get("active_skills", {})
        if isinstance(active, list):
            return {str(item) for item in active}
        if isinstance(active, dict):
            return {str(key) for key, value in active.items() if value}
        return set()

    def get_active_toggle_skills(self, workspace_dir: Path) -> list[SkillDefinition]:
        active_ids = self.get_active_toggle_ids(workspace_dir)
        return [skill for skill in self.list_skills() if skill.type == "toggle" and skill.id in active_ids]

    def set_toggle_state(
        self,
        workspace_dir: Path,
        skill_id: str,
        enabled: bool,
        actor: str = "user",
    ) -> tuple[bool, str]:
        skill = self.get_skill(skill_id)
        if skill is None:
            return False, f"Unknown skill: {skill_id}"
        if skill.type != "toggle":
            return False, f"Skill '{skill.id}' is not a toggle skill."

        state_path = self._skill_state_path(workspace_dir)
        state = self._load_json(state_path, {})
        active = state.get("active_skills", {})
        if not isinstance(active, dict):
            active = {}
        if enabled:
            active[skill.id] = {"enabled_at": self._now(), "enabled_by": actor}
        else:
            active.pop(skill.id, None)
        state["active_skills"] = active
        self._save_json(state_path, state)
        return True, f"{skill.name} is now {'ON' if enabled else 'OFF'}."

    def describe_skill(self, skill: SkillDefinition, workspace_dir: Path) -> str:
        lines = [
            f"{skill.name} [{skill.type}]",
            skill.description or "No description.",
        ]
        if skill.type == "toggle":
            state = "ON" if skill.id in self.get_active_toggle_ids(workspace_dir) else "OFF"
            lines.append(f"State: {state}")
            lines.append("Usage: /skill {name} on | /skill {name} off".format(name=skill.id))
        elif skill.type == "prompt":
            backend = skill.backend or "current-backend"
            lines.append(f"Backend: {backend}")
            lines.append(f"Usage: /skill {skill.id} <prompt>")
        elif skill.type == "action":
            lines.append(f"Usage: /skill {skill.id}")
        body = (skill.body or "").strip()
        if body:
            preview = body if len(body) <= 700 else body[:700].rstrip() + "\n\n[truncated]"
            lines.extend(["", preview])
        return "\n".join(lines)

    def build_toggle_sections(self, workspace_dir: Path) -> list[tuple[str, str, str]]:
        sections = []
        for skill in self.get_active_toggle_skills(workspace_dir):
            if not skill.body.strip():
                continue
            sections.append((skill.id, skill.name, skill.body.strip()))
        return sections

    def build_prompt_for_skill(self, skill: SkillDefinition, user_prompt: str) -> str:
        body = (skill.body or "").strip()
        if not body:
            return user_prompt
        return (
            f"--- SKILL CONTEXT [{skill.id}] ---\n"
            f"{body}\n\n"
            f"--- SKILL REQUEST ---\n"
            f"{user_prompt}"
        )

    async def run_action_skill(
        self,
        skill: SkillDefinition,
        workspace_dir: Path,
        args: str = "",
        extra_env: dict[str, str] | None = None,
    ) -> tuple[bool, str]:
        if skill.id in {"cron", "heartbeat"}:
            return True, self.describe_jobs(skill.id)
        if not skill.run:
            return False, f"Action skill '{skill.id}' is missing a run target."

        run_path = skill.skill_dir / skill.run
        if not run_path.exists():
            return False, f"Action target not found: {run_path.name}"

        suffix = run_path.suffix.lower()
        if suffix == ".py":
            cmd = [sys.executable, str(run_path)]
        elif suffix == ".ps1":
            cmd = ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(run_path)]
        elif suffix == ".bat":
            cmd = ["cmd", "/c", str(run_path)]
        else:
            cmd = [str(run_path)]
        if args.strip():
            cmd.append(args.strip())

        env = os.environ.copy()
        env["BRIDGE_PROJECT_ROOT"] = str(self.project_root)
        env["BRIDGE_WORKSPACE_DIR"] = str(workspace_dir)
        env["BRIDGE_SKILL_ID"] = skill.id
        if extra_env:
            env.update({k: str(v) for k, v in extra_env.items() if v is not None})

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(workspace_dir),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            # Keep the local action-skill watchdog longer than the scheduler's
            # outer timeout so nightly jobs do not get marked failed early.
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=float(self.ACTION_SKILL_TIMEOUT_S),
            )
        except asyncio.TimeoutError:
            with suppress(Exception):
                proc.kill()
            return False, f"Action skill '{skill.id}' timed out."
        except Exception as e:
            return False, f"Action skill '{skill.id}' failed to start: {e}"

        out_text = stdout.decode("utf-8", errors="replace").strip()
        err_text = stderr.decode("utf-8", errors="replace").strip()
        lines = []
        if out_text:
            lines.append(out_text)
        if err_text:
            lines.append(f"stderr:\n{err_text}")
        if proc.returncode != 0:
            lines.append(f"exit_code={proc.returncode}")
        text = "\n\n".join(lines).strip() or f"Action skill '{skill.id}' completed."
        success = proc.returncode == 0
        return success, text

    def _load_tasks(self) -> dict[str, Any]:
        return self._load_json(self.tasks_path, {"version": 1, "heartbeats": [], "crons": []})

    def _save_tasks(self, payload: dict[str, Any]):
        self._save_json(self.tasks_path, payload)

    def _is_managed_active_heartbeat(self, job: dict[str, Any]) -> bool:
        if not isinstance(job, dict):
            return False
        return (
            job.get("managed_by") == "active-command"
            or str(job.get("id", "")).endswith("-active-heartbeat")
        )

    def _load_active_heartbeats(self) -> list[dict[str, Any]]:
        payload = self._load_json(self.active_heartbeats_path, {"heartbeats": []})
        if isinstance(payload, list):
            jobs = payload
        else:
            jobs = payload.get("heartbeats", [])
        return [dict(job) for job in jobs if isinstance(job, dict)]

    def _save_active_heartbeats(self, jobs: list[dict[str, Any]]):
        self._save_json(self.active_heartbeats_path, {"heartbeats": jobs})

    def _ensure_active_heartbeats_migrated(self):
        tasks = self._load_tasks()
        heartbeats = list(tasks.get("heartbeats", []))
        migrated = [dict(job) for job in heartbeats if self._is_managed_active_heartbeat(job)]
        if not migrated:
            return

        remaining = [job for job in heartbeats if not self._is_managed_active_heartbeat(job)]
        existing = {job.get("id"): dict(job) for job in self._load_active_heartbeats()}
        for job in migrated:
            existing[job.get("id")] = job

        tasks["heartbeats"] = remaining
        self._save_tasks(tasks)
        self._save_active_heartbeats(list(existing.values()))

    def list_jobs(self, kind: str, agent_name: str | None = None) -> list[dict[str, Any]]:
        self._ensure_active_heartbeats_migrated()
        tasks = self._load_tasks()
        key = "crons" if kind == "cron" else "heartbeats"
        jobs = list(tasks.get(key, []))
        if kind == "heartbeat":
            jobs.extend(self._load_active_heartbeats())
        if agent_name:
            jobs = [job for job in jobs if job.get("agent") == agent_name]
        return jobs

    def get_job(self, kind: str, task_id: str) -> dict[str, Any] | None:
        for job in self.list_jobs(kind):
            if job.get("id") == task_id:
                return job
        return None

    def describe_jobs(self, kind: str, agent_name: str | None = None) -> str:
        jobs = self.list_jobs(kind, agent_name=agent_name)
        title = "Cron Jobs" if kind == "cron" else "Heartbeat Jobs"
        if not jobs:
            suffix = f" for {agent_name}" if agent_name else ""
            return f"{title}{suffix}\n\nNo jobs configured."
        lines = [title]
        if agent_name:
            lines.append(f"Agent: {agent_name}")
        lines.append("")
        for job in jobs:
            enabled = "ON" if job.get("enabled", False) else "OFF"
            schedule = (job.get("schedule") or job.get("time", "?")) if kind == "cron" else f"{job.get('interval_seconds', 0)}s"
            action = job.get("action", "enqueue_prompt")
            note = job.get("note") or ""
            lines.append(f"- {job.get('id')} [{enabled}] {schedule} -> {action}")
            if note:
                lines.append(f"  {note}")
        return "\n".join(lines)

    def set_job_enabled(self, kind: str, task_id: str, enabled: bool) -> tuple[bool, str]:
        self._ensure_active_heartbeats_migrated()
        if kind == "heartbeat":
            jobs = self._load_active_heartbeats()
            for job in jobs:
                if job.get("id") != task_id:
                    continue
                job["enabled"] = enabled
                self._save_active_heartbeats(jobs)
                return True, f"{task_id} is now {'ON' if enabled else 'OFF'}."
        tasks = self._load_tasks()
        key = "crons" if kind == "cron" else "heartbeats"
        for job in tasks.get(key, []):
            if job.get("id") != task_id:
                continue
            job["enabled"] = enabled
            self._save_tasks(tasks)
            return True, f"{task_id} is now {'ON' if enabled else 'OFF'}."
        return False, f"Unknown {kind} task: {task_id}"

    def delete_job(self, kind: str, task_id: str) -> tuple[bool, str]:
        self._ensure_active_heartbeats_migrated()
        if kind == "heartbeat":
            jobs = self._load_active_heartbeats()
            for i, job in enumerate(jobs):
                if job.get("id") == task_id:
                    jobs.pop(i)
                    self._save_active_heartbeats(jobs)
                    return True, f"Deleted {task_id}."
        tasks = self._load_tasks()
        key = "crons" if kind == "cron" else "heartbeats"
        jobs = tasks.get(key, [])
        for i, job in enumerate(jobs):
            if job.get("id") == task_id:
                jobs.pop(i)
                self._save_tasks(tasks)
                return True, f"Deleted {task_id}."
        return False, f"Unknown {kind} task: {task_id}"

    def transfer_job(self, kind: str, task_id: str, new_agent: str) -> tuple[bool, str, dict | None]:
        """Disable original job and create a copy owned by new_agent.

        Returns (ok, message, new_job_dict).
        The new job is enabled=False so the recipient can review before enabling.
        """
        import copy
        from uuid import uuid4

        job = self.get_job(kind, task_id)
        if not job:
            return False, f"Job {task_id} not found.", None

        # Disable original
        ok, msg = self.set_job_enabled(kind, task_id, enabled=False)
        if not ok:
            return False, msg, None

        # Create copy for new owner
        new_job = copy.deepcopy(job)
        new_job["id"] = f"{new_agent}-{uuid4().hex[:8]}"
        new_job["agent"] = new_agent
        new_job["enabled"] = False
        new_job["note"] = (job.get("note") or job["id"]) + f" [transferred from {job.get('agent', '?')}]"

        tasks = self._load_tasks()
        key = "crons" if kind == "cron" else "heartbeats"
        tasks.setdefault(key, []).append(new_job)
        self._save_tasks(tasks)
        return True, f"Transferred to {new_agent} (disabled, review before enabling).", new_job

    def import_job(self, kind: str, job: dict) -> tuple[bool, str]:
        """Import a job dict into local tasks.json (used for cross-instance transfer)."""
        tasks = self._load_tasks()
        key = "crons" if kind == "cron" else "heartbeats"
        # Avoid duplicate IDs
        existing_ids = {j.get("id") for j in tasks.get(key, [])}
        if job.get("id") in existing_ids:
            from uuid import uuid4
            job = dict(job)
            job["id"] = f"{job.get('agent', 'imported')}-{uuid4().hex[:8]}"
        tasks.setdefault(key, []).append(job)
        self._save_tasks(tasks)
        return True, f"Imported job {job['id']} for {job.get('agent', '?')}."

    def get_active_heartbeat_job_id(self, agent_name: str) -> str:
        return f"{agent_name}-active-heartbeat"

    def get_active_heartbeat_job(self, agent_name: str) -> dict[str, Any] | None:
        self._ensure_active_heartbeats_migrated()
        task_id = self.get_active_heartbeat_job_id(agent_name)
        for job in self._load_active_heartbeats():
            if job.get("id") == task_id:
                return job
        return None

    def describe_active_heartbeat(self, agent_name: str) -> str:
        job = self.get_active_heartbeat_job(agent_name)
        default_minutes = self.ACTIVE_HEARTBEAT_DEFAULT_MINUTES
        if not job:
            return (
                f"Active mode: OFF\n"
                f"Interval: {default_minutes} min (default)\n"
                f"Usage: /active on [{default_minutes}] | /active off"
            )
        interval_minutes = max(1, int(job.get("interval_seconds", default_minutes * 60) // 60))
        state = "ON" if job.get("enabled", False) else "OFF"
        reset_note = " (default reset)" if not job.get("enabled", False) and interval_minutes == default_minutes else ""
        return (
            f"Active mode: {state}\n"
            f"Interval: {interval_minutes} min{reset_note}\n"
            f"Job: {job.get('id')}\n"
            f"Usage: /active on [{default_minutes}] | /active off"
        )

    def _active_heartbeat_prompt(self, interval_minutes: int) -> str:
        return (
            "SYSTEM: Active follow-up heartbeat. You are in proactive mode. "
            f"About {interval_minutes} minutes have passed since the user's recent activity. "
            "Review the most recent conversation context, workspace evidence, queued/running work, and any obvious signs of progress or blockage. "
            "Then proactively help the user: report meaningful progress, warn about concrete problems or stalls, ask a concise unblock question if needed, or remind the user about unfinished work that likely still matters. "
            "Be concise, specific, and useful. Do not pretend to have done work you have not done. If there is nothing meaningful to report, say that briefly instead of inventing activity."
        )

    def set_active_heartbeat(self, agent_name: str, enabled: bool, minutes: int | None = None) -> tuple[bool, str]:
        self._ensure_active_heartbeats_migrated()
        heartbeats = self._load_active_heartbeats()
        task_id = self.get_active_heartbeat_job_id(agent_name)
        interval_minutes = max(1, int(minutes or self.ACTIVE_HEARTBEAT_DEFAULT_MINUTES))
        if not enabled:
            interval_minutes = self.ACTIVE_HEARTBEAT_DEFAULT_MINUTES
        interval_seconds = interval_minutes * 60

        job = None
        for entry in heartbeats:
            if entry.get("id") == task_id:
                job = entry
                break

        if job is None and not enabled:
            return True, (
                f"Active mode is already OFF. Interval remains "
                f"{self.ACTIVE_HEARTBEAT_DEFAULT_MINUTES} min."
            )

        if job is None:
            job = {
                "id": task_id,
                "agent": agent_name,
                "enabled": enabled,
                "interval_seconds": interval_seconds,
                "action": "enqueue_prompt",
                "prompt": self._active_heartbeat_prompt(interval_minutes),
                "note": f"Managed proactive follow-up heartbeat for {agent_name}",
                "managed_by": "active-command",
            }
            heartbeats.append(job)
        else:
            job["enabled"] = enabled
            job["interval_seconds"] = interval_seconds
            job["action"] = "enqueue_prompt"
            job["prompt"] = self._active_heartbeat_prompt(interval_minutes)
            job["note"] = f"Managed proactive follow-up heartbeat for {agent_name}"
            job["managed_by"] = "active-command"

        self._save_active_heartbeats(heartbeats)

        if enabled:
            return True, f"Active mode is now ON. Proactive heartbeat set to every {interval_minutes} min."
        return True, (
            f"Active mode is now OFF. Proactive heartbeat disabled and interval reset to "
            f"{self.ACTIVE_HEARTBEAT_DEFAULT_MINUTES} min."
        )
