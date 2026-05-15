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
        self.append_loop_event(loop_id, event_type="loop.created", data={"status": loop_state.get("status", "draft")})
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
            "actor": actor or {},
            "refs": refs or {},
            "data": data or {},
        }
        events_path = self.loop_dir(loop_id) / "events.jsonl"
        with self._lock:
            events_path.parent.mkdir(parents=True, exist_ok=True)
            with open(events_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        return event
