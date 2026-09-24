"""Agent-scoped Superloop operations used by HER v2 and command adapters.

The Superloop files are the authoritative state.  This service is deliberately
small: it binds existing recording, compiler, control, taskboard, issue, wait,
runner, and validator owners to one authenticated Agent identity instead of
letting a model edit loop files directly.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchestrator.superloop_compiler import SuperloopCompiler
from orchestrator.superloop_control import SuperloopControlService
from orchestrator.superloop_issues import SuperloopIssuesService
from orchestrator.superloop_recording import SuperloopRecordingService
from orchestrator.superloop_runner import SuperloopRunner
from orchestrator.superloop_store import SuperloopStore, agent_actor
from orchestrator.superloop_taskboard import SuperloopTaskboardService
from orchestrator.superloop_validator import format_validation_report, validate_loop
from orchestrator.superloop_waits import SuperloopWaitsService


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SuperloopAgentService:
    """Expose only operations owned by one Agent identity."""

    def __init__(self, root_dir: Path, *, agent_name: str, instance: str = "HASHI"):
        self.store = SuperloopStore(Path(root_dir))
        self.agent_name = str(agent_name or "").strip()
        self.instance = str(instance or "HASHI").strip() or "HASHI"
        if not self.agent_name:
            raise ValueError("agent_name is required")
        self.actor = agent_actor(
            self.agent_name,
            instance=self.instance,
            source="her_v2_tool",
        )

    def _owned_state(self, loop_id: str) -> dict[str, Any]:
        loop_id = str(loop_id or "").strip()
        if not loop_id:
            raise ValueError("loop_id is required")
        state = self.store.load_loop_state(loop_id)
        controller = state.get("controller") if isinstance(state.get("controller"), dict) else {}
        owner = str(state.get("owner_agent") or controller.get("agent") or "")
        if owner != self.agent_name:
            raise PermissionError(f"Loop {loop_id} does not belong to this agent")
        return state

    def list_loops(self, *, include_deleted: bool = False) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for loop_dir in sorted(self.store.loops_dir.glob("sl-*")):
            if not loop_dir.is_dir():
                continue
            try:
                state = self.store.load_loop_state(loop_dir.name)
            except (FileNotFoundError, ValueError, OSError):
                continue
            controller = state.get("controller") if isinstance(state.get("controller"), dict) else {}
            owner = str(state.get("owner_agent") or controller.get("agent") or "")
            if owner != self.agent_name:
                continue
            if not include_deleted and str(state.get("status") or "") == "deleted":
                continue
            rows.append(
                {
                    "loop_id": state.get("loop_id") or loop_dir.name,
                    "recording_id": state.get("recording_id"),
                    "title": state.get("title"),
                    "status": state.get("status"),
                    "owner_agent": owner,
                    "owner_instance": state.get("owner_instance"),
                    "current_phase": state.get("current_phase"),
                    "current_step": state.get("current_step"),
                    "next_action": state.get("next_action"),
                    "stats": state.get("stats") or {},
                    "updated_at": state.get("updated_at"),
                }
            )
        return rows

    def get_loop(self, loop_id: str) -> dict[str, Any]:
        state = self._owned_state(loop_id)
        taskboard = SuperloopTaskboardService(self.store)
        issues = SuperloopIssuesService(self.store)
        waits = SuperloopWaitsService(self.store)
        return {
            "loop": dict(state),
            "tasks": taskboard.list_tasks(loop_id),
            "issues": issues.list_issues(loop_id),
            "waits": waits.list_waits(loop_id),
        }

    def create_quickstart(self, goal: str, *, task_title: str | None = None) -> dict[str, Any]:
        goal = str(goal or "").strip()
        if not goal:
            raise ValueError("goal is required")
        recording = SuperloopRecordingService(self.store)
        compiler = SuperloopCompiler(self.store)
        started = recording.start_recording(
            goal=goal,
            owner_agent=self.agent_name,
            owner_instance=self.instance,
            source_mode="one_shot_prompt",
        )
        recording_id = started["recording_id"]
        recording.set_intent_summary(
            recording_id,
            intent_summary=goal,
            actor_agent=self.agent_name,
            actor_instance=self.instance,
        )
        recording.record_trial_step(
            recording_id,
            title=f"Bootstrap loop for: {goal}",
            step_kind="human_or_agent_action",
            owner_agent=self.agent_name,
            owner_instance=self.instance,
            execution_mode="simulated",
            success=True,
        )
        recording.set_exit_condition(
            recording_id,
            exit_condition={"kind": "all_tasks_completed", "details": {"task_ids": []}},
            actor_agent=self.agent_name,
            actor_instance=self.instance,
        )
        result = compiler.compile_recording(
            recording_id,
            actor_agent=self.agent_name,
            actor_instance=self.instance,
        )
        if not result.get("ok"):
            return result
        loop_id = str(result["loop_id"])
        self.store.save_loop_state(
            loop_id,
            {**self.store.load_loop_state(loop_id), "status": "running"},
        )
        self.store.append_loop_event(
            loop_id,
            event_type="loop.resumed",
            data={"source": "her_v2_tool"},
            actor=self.actor,
        )
        task = SuperloopTaskboardService(self.store).add_task(
            loop_id,
            title=task_title or f"First actionable task for: {goal}",
            owner_agent=self.agent_name,
            owner_instance=self.instance,
            actor=self.actor,
        )
        return {
            "ok": True,
            "loop_id": loop_id,
            "recording_id": recording_id,
            "seed_task_id": task["task_id"],
            "status": "running",
        }

    def update_loop(self, loop_id: str, *, action: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self._owned_state(loop_id)
        action = str(action or "").strip().lower()
        args = dict(arguments or {})
        if action == "pause":
            mode = str(args.get("mode") or "drain").strip().lower()
            return SuperloopControlService(self.store).pause(
                loop_id, mode=mode, actor=self.actor, source="her_v2_tool"
            )
        if action == "resume":
            return SuperloopControlService(self.store).resume(
                loop_id, actor=self.actor, source="her_v2_tool"
            )
        if action == "next":
            result = SuperloopRunner(self.store).next_action(loop_id)
            return dict(result)
        if action == "closeout":
            report = validate_loop(self.store, loop_id, closeout=True)
            if report.get("blocking"):
                return {
                    "ok": False,
                    "loop_id": loop_id,
                    "status": "blocked",
                    "report": report,
                    "summary": format_validation_report(report),
                }
            state = self.store.load_loop_state(loop_id)
            state["status"] = "completed"
            state["next_action"] = {"kind": "none", "reason": "validated_closeout"}
            self.store.save_loop_state(loop_id, state)
            self.store.append_loop_event(
                loop_id,
                event_type="loop.completed",
                data={"reason": "validated_closeout"},
                actor=self.actor,
            )
            return {"ok": True, "loop_id": loop_id, "status": "completed", "report": report}
        if action == "task_add":
            title = str(args.get("title") or "").strip()
            if not title:
                raise ValueError("title is required")
            return SuperloopTaskboardService(self.store).add_task(
                loop_id,
                title=title,
                owner_agent=self.agent_name,
                owner_instance=self.instance,
                depends_on=[str(item) for item in args.get("depends_on", []) if str(item).strip()],
                actor=self.actor,
            )
        if action == "issue_add":
            title = str(args.get("title") or "").strip()
            if not title:
                raise ValueError("title is required")
            severity = str(args.get("severity") or "medium").strip().lower()
            if severity not in {"low", "medium", "high", "critical"}:
                raise ValueError("severity must be low, medium, high, or critical")
            return SuperloopIssuesService(self.store).open_issue(
                loop_id,
                title=title,
                severity=severity,
                opened_by_agent=self.agent_name,
                opened_by_instance=self.instance,
                related_task_ids=[str(item) for item in args.get("related_task_ids", []) if str(item).strip()],
                actor=self.actor,
            )
        if action == "wait_add":
            kind = str(args.get("kind") or "").strip()
            if not kind:
                raise ValueError("kind is required")
            deadline = str(args.get("deadline") or "").strip() or None
            details = args.get("details") if isinstance(args.get("details"), dict) else None
            return SuperloopWaitsService(self.store).add_wait(
                loop_id,
                kind=kind,
                details=details,
                deadline=deadline,
                actor=self.actor,
            )
        raise ValueError(
            "unsupported action; use pause, resume, next, closeout, task_add, issue_add, or wait_add"
        )

    def delete_loop(self, loop_id: str) -> dict[str, Any]:
        state = self._owned_state(loop_id)
        if str(state.get("status") or "") == "deleted":
            return {"ok": True, "loop_id": loop_id, "status": "deleted", "already_deleted": True}
        state["status"] = "deleted"
        state["deleted_at"] = _utc_now()
        state["next_action"] = {"kind": "none", "reason": "agent_deleted"}
        self.store.save_loop_state(loop_id, state)
        self.store.append_loop_event(
            loop_id,
            event_type="loop.deleted",
            data={"reason": "agent_requested"},
            actor=self.actor,
        )
        return {"ok": True, "loop_id": loop_id, "status": "deleted"}


__all__ = ["SuperloopAgentService"]
