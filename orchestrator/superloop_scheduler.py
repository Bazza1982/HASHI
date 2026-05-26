from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchestrator.superloop_issues import SuperloopIssuesService
from orchestrator.superloop_runner import SuperloopRunner
from orchestrator.superloop_store import SuperloopStore, system_actor
from orchestrator.superloop_waits import SuperloopWaitsService


def _parse_ts(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    candidate = raw.strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _wait_deadline(wait: dict[str, Any]) -> datetime | None:
    timeout = wait.get("timeout")
    if isinstance(timeout, dict):
        parsed = _parse_ts(timeout.get("deadline"))
        if parsed is not None:
            return parsed
    details = wait.get("details")
    if isinstance(details, dict):
        parsed = _parse_ts(details.get("until"))
        if parsed is not None:
            return parsed
    return None


def _scheduler_auto_advance_enabled(state: dict[str, Any]) -> bool:
    scheduler_cfg = state.get("scheduler") if isinstance(state.get("scheduler"), dict) else {}
    automation_cfg = state.get("automation") if isinstance(state.get("automation"), dict) else {}
    return bool(
        state.get("scheduler_auto_advance") is True
        or scheduler_cfg.get("auto_advance") is True
        or automation_cfg.get("scheduler_auto_advance") is True
    )


def advance_superloops_once(superloops_root: Path, now: datetime | None = None) -> dict[str, int]:
    now_utc = now or datetime.now(timezone.utc)
    store = SuperloopStore(superloops_root)
    waits = SuperloopWaitsService(store)
    issues = SuperloopIssuesService(store)
    runner = SuperloopRunner(store, waits_service=waits)
    scheduler_actor = system_actor("superloop_scheduler")

    loops_checked = 0
    waits_satisfied = 0
    loops_advanced = 0

    for loop_dir in sorted(store.loops_dir.glob("sl-*")):
        if not loop_dir.is_dir():
            continue
        loop_id = loop_dir.name
        try:
            state = store.load_loop_state(loop_id)
        except FileNotFoundError:
            continue
        status = str(state.get("status") or "")
        if status not in {"running", "paused"}:
            continue
        loops_checked += 1

        wait_ids = waits.pending_wait_ids(loop_id)
        waits_by_id = {str(wait.get("wait_id")): wait for wait in waits.list_waits(loop_id)}
        wait_satisfied_this_loop = False
        for wait_id in wait_ids:
            wait_obj = waits_by_id.get(wait_id)
            if not isinstance(wait_obj, dict):
                continue
            deadline = _wait_deadline(wait_obj)
            if deadline is None or now_utc < deadline:
                continue
            kind = str(wait_obj.get("kind") or "wait")
            resume_policy = wait_obj.get("resume_policy") if isinstance(wait_obj.get("resume_policy"), dict) else {}
            if waits.satisfy_wait(loop_id, wait_id, source="scheduler.deadline", actor=scheduler_actor):
                waits_satisfied += 1
                wait_satisfied_this_loop = True
                if resume_policy.get("on_timeout") == "raise_issue":
                    issues.open_issue(
                        loop_id,
                        title=f"{kind} timeout in {loop_id} ({wait_id})",
                        severity="medium",
                        opened_by_agent="scheduler",
                        opened_by_instance="local",
                        related_task_ids=[str(state.get("current_step") or "")] if state.get("current_step") else [],
                        actor=scheduler_actor,
                    )

        if wait_satisfied_this_loop or (not wait_ids and _scheduler_auto_advance_enabled(state)):
            result = runner.next_action(loop_id)
            if result.get("advanced"):
                loops_advanced += 1

    return {
        "loops_checked": loops_checked,
        "waits_satisfied": waits_satisfied,
        "loops_advanced": loops_advanced,
    }
