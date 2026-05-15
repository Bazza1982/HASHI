from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchestrator.superloop_store import SuperloopStore


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json_list(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, list):
        raise ValueError(f"Expected list JSON in {path}")
    return [item for item in payload if isinstance(item, dict)]


def _dump_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    tmp_path.replace(path)


class SuperloopWaitsService:
    def __init__(self, store: SuperloopStore):
        self.store = store

    def add_wait(
        self,
        loop_id: str,
        *,
        kind: str,
        details: dict[str, Any] | None = None,
        resume_policy: dict[str, Any] | None = None,
        deadline: str | None = None,
    ) -> dict[str, Any]:
        waits_path = self._waits_path(loop_id)
        waits = _load_json_list(waits_path)
        wait_id = f"wait-{len(waits) + 1:03d}"
        wait = {
            "wait_id": wait_id,
            "kind": kind,
            "status": "pending",
            "created_at": _utc_now(),
            "resume_policy": resume_policy or {"on_satisfied": "advance", "on_timeout": "raise_issue"},
            "details": details or {},
            "timeout": {"deadline": deadline} if deadline else {},
        }
        waits.append(wait)
        _dump_json(waits_path, waits)
        self.store.append_loop_event(loop_id, event_type="wait.entered", data={"wait_id": wait_id, "kind": kind})
        return wait

    def satisfy_wait(self, loop_id: str, wait_id: str, *, source: str = "manual") -> bool:
        waits_path = self._waits_path(loop_id)
        waits = _load_json_list(waits_path)
        updated = False
        for wait in waits:
            if wait.get("wait_id") != wait_id:
                continue
            wait["status"] = "satisfied"
            wait["satisfied_at"] = _utc_now()
            wait["satisfied_source"] = source
            updated = True
            break
        if not updated:
            return False
        _dump_json(waits_path, waits)
        self.store.append_loop_event(loop_id, event_type="wait.satisfied", data={"wait_id": wait_id, "source": source})
        return True

    def has_open_waits(self, loop_id: str) -> bool:
        waits = _load_json_list(self._waits_path(loop_id))
        return any(wait.get("status") == "pending" for wait in waits)

    def pending_wait_ids(self, loop_id: str) -> list[str]:
        waits = _load_json_list(self._waits_path(loop_id))
        return [str(wait.get("wait_id")) for wait in waits if wait.get("status") == "pending"]

    def _waits_path(self, loop_id: str) -> Path:
        state = self.store.load_loop_state(loop_id)
        waits_rel = state.get("waits_path")
        if isinstance(waits_rel, str) and waits_rel.strip():
            return (self.store.root_dir.parent / waits_rel).resolve()
        return self.store.loop_dir(loop_id) / "waits.json"
