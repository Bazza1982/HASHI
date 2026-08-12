from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from orchestrator.superloop_store import SuperloopStore, normalize_event_actor, system_actor


TERMINAL_OUTCOMES = {"collected", "aborted", "failed"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SuperloopDispatchLedger:
    """Append-only schema-v2 dispatch lifecycle records.

    A collected transport result is deliberately distinct from a certification
    verdict. Aborted or failed requests cannot acquire a PASS by implication.
    """

    def __init__(self, store: SuperloopStore):
        self.store = store

    def record_started(
        self,
        loop_id: str,
        *,
        dispatch_instance_id: str,
        task_id: str,
        request_id: str,
        metadata: dict[str, Any] | None = None,
        actor: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not dispatch_instance_id.strip() or not request_id.strip():
            raise ValueError("dispatch_instance_id and request_id are required")
        row = {
            "schema_version": 2,
            "dispatch_instance_id": dispatch_instance_id,
            "dispatch_id": dispatch_instance_id,
            "loop_id": loop_id,
            "task_id": task_id,
            "request_id": request_id,
            "status": "accepted",
            "terminal": False,
            "created_at": _utc_now(),
            **(metadata or {}),
        }
        self._append(loop_id, row)
        self.store.append_loop_event(
            loop_id,
            event_type="dispatch.accepted",
            data={
                "dispatch_instance_id": dispatch_instance_id,
                "task_id": task_id,
                "request_id": request_id,
            },
            actor=normalize_event_actor(actor or system_actor("superloop_dispatch")),
        )
        return row

    def record_terminal(
        self,
        loop_id: str,
        *,
        dispatch_instance_id: str,
        task_id: str,
        request_id: str | None,
        outcome: str,
        reason: str | None = None,
        evidence_ref: str | None = None,
        classification: str | None = None,
        actor: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_outcome = outcome.strip().lower()
        if normalized_outcome not in TERMINAL_OUTCOMES:
            raise ValueError(f"Unsupported terminal dispatch outcome: {outcome}")
        if normalized_outcome in {"aborted", "failed"} and not str(reason or "").strip():
            raise ValueError(f"{normalized_outcome} dispatch requires an explicit reason")
        row = {
            "schema_version": 2,
            "dispatch_instance_id": dispatch_instance_id,
            "dispatch_id": dispatch_instance_id,
            "loop_id": loop_id,
            "task_id": task_id,
            "request_id": request_id,
            "status": normalized_outcome,
            "terminal": True,
            "outcome": normalized_outcome,
            "reason": reason,
            "classification": classification,
            "evidence_ref": evidence_ref,
            "completed_at": _utc_now(),
            **(metadata or {}),
        }
        # A terminal reconciliation is never a certification PASS unless a
        # separate verdict field is deliberately supplied in metadata.
        if normalized_outcome != "collected":
            row.pop("verdict", None)
        self._append(loop_id, row)
        event_actor = normalize_event_actor(actor or system_actor("superloop_dispatch"))
        self.store.append_loop_event(
            loop_id,
            event_type=f"dispatch.{normalized_outcome}",
            data={
                "dispatch_instance_id": dispatch_instance_id,
                "task_id": task_id,
                "request_id": request_id,
                "reason": reason,
                "classification": classification,
            },
            actor=event_actor,
        )
        self._clear_matching_active(loop_id, dispatch_instance_id, request_id)
        return row

    def _append(self, loop_id: str, row: dict[str, Any]) -> None:
        path = self.store.loop_dir(loop_id) / "dispatches.jsonl"
        with self.store._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _clear_matching_active(
        self,
        loop_id: str,
        dispatch_instance_id: str,
        request_id: str | None,
    ) -> None:
        state = self.store.load_loop_state(loop_id)
        changed = False
        if state.get("active_dispatch_id") in {dispatch_instance_id, request_id}:
            state["active_dispatch_id"] = None
            changed = True
        if state.get("active_request_id") == request_id:
            state["active_request_id"] = None
            changed = True
        if changed:
            self.store.save_loop_state(loop_id, state)
