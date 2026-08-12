from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from orchestrator.superloop_interlock import evaluate_dispatch_interlock, loop_dispatch_lock
from orchestrator.superloop_store import SuperloopStore, normalize_event_actor, system_actor


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SuperloopControlService:
    """Persist pause/resume transitions against the dispatch acceptance lock."""

    def __init__(self, store: SuperloopStore):
        self.store = store

    def pause(
        self,
        loop_id: str,
        *,
        mode: str = "drain",
        active_request_ids: list[str] | None = None,
        actor: dict[str, Any] | None = None,
        source: str = "command",
    ) -> dict[str, Any]:
        normalized_mode = mode.strip().lower()
        if normalized_mode not in {"drain", "immediate"}:
            raise ValueError(f"Unsupported pause mode: {mode}")
        event_actor = normalize_event_actor(actor or system_actor("superloop_control"))

        with loop_dispatch_lock(self.store, loop_id):
            state = self.store.load_loop_state(loop_id)
            previous_status = str(state.get("status") or "")
            control = state.get("control") if isinstance(state.get("control"), dict) else {}
            control = dict(control)
            prior_pause = control.get("pause") if isinstance(control.get("pause"), dict) else {}
            inferred_active = [
                str(value)
                for value in (
                    state.get("active_request_id"),
                    state.get("active_dispatch_id"),
                )
                if value
            ]
            active = list(dict.fromkeys(active_request_ids if active_request_ids is not None else inferred_active))
            requested_at = str(prior_pause.get("requested_at") or _utc_now())
            pause = {
                "mode": normalized_mode,
                "requested_at": requested_at,
                "source": source,
                "previous_status": prior_pause.get("previous_status") or previous_status,
                "active_request_ids": active,
                "drain_complete": not active,
                "resume_action": prior_pause.get("resume_action") or state.get("next_action"),
            }
            control["requested_action"] = "pause"
            control["pause_requested"] = True
            control["pause"] = pause
            state["control"] = control
            state["status"] = "paused"
            state["next_action"] = {
                "kind": "await_operator_resume",
                "reason": "operator_pause",
                "mode": normalized_mode,
                "active_request_ids": active,
            }
            self.store.save_loop_state(loop_id, state)
            self.store.append_loop_event(
                loop_id,
                event_type="loop.paused",
                data={
                    "source": source,
                    "mode": normalized_mode,
                    "previous_status": previous_status,
                    "active_request_ids": active,
                    "drain_complete": not active,
                },
                actor=event_actor,
            )
        return {
            "ok": True,
            "loop_id": loop_id,
            "status": "paused",
            "mode": normalized_mode,
            "active_request_ids": active,
            "drain_complete": not active,
            "already_paused": previous_status == "paused",
        }

    def mark_drained(
        self,
        loop_id: str,
        *,
        actor: dict[str, Any] | None = None,
        source: str = "reconciliation",
        resume_action: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        event_actor = normalize_event_actor(actor or system_actor("superloop_control"))
        with loop_dispatch_lock(self.store, loop_id):
            state = self.store.load_loop_state(loop_id)
            control = state.get("control") if isinstance(state.get("control"), dict) else {}
            control = dict(control)
            pause = control.get("pause") if isinstance(control.get("pause"), dict) else {}
            pause = dict(pause)
            pause["active_request_ids"] = []
            pause["drain_complete"] = True
            pause["drained_at"] = _utc_now()
            if resume_action is not None:
                pause["resume_action"] = dict(resume_action)
            control["pause"] = pause
            state["control"] = control
            state["active_request_id"] = None
            state["active_dispatch_id"] = None
            next_action = state.get("next_action")
            if isinstance(next_action, dict) and next_action.get("kind") == "await_operator_resume":
                next_action = dict(next_action)
                next_action["active_request_ids"] = []
                state["next_action"] = next_action
            self.store.save_loop_state(loop_id, state)
            self.store.append_loop_event(
                loop_id,
                event_type="loop.pause_drained",
                data={"source": source},
                actor=event_actor,
            )
        return {"ok": True, "loop_id": loop_id, "status": "paused", "drain_complete": True}

    def resume(
        self,
        loop_id: str,
        *,
        actor: dict[str, Any] | None = None,
        source: str = "command",
    ) -> dict[str, Any]:
        event_actor = normalize_event_actor(actor or system_actor("superloop_control"))
        with loop_dispatch_lock(self.store, loop_id):
            state = self.store.load_loop_state(loop_id)
            proposed = dict(state)
            control = proposed.get("control") if isinstance(proposed.get("control"), dict) else {}
            control = dict(control)
            pause = control.get("pause") if isinstance(control.get("pause"), dict) else {}
            if pause.get("active_request_ids") or pause.get("drain_complete") is False:
                decision = {
                    "allowed": False,
                    "reason": "pause_not_drained",
                    "details": {"active_request_ids": list(pause.get("active_request_ids") or [])},
                }
            else:
                control.pop("pause", None)
                control.pop("pause_requested", None)
                if str(control.get("requested_action") or "").lower() in {"pause", "drain"}:
                    control.pop("requested_action", None)
                if control:
                    proposed["control"] = control
                else:
                    proposed.pop("control", None)
                proposed["status"] = "running"
                interlock = evaluate_dispatch_interlock(
                    self.store,
                    loop_id,
                    state=proposed,
                    check_status=False,
                )
                decision = interlock.as_dict()

            if not decision["allowed"]:
                self.store.append_loop_event(
                    loop_id,
                    event_type="loop.resume_blocked",
                    data={"source": source, **decision},
                    actor=event_actor,
                )
                return {"ok": False, "loop_id": loop_id, **decision}

            proposed["next_action"] = self._resume_next_action(state)
            proposed.pop("dispatch_interlock", None)
            self.store.save_loop_state(loop_id, proposed)
            self.store.append_loop_event(
                loop_id,
                event_type="loop.resumed",
                data={"source": source},
                actor=event_actor,
            )
        return {"ok": True, "loop_id": loop_id, "status": "running", "allowed": True}

    @staticmethod
    def _resume_next_action(state: dict[str, Any]) -> dict[str, Any]:
        control = state.get("control") if isinstance(state.get("control"), dict) else {}
        pause = control.get("pause") if isinstance(control.get("pause"), dict) else {}
        resume_action = pause.get("resume_action")
        if isinstance(resume_action, dict) and resume_action:
            return dict(resume_action)
        current_step = state.get("current_step")
        prior = state.get("next_action") if isinstance(state.get("next_action"), dict) else {}
        if current_step:
            kind = str(prior.get("resume_kind") or "run_task")
            return {"kind": kind, "task_id": current_step}
        return {"kind": "evaluate_next"}
