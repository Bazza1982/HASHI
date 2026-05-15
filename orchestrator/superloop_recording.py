from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from orchestrator.superloop_store import SuperloopStore


class SuperloopRecordingService:
    def __init__(self, store: SuperloopStore):
        self.store = store

    def start_recording(
        self,
        *,
        goal: str,
        owner_agent: str,
        owner_instance: str,
        source_mode: str = "incremental",
        recording_id: str | None = None,
    ) -> dict[str, Any]:
        target_id = recording_id or self.store.generate_recording_id()
        state = self.store.create_recording(
            recording_id=target_id,
            goal=goal,
            owner_agent=owner_agent,
            owner_instance=owner_instance,
            source_mode=source_mode,
        )
        return {
            "ok": True,
            "recording_id": target_id,
            "status": state["status"],
            "message": "Recording session created.",
        }

    def get_status(self, recording_id: str) -> dict[str, Any]:
        state = self.store.load_recording_state(recording_id)
        return {"ok": True, "recording_id": recording_id, "status": state.get("status"), "state": state}

    def add_note(self, recording_id: str, note: str, *, actor_agent: str, actor_instance: str) -> dict[str, Any]:
        state = self.store.load_recording_state(recording_id)
        notes = list(state.get("notes") or [])
        notes.append(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "text": note,
                "actor": {"agent": actor_agent, "instance": actor_instance},
            }
        )
        state["notes"] = notes
        self.store.save_recording_state(recording_id, state)
        self.store.append_recording_event(
            recording_id,
            event_type="recording.note_added",
            data={"note": note},
            actor={"agent": actor_agent, "instance": actor_instance},
        )
        return {"ok": True, "recording_id": recording_id, "notes_count": len(notes)}

    def record_trial_step(
        self,
        recording_id: str,
        *,
        title: str,
        step_kind: str,
        owner_agent: str,
        owner_instance: str,
        depends_on: list[str] | None = None,
        execution_mode: str = "simulated",
        success: bool = True,
    ) -> dict[str, Any]:
        state = self.store.load_recording_state(recording_id)
        steps = list(state.get("candidate_steps") or [])
        step_id = self._next_step_id(steps)
        step = {
            "step_id": step_id,
            "kind": step_kind,
            "title": title,
            "status": "accepted" if success else "rejected",
            "owner_agent": owner_agent,
            "owner_instance": owner_instance,
            "depends_on": list(depends_on or []),
            "trial": {"mode": execution_mode, "success": success},
        }
        steps.append(step)
        state["candidate_steps"] = steps
        state["finish_ready"] = self._is_finish_ready(state)
        self.store.save_recording_state(recording_id, state)
        self.store.append_recording_event(
            recording_id,
            event_type="step.tried",
            data={"step_id": step_id, "title": title, "success": success, "mode": execution_mode},
            actor={"agent": owner_agent, "instance": owner_instance},
        )
        return {
            "ok": True,
            "recording_id": recording_id,
            "trial_id": f"trial-{len(steps):03d}",
            "recorded_as_step_id": step_id,
            "execution": {"mode": execution_mode, "success": success},
        }

    def set_intent_summary(
        self,
        recording_id: str,
        *,
        intent_summary: str,
        actor_agent: str,
        actor_instance: str,
    ) -> dict[str, Any]:
        state = self.store.load_recording_state(recording_id)
        state["intent_summary"] = intent_summary.strip()
        state["finish_ready"] = self._is_finish_ready(state)
        self.store.save_recording_state(recording_id, state)
        self.store.append_recording_event(
            recording_id,
            event_type="intent.reframed",
            data={"intent_summary": state["intent_summary"]},
            actor={"agent": actor_agent, "instance": actor_instance},
        )
        return {"ok": True, "recording_id": recording_id}

    def set_exit_condition(
        self,
        recording_id: str,
        *,
        exit_condition: dict[str, Any],
        actor_agent: str,
        actor_instance: str,
    ) -> dict[str, Any]:
        state = self.store.load_recording_state(recording_id)
        state["exit_condition_draft"] = dict(exit_condition)
        state["finish_ready"] = self._is_finish_ready(state)
        self.store.save_recording_state(recording_id, state)
        self.store.append_recording_event(
            recording_id,
            event_type="recording.exit_condition_set",
            data={"exit_condition": exit_condition},
            actor={"agent": actor_agent, "instance": actor_instance},
        )
        return {"ok": True, "recording_id": recording_id}

    @staticmethod
    def _next_step_id(steps: list[dict[str, Any]]) -> str:
        return f"step-{len(steps) + 1:03d}"

    @staticmethod
    def _is_finish_ready(state: dict[str, Any]) -> bool:
        goal = str(state.get("goal") or "").strip()
        intent_summary = str(state.get("intent_summary") or "").strip()
        exit_condition = state.get("exit_condition_draft")
        steps = [item for item in (state.get("candidate_steps") or []) if item.get("status") == "accepted"]
        return bool(goal and intent_summary and isinstance(exit_condition, dict) and steps)
