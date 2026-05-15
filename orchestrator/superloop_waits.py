from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchestrator.superloop_store import SuperloopStore


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        with self.store._lock:
            waits = self.store.load_loop_json_list(waits_path)
            wait_id = self.store.generate_record_id("wait")
            wait = {
                "wait_id": wait_id,
                "kind": kind,
                "status": "pending",
                "created_at": _utc_now(),
                "resume_policy": resume_policy or {"on_satisfied": "advance", "on_timeout": "advance"},
                "details": details or {},
                "timeout": {"deadline": deadline} if deadline else {},
            }
            waits.append(wait)
            self.store.save_loop_json_list(waits_path, waits)
            self.store.refresh_loop_stats(loop_id)
        self.store.append_loop_event(loop_id, event_type="wait.entered", data={"wait_id": wait_id, "kind": kind})
        return wait

    def satisfy_wait(self, loop_id: str, wait_id: str, *, source: str = "manual") -> bool:
        waits_path = self._waits_path(loop_id)
        with self.store._lock:
            waits = self.store.load_loop_json_list(waits_path)
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
            self.store.save_loop_json_list(waits_path, waits)
            self.store.refresh_loop_stats(loop_id)
        self.store.append_loop_event(loop_id, event_type="wait.satisfied", data={"wait_id": wait_id, "source": source})
        return True

    def has_open_waits(self, loop_id: str) -> bool:
        waits = self.store.load_loop_json_list(self._waits_path(loop_id))
        return any(wait.get("status") == "pending" for wait in waits)

    def pending_wait_ids(self, loop_id: str) -> list[str]:
        waits = self.store.load_loop_json_list(self._waits_path(loop_id))
        return [str(wait.get("wait_id")) for wait in waits if wait.get("status") == "pending"]

    def list_waits(self, loop_id: str) -> list[dict[str, Any]]:
        return self.store.load_loop_json_list(self._waits_path(loop_id))

    def _waits_path(self, loop_id: str) -> Path:
        state = self.store.load_loop_state(loop_id)
        waits_rel = state.get("waits_path")
        return self.store.resolve_loop_path(loop_id, waits_rel, "waits.json")
