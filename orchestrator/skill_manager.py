from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from orchestrator.job_ownership import ownership_mismatch_label


@dataclass
class SkillDefinition:
    id: str
    name: str
    description: str
    body: str
    skill_dir: Path


class SkillValidationError(ValueError):
    """Raised when a HASHI Skill package violates the public package contract."""


class SkillManager:
    ACTIVE_HEARTBEAT_DEFAULT_MINUTES = 10
    SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    SKILL_FRONTMATTER_KEYS = frozenset(
        {
            "name",
            "description",
            "license",
            "compatibility",
            "metadata",
            "allowed-tools",
        }
    )
    RUNTIME_TOGGLE_IDS = frozenset({"debug", "recall"})

    def __init__(self, project_root: Path, tasks_path: Path):
        self.project_root = project_root
        self.skills_dir = project_root / "skills"
        self.tasks_path = tasks_path
        self.active_heartbeats_path = project_root / "managed_active_heartbeats.json"
        self._skill_validation_errors: list[str] = []

    def _now(self) -> str:
        return datetime.now().isoformat(timespec="seconds")

    def _read_text(self, path: Path) -> str:
        return path.read_text(encoding="utf-8-sig")

    def _parse_frontmatter(self, text: str, *, source: Path) -> tuple[dict[str, Any], str]:
        lines = (text or "").splitlines()
        if not lines or lines[0].strip() != "---":
            raise SkillValidationError(f"{source}: SKILL.md must start with YAML frontmatter")
        try:
            closing_index = next(
                index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"
            )
        except StopIteration as exc:
            raise SkillValidationError(f"{source}: YAML frontmatter is not closed") from exc

        try:
            parsed = yaml.safe_load("\n".join(lines[1:closing_index])) or {}
        except yaml.YAMLError as exc:
            raise SkillValidationError(f"{source}: invalid YAML frontmatter: {exc}") from exc
        if not isinstance(parsed, dict):
            raise SkillValidationError(f"{source}: YAML frontmatter must be a mapping")

        unknown = sorted(
            str(key) for key in set(parsed) - self.SKILL_FRONTMATTER_KEYS
        )
        if unknown:
            raise SkillValidationError(
                f"{source}: unsupported frontmatter keys: {', '.join(str(key) for key in unknown)}"
            )
        for key in ("name", "description"):
            value = parsed.get(key)
            if not isinstance(value, str) or not value.strip():
                raise SkillValidationError(f"{source}: `{key}` must be a non-empty string")
            parsed[key] = value.strip()

        optional_strings = ("license", "allowed-tools")
        for key in optional_strings:
            if key in parsed and not isinstance(parsed[key], str):
                raise SkillValidationError(f"{source}: `{key}` must be a string")

        if "compatibility" in parsed:
            compatibility = parsed["compatibility"]
            if not isinstance(compatibility, str):
                raise SkillValidationError(f"{source}: `compatibility` must be a string")
            if len(compatibility) > 500:
                raise SkillValidationError(
                    f"{source}: `compatibility` must be 500 characters or fewer"
                )

        if "metadata" in parsed:
            metadata = parsed["metadata"]
            if not isinstance(metadata, dict) or any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in metadata.items()
            ):
                raise SkillValidationError(
                    f"{source}: `metadata` must map string keys to string values"
                )

        return parsed, "\n".join(lines[closing_index + 1 :]).strip()

    def _load_skill_definition(self, skill_dir: Path) -> SkillDefinition:
        skill_md = skill_dir / "SKILL.md"
        frontmatter, body = self._parse_frontmatter(self._read_text(skill_md), source=skill_md)
        skill_id = frontmatter["name"]
        if len(skill_id) > 64:
            raise SkillValidationError(f"{skill_md}: `name` must be 64 characters or fewer")
        if not self.SKILL_NAME_PATTERN.fullmatch(skill_id):
            raise SkillValidationError(
                f"{skill_md}: `name` must be lowercase kebab-case (letters, digits, hyphens)"
            )
        if skill_dir.name != skill_id:
            raise SkillValidationError(
                f"{skill_md}: directory `{skill_dir.name}` must match skill name `{skill_id}`"
            )
        if len(frontmatter["description"]) > 1024:
            raise SkillValidationError(
                f"{skill_md}: `description` must be 1024 characters or fewer"
            )
        if not body:
            raise SkillValidationError(f"{skill_md}: instruction body must not be empty")
        return SkillDefinition(
            id=skill_id,
            name=skill_id,
            description=frontmatter["description"],
            body=body,
            skill_dir=skill_dir,
        )

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
            self._skill_validation_errors = []
            return []
        skills: list[SkillDefinition] = []
        errors: list[str] = []
        for skill_dir in sorted(p for p in self.skills_dir.iterdir() if p.is_dir()):
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.exists():
                continue
            try:
                skills.append(self._load_skill_definition(skill_dir))
            except (OSError, UnicodeError, SkillValidationError) as exc:
                errors.append(str(exc))
                continue
        self._skill_validation_errors = errors
        return skills

    def skill_validation_errors(self) -> list[str]:
        self.list_skills()
        return list(self._skill_validation_errors)

    def _canonical_skill_id(self, value: str) -> str:
        return (value or "").strip().lower().replace("_", "-")

    def get_skill(self, skill_id: str) -> SkillDefinition | None:
        wanted = self._canonical_skill_id(skill_id)
        for skill in self.list_skills():
            if skill.id == wanted:
                return skill
        return None

    def get_active_toggle_ids(self, workspace_dir: Path) -> set[str]:
        state = self._load_json(self._skill_state_path(workspace_dir), {})
        active = state.get("active_skills", {})
        if isinstance(active, list):
            raw_ids = {str(item) for item in active}
        elif isinstance(active, dict):
            raw_ids = {str(key) for key, value in active.items() if value}
        else:
            raw_ids = set()
        return {
            canonical
            for item in raw_ids
            if (canonical := self._canonical_skill_id(item)) in self.RUNTIME_TOGGLE_IDS
        }

    def get_active_toggle_skills(self, workspace_dir: Path) -> list[SkillDefinition]:
        active_ids = self.get_active_toggle_ids(workspace_dir)
        debug = self.get_skill("debug")
        return [debug] if debug is not None and "debug" in active_ids else []

    def set_toggle_state(
        self,
        workspace_dir: Path,
        skill_id: str,
        enabled: bool,
        actor: str = "user",
    ) -> tuple[bool, str]:
        canonical_id = self._canonical_skill_id(skill_id)
        if canonical_id not in self.RUNTIME_TOGGLE_IDS:
            return False, f"Unknown runtime setting: {skill_id}"

        state_path = self._skill_state_path(workspace_dir)
        state = self._load_json(state_path, {})
        active = state.get("active_skills", {})
        if not isinstance(active, dict):
            active = {}
        if enabled:
            active[canonical_id] = {"enabled_at": self._now(), "enabled_by": actor}
        else:
            active.pop(canonical_id, None)
        state["active_skills"] = active
        self._save_json(state_path, state)
        label = "Debug" if canonical_id == "debug" else "Recall"
        return True, f"{label} is now {'ON' if enabled else 'OFF'}."

    def describe_skill(self, skill: SkillDefinition, workspace_dir: Path) -> str:
        lines = [
            skill.name,
            skill.description or "No description.",
            f"Usage: /skill {skill.id} <request>",
        ]
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
            f"--- SKILL INSTRUCTIONS [{skill.id}] ---\n"
            f"Skill package root: {skill.skill_dir}\n"
            "Resolve relative resource paths against that package root.\n\n"
            f"{body}\n\n"
            f"--- USER REQUEST ---\n"
            f"{user_prompt}"
        )

    def _load_tasks(self) -> dict[str, Any]:
        tasks = self._load_json(self.tasks_path, {"version": 1, "heartbeats": [], "crons": [], "nudges": []})
        tasks.setdefault("heartbeats", [])
        tasks.setdefault("crons", [])
        tasks.setdefault("nudges", [])
        return tasks

    def _save_tasks(self, payload: dict[str, Any]):
        self._save_json(self.tasks_path, payload)

    def _task_key_for_kind(self, kind: str) -> str:
        if kind == "cron":
            return "crons"
        if kind == "nudge":
            return "nudges"
        return "heartbeats"

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
        key = self._task_key_for_kind(kind)
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

    def upsert_cron_job(
        self,
        *,
        task_id: str,
        agent_name: str,
        schedule: str,
        action: str,
        enabled: bool,
        note: str,
    ) -> dict[str, Any]:
        """Create or update one owned fixed-wall-clock cron definition."""

        tasks = self._load_tasks()
        crons = tasks.setdefault("crons", [])
        existing = next((job for job in crons if job.get("id") == task_id), None)
        if existing is not None and existing.get("agent") != agent_name:
            raise ValueError(f"Cron task {task_id} belongs to another agent")
        job = existing if existing is not None else {"id": task_id}
        job.update(
            {
                "agent": agent_name,
                "enabled": bool(enabled),
                "schedule": str(schedule).strip(),
                "action": str(action).strip(),
                "note": str(note).strip(),
                "updated_at": self._now(),
            }
        )
        job.pop("time", None)
        if existing is None:
            job["created_at"] = job["updated_at"]
            crons.append(job)
        self._save_tasks(tasks)
        return dict(job)

    def migrate_legacy_dream_cron(
        self,
        *,
        agent_name: str,
        new_task_id: str,
        backend_is_her: bool,
    ) -> dict[str, Any]:
        """Retire enabled generic Dream jobs without touching legacy data."""

        tasks = self._load_tasks()
        crons = tasks.setdefault("crons", [])
        legacy_id = f"dream-{agent_name}-nightly"
        legacy_jobs = [
            job
            for job in crons
            if job.get("agent") == agent_name
            and (
                job.get("id") == legacy_id
                or job.get("action") == "skill:dream"
            )
        ]
        enabled_legacy = [job for job in legacy_jobs if bool(job.get("enabled"))]
        canonical = next((job for job in crons if job.get("id") == new_task_id), None)
        changed = False
        created = False
        now = self._now()

        if enabled_legacy and backend_is_her and canonical is None:
            source = enabled_legacy[0]
            schedule = str(source.get("schedule") or "").strip()
            if not schedule:
                legacy_time = str(source.get("time") or "01:30").strip()
                parts = legacy_time.split(":", 1)
                if len(parts) == 2 and all(part.isdigit() for part in parts):
                    schedule = f"{int(parts[1])} {int(parts[0])} * * *"
                else:
                    schedule = "30 1 * * *"
            canonical = {
                "id": new_task_id,
                "agent": agent_name,
                "enabled": True,
                "schedule": schedule,
                "action": "her:dream",
                "note": f"[HER Dream] Habit maintenance for {agent_name}",
                "created_at": now,
                "updated_at": now,
                "migrated_from": str(source.get("id") or legacy_id),
            }
            crons.append(canonical)
            changed = True
            created = True

        for job in legacy_jobs:
            if job.get("enabled"):
                job["enabled"] = False
                job["updated_at"] = now
                job["migration_note"] = (
                    f"Migrated to {new_task_id}."
                    if backend_is_her
                    else "Disabled because HER Dream requires the HER backend."
                )
                changed = True

        if changed:
            self._save_tasks(tasks)
        return {
            "changed": changed,
            "created": created,
            "legacy_enabled_count": len(enabled_legacy),
            "backend_is_her": bool(backend_is_her),
            "new_job": dict(canonical) if canonical is not None else None,
        }

    def describe_jobs(self, kind: str, agent_name: str | None = None) -> str:
        jobs = self.list_jobs(kind, agent_name=agent_name)
        title = "Cron Jobs" if kind == "cron" else "Nudge Jobs" if kind == "nudge" else "Heartbeat Jobs"
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
                if enabled:
                    mismatch = ownership_mismatch_label(job)
                    if mismatch:
                        return False, f"Refusing to enable {task_id}: {mismatch}."
                job["enabled"] = enabled
                self._save_active_heartbeats(jobs)
                return True, f"{task_id} is now {'ON' if enabled else 'OFF'}."
        tasks = self._load_tasks()
        key = self._task_key_for_kind(kind)
        for job in tasks.get(key, []):
            if job.get("id") != task_id:
                continue
            if enabled:
                mismatch = ownership_mismatch_label(job)
                if mismatch:
                    return False, f"Refusing to enable {task_id}: {mismatch}."
            job["enabled"] = enabled
            self._save_tasks(tasks)
            return True, f"{task_id} is now {'ON' if enabled else 'OFF'}."
        return False, f"Unknown {kind} task: {task_id}"

    def set_nudge_max(self, task_id: str, max_nudges: int) -> tuple[bool, str]:
        tasks = self._load_tasks()
        max_value = max(0, int(max_nudges or 0))
        for job in tasks.get("nudges", []):
            if job.get("id") != task_id:
                continue
            meta = job.setdefault("nudge_meta", {})
            meta["max"] = max_value
            meta.pop("stopped_reason", None)
            self._save_tasks(tasks)
            label = "unlimited" if max_value == 0 else str(max_value)
            return True, f"{task_id} max is now {label}."
        return False, f"Unknown nudge task: {task_id}"

    def adjust_nudge_max(self, task_id: str, delta: int) -> tuple[bool, str]:
        tasks = self._load_tasks()
        for job in tasks.get("nudges", []):
            if job.get("id") != task_id:
                continue
            meta = job.setdefault("nudge_meta", {})
            current = max(0, int(meta.get("max", 0) or 0))
            new_value = max(0, current + int(delta))
            meta["max"] = new_value
            meta.pop("stopped_reason", None)
            self._save_tasks(tasks)
            label = "unlimited" if new_value == 0 else str(new_value)
            return True, f"{task_id} max is now {label}."
        return False, f"Unknown nudge task: {task_id}"

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
        key = self._task_key_for_kind(kind)
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
        mismatch = ownership_mismatch_label(new_job)
        if mismatch:
            new_job["note"] += f" [{mismatch}; review before enabling]"

        tasks = self._load_tasks()
        key = self._task_key_for_kind(kind)
        tasks.setdefault(key, []).append(new_job)
        self._save_tasks(tasks)
        return True, f"Transferred to {new_agent} (disabled, review before enabling).", new_job

    def import_job(self, kind: str, job: dict) -> tuple[bool, str]:
        """Import a job dict into local tasks.json (used for cross-instance transfer)."""
        tasks = self._load_tasks()
        key = self._task_key_for_kind(kind)
        job = dict(job)
        mismatch = ownership_mismatch_label(job)
        if mismatch:
            job["enabled"] = False
            note = job.get("note") or job.get("id") or ""
            suffix = f"[{mismatch}; imported disabled]"
            job["note"] = f"{note} {suffix}".strip()
        # Avoid duplicate IDs
        existing_ids = {j.get("id") for j in tasks.get(key, [])}
        if job.get("id") in existing_ids:
            from uuid import uuid4
            job["id"] = f"{job.get('agent', 'imported')}-{uuid4().hex[:8]}"
        tasks.setdefault(key, []).append(job)
        self._save_tasks(tasks)
        return True, f"Imported job {job['id']} for {job.get('agent', '?')}."

    def _nudge_prompt(self, task_id: str, exit_condition: str) -> str:
        return (
            "SYSTEM: Idle nudge continuation.\n"
            "You are being nudged because the agent is idle and this nudge job is still enabled.\n\n"
            f"Nudge job id: {task_id}\n"
            f"Exit condition: {exit_condition}\n\n"
            "Instructions:\n"
            "1. Review the current task state and recent work.\n"
            "2. If the exit condition is NOT satisfied, continue the work with a concrete next step.\n"
            "3. If the exit condition IS satisfied, say so clearly and include this exact marker on its own line:\n"
            f"NUDGE_COMPLETE:{task_id}\n"
            "4. Do not emit the completion marker unless the exit condition is genuinely satisfied.\n"
        )

    def create_nudge_job(
        self,
        *,
        agent_name: str,
        interval_minutes: int,
        exit_condition: str,
        max_nudges: int = 0,
    ) -> dict[str, Any]:
        tasks = self._load_tasks()
        task_id = f"{agent_name}-nudge-{uuid4().hex[:6]}"
        interval_minutes = max(1, int(interval_minutes))
        exit_condition = str(exit_condition or "").strip()
        job = {
            "id": task_id,
            "agent": agent_name,
            "enabled": True,
            "interval_seconds": interval_minutes * 60,
            "action": "enqueue_prompt",
            "prompt": self._nudge_prompt(task_id, exit_condition),
            "note": f"Nudge: {exit_condition[:80]}",
            "exit_condition": exit_condition,
            "nudge_meta": {
                "count": 0,
                "max": int(max_nudges),
                "created_at": self._now(),
            },
        }
        tasks.setdefault("nudges", []).append(job)
        self._save_tasks(tasks)
        return job

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
