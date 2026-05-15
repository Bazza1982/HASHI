from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from orchestrator.superloop_store import SuperloopStore


class SuperloopCompiler:
    def __init__(self, store: SuperloopStore):
        self.store = store

    def compile_recording(
        self,
        recording_id: str,
        *,
        loop_id: str | None = None,
        actor_agent: str = "system",
        actor_instance: str = "HASHI",
    ) -> dict[str, Any]:
        state = self.store.load_recording_state(recording_id)
        missing = self._compile_missing(state)
        if missing:
            self.store.append_recording_event(
                recording_id,
                event_type="recording.finish_blocked",
                data={"missing": missing},
                actor={"agent": actor_agent, "instance": actor_instance},
            )
            return {
                "ok": False,
                "recording_id": recording_id,
                "code": "compile_blocked",
                "missing": missing,
            }

        compiled_loop_id = loop_id or self.store.generate_loop_id()
        accepted_steps = [item for item in state.get("candidate_steps", []) if item.get("status") == "accepted"]
        taskboard = self._build_taskboard(accepted_steps)
        waits = list(state.get("candidate_waits") or [])
        issues = list(state.get("candidate_issues") or state.get("candidate_issues", []))
        if not issues:
            issues = []
        loop_state = self._build_loop_state(recording_state=state, loop_id=compiled_loop_id)
        summary = self._build_summary(loop_state=loop_state, taskboard=taskboard, waits=waits, issues=issues)

        compiled_paths = self.store.create_compiled_loop(
            loop_id=compiled_loop_id,
            loop_state=loop_state,
            taskboard=taskboard,
            issues=issues,
            waits=waits,
            operator_summary=summary,
        )
        self.store.append_loop_event(
            compiled_loop_id,
            event_type="loop.created",
            data={"recording_id": recording_id},
            actor={"agent": actor_agent, "instance": actor_instance},
        )

        state["status"] = "compiled"
        state["compiled_loop_id"] = compiled_loop_id
        state["finish_ready"] = True
        self.store.save_recording_state(recording_id, state)
        self.store.append_recording_event(
            recording_id,
            event_type="recording.finished",
            data={"loop_id": compiled_loop_id},
            actor={"agent": actor_agent, "instance": actor_instance},
        )

        return {
            "ok": True,
            "recording_id": recording_id,
            "loop_id": compiled_loop_id,
            "compiled_paths": compiled_paths,
        }

    @staticmethod
    def _compile_missing(state: dict[str, Any]) -> list[str]:
        missing: list[str] = []
        if not str(state.get("goal") or "").strip():
            missing.append("goal")
        if not str(state.get("intent_summary") or "").strip():
            missing.append("intent_summary")
        if not isinstance(state.get("exit_condition_draft"), dict):
            missing.append("exit_condition_draft")
        accepted_steps = [item for item in state.get("candidate_steps", []) if item.get("status") == "accepted"]
        if not accepted_steps:
            missing.append("accepted_step_ids")
        return missing

    @staticmethod
    def _build_taskboard(accepted_steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc).isoformat()
        taskboard: list[dict[str, Any]] = []
        for item in accepted_steps:
            taskboard.append(
                {
                    "task_id": item.get("step_id"),
                    "title": item.get("title", item.get("step_id")),
                    "description": item.get("title", ""),
                    "status": "pending",
                    "owner_agent": item.get("owner_agent"),
                    "owner_instance": item.get("owner_instance"),
                    "depends_on": list(item.get("depends_on") or []),
                    "priority": "normal",
                    "created_at": now,
                    "updated_at": now,
                    "artifact_refs": [],
                    "notes": [],
                }
            )
        return taskboard

    @staticmethod
    def _build_loop_state(*, recording_state: dict[str, Any], loop_id: str) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        owner_agent = str(recording_state.get("owner_agent") or "")
        owner_instance = str(recording_state.get("owner_instance") or "")
        return {
            "loop_id": loop_id,
            "recording_id": recording_state.get("recording_id"),
            "title": f"Superloop from {recording_state.get('recording_id')}",
            "status": "paused",
            "owner_agent": owner_agent,
            "owner_instance": owner_instance,
            "controller": {"agent": owner_agent, "instance": owner_instance, "mode": "superloop_controller"},
            "participants": list(recording_state.get("candidate_agents") or []),
            "created_at": now,
            "updated_at": now,
            "started_at": None,
            "ended_at": None,
            "current_phase": "planned",
            "current_step": None,
            "next_action": {"kind": "await_operator_start"},
            "exit_condition": recording_state.get("exit_condition_draft"),
            "taskboard_path": f"superloops/loops/{loop_id}/taskboard.json",
            "issues_path": f"superloops/loops/{loop_id}/issues.json",
            "waits_path": f"superloops/loops/{loop_id}/waits.json",
            "child_runs": list(recording_state.get("candidate_nagare_runs") or []),
            "artifacts": list(recording_state.get("candidate_artifacts") or []),
            "stats": {
                "task_total": len([item for item in recording_state.get("candidate_steps", []) if item.get("status") == "accepted"]),
                "task_completed": 0,
                "issue_open": 0,
                "wait_open": len(recording_state.get("candidate_waits") or []),
            },
            "operator_summary_path": f"superloops/loops/{loop_id}/README.md",
        }

    @staticmethod
    def _build_summary(
        *,
        loop_state: dict[str, Any],
        taskboard: list[dict[str, Any]],
        waits: list[dict[str, Any]],
        issues: list[dict[str, Any]],
    ) -> str:
        lines = [
            f"# {loop_state['title']}",
            "",
            f"- loop_id: `{loop_state['loop_id']}`",
            f"- recording_id: `{loop_state['recording_id']}`",
            f"- status: `{loop_state['status']}`",
            f"- owner: `{loop_state['owner_agent']}@{loop_state['owner_instance']}`",
            "",
            "## Snapshot",
            "",
            f"- tasks: {len(taskboard)}",
            f"- waits: {len(waits)}",
            f"- issues: {len(issues)}",
        ]
        return "\n".join(lines) + "\n"
