from __future__ import annotations

import json
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_dump(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    tmp_path.replace(path)


def _json_load(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return data


def system_actor(name: str, *, instance: str = "HASHI1", reason: str | None = None) -> dict[str, Any]:
    actor = {"kind": "system", "agent": name, "instance": instance}
    if reason:
        actor["reason"] = reason
    return actor


def agent_actor(agent: str, *, instance: str = "HASHI1", source: str | None = None) -> dict[str, Any]:
    actor = {"kind": "agent", "agent": agent, "instance": instance}
    if source:
        actor["source"] = source
    return actor


def normalize_event_actor(actor: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(actor, dict) or not actor:
        return system_actor("superloop_unknown_writer", reason="actor_not_supplied")
    normalized = dict(actor)
    if not str(normalized.get("agent") or "").strip():
        normalized["agent"] = "superloop_unknown_writer"
    if not str(normalized.get("instance") or "").strip():
        normalized["instance"] = "HASHI1"
    if not str(normalized.get("kind") or "").strip():
        normalized["kind"] = "agent" if normalized.get("source") else "system"
    return normalized


class SuperloopStore:
    def __init__(self, root_dir: Path):
        self.root_dir = root_dir
        self.recordings_dir = self.root_dir / "recordings"
        self.loops_dir = self.root_dir / "loops"
        self._lock = threading.RLock()
        self.recordings_dir.mkdir(parents=True, exist_ok=True)
        self.loops_dir.mkdir(parents=True, exist_ok=True)

    def generate_recording_id(self) -> str:
        return f"slrec-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S%f')}-{secrets.token_hex(2)}"

    def generate_loop_id(self) -> str:
        return f"sl-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S%f')}-{secrets.token_hex(2)}"

    def generate_record_id(self, prefix: str) -> str:
        return f"{prefix}-{datetime.now(timezone.utc).strftime('%H%M%S%f')}-{secrets.token_hex(2)}"

    def recording_dir(self, recording_id: str) -> Path:
        return self.recordings_dir / recording_id

    def loop_dir(self, loop_id: str) -> Path:
        return self.loops_dir / loop_id

    def create_recording(
        self,
        *,
        recording_id: str,
        goal: str,
        owner_agent: str,
        owner_instance: str,
        source_mode: str = "incremental",
    ) -> dict[str, Any]:
        now = _utc_now()
        state = {
            "recording_id": recording_id,
            "status": "recording",
            "goal": goal,
            "source_mode": source_mode,
            "owner_agent": owner_agent,
            "owner_instance": owner_instance,
            "created_at": now,
            "updated_at": now,
            "intent_summary": "",
            "exit_condition_draft": None,
            "candidate_steps": [],
            "candidate_waits": [],
            "candidate_agents": [],
            "candidate_artifacts": [],
            "candidate_nagare_runs": [],
            "open_questions": [],
            "finish_ready": False,
            "compiled_loop_id": None,
            "notes": [],
        }
        rec_dir = self.recording_dir(recording_id)
        with self._lock:
            if rec_dir.exists():
                raise FileExistsError(f"Recording already exists: {recording_id}")
            rec_dir.mkdir(parents=True, exist_ok=False)
            _json_dump(rec_dir / "state.json", state)
        self.append_recording_event(
            recording_id,
            event_type="recording.started",
            data={"goal": goal, "source_mode": source_mode},
            actor={"agent": owner_agent, "instance": owner_instance},
        )
        return state

    def load_recording_state(self, recording_id: str) -> dict[str, Any]:
        state_path = self.recording_dir(recording_id) / "state.json"
        if not state_path.exists():
            raise FileNotFoundError(f"Recording state not found: {recording_id}")
        return _json_load(state_path)

    def save_recording_state(self, recording_id: str, state: dict[str, Any]) -> None:
        state = dict(state)
        state["updated_at"] = _utc_now()
        state_path = self.recording_dir(recording_id) / "state.json"
        with self._lock:
            _json_dump(state_path, state)

    def append_recording_event(
        self,
        recording_id: str,
        *,
        event_type: str,
        data: dict[str, Any] | None = None,
        actor: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        event = {
            "event_id": f"rec-event-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}",
            "ts": _utc_now(),
            "recording_id": recording_id,
            "kind": event_type,
            "actor": actor or {},
            "data": data or {},
        }
        events_path = self.recording_dir(recording_id) / "events.jsonl"
        with self._lock:
            events_path.parent.mkdir(parents=True, exist_ok=True)
            with open(events_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        return event

    def create_compiled_loop(
        self,
        *,
        loop_id: str,
        loop_state: dict[str, Any],
        taskboard: list[dict[str, Any]],
        issues: list[dict[str, Any]],
        waits: list[dict[str, Any]],
        operator_summary: str,
        event_data: dict[str, Any] | None = None,
        actor: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        loop_dir = self.loop_dir(loop_id)
        with self._lock:
            if loop_dir.exists():
                raise FileExistsError(f"Loop already exists: {loop_id}")
            loop_dir.mkdir(parents=True, exist_ok=False)
            _json_dump(loop_dir / "state.json", loop_state)
            _json_dump(loop_dir / "taskboard.json", taskboard)
            _json_dump(loop_dir / "issues.json", issues)
            _json_dump(loop_dir / "waits.json", waits)
            with open(loop_dir / "README.md", "w", encoding="utf-8") as handle:
                handle.write(operator_summary)
        created_data = {"status": loop_state.get("status", "draft")}
        if event_data:
            created_data.update(event_data)
        self.append_loop_event(loop_id, event_type="loop.created", data=created_data, actor=actor)
        return {
            "state": str(loop_dir / "state.json"),
            "taskboard": str(loop_dir / "taskboard.json"),
            "issues": str(loop_dir / "issues.json"),
            "waits": str(loop_dir / "waits.json"),
            "summary": str(loop_dir / "README.md"),
        }

    def load_loop_state(self, loop_id: str) -> dict[str, Any]:
        state_path = self.loop_dir(loop_id) / "state.json"
        if not state_path.exists():
            raise FileNotFoundError(f"Loop state not found: {loop_id}")
        return _json_load(state_path)

    def save_loop_state(self, loop_id: str, state: dict[str, Any]) -> None:
        payload = dict(state)
        payload["updated_at"] = _utc_now()
        state_path = self.loop_dir(loop_id) / "state.json"
        with self._lock:
            _json_dump(state_path, payload)

    def resolve_loop_path(self, loop_id: str, maybe_rel: str | None, fallback_name: str) -> Path:
        if isinstance(maybe_rel, str) and maybe_rel.strip():
            candidate = (self.root_dir.parent / maybe_rel).resolve()
        else:
            candidate = (self.loop_dir(loop_id) / fallback_name).resolve()
        root = self.root_dir.resolve()
        if root not in candidate.parents and candidate != root:
            raise ValueError(f"Path escapes superloops root: {candidate}")
        return candidate

    def load_loop_json_list(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        with self._lock:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        if not isinstance(payload, list):
            raise ValueError(f"Expected list JSON in {path}")
        return [item for item in payload if isinstance(item, dict)]

    def save_loop_json_list(self, path: Path, payload: list[dict[str, Any]]) -> None:
        with self._lock:
            _json_dump(path, payload)

    def refresh_loop_stats(self, loop_id: str) -> dict[str, Any]:
        with self._lock:
            state = self.load_loop_state(loop_id)
            taskboard_path = self.resolve_loop_path(loop_id, state.get("taskboard_path"), "taskboard.json")
            issues_path = self.resolve_loop_path(loop_id, state.get("issues_path"), "issues.json")
            waits_path = self.resolve_loop_path(loop_id, state.get("waits_path"), "waits.json")
            tasks = self.load_loop_json_list(taskboard_path)
            issues = self.load_loop_json_list(issues_path)
            waits = self.load_loop_json_list(waits_path)
            state["stats"] = {
                "task_total": len(tasks),
                "task_completed": sum(1 for task in tasks if task.get("status") == "completed"),
                "issue_open": sum(1 for issue in issues if issue.get("status") == "open"),
                "wait_open": sum(1 for wait in waits if wait.get("status") == "pending"),
            }
            self.save_loop_state(loop_id, state)
            return state["stats"]

    def append_loop_event(
        self,
        loop_id: str,
        *,
        event_type: str,
        data: dict[str, Any] | None = None,
        actor: dict[str, Any] | None = None,
        refs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        event = {
            "event_id": f"loop-event-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}",
            "ts": _utc_now(),
            "loop_id": loop_id,
            "kind": event_type,
            "actor": normalize_event_actor(actor),
            "refs": refs or {},
            "data": data or {},
        }
        events_path = self.loop_dir(loop_id) / "events.jsonl"
        with self._lock:
            events_path.parent.mkdir(parents=True, exist_ok=True)
            with open(events_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        return event
