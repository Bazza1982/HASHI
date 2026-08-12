from __future__ import annotations

import fcntl
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from orchestrator.superloop_store import SuperloopStore


ACTIVE_ISSUE_STATUSES = {"open", "in_progress"}
PAUSE_SIGNAL_FILENAMES = ("_pause", ".pause", "pause.signal")
HALT_SIGNAL_FILENAMES = ("_halt", ".halt", "halt.signal")


@dataclass(frozen=True)
class DispatchInterlockDecision:
    allowed: bool
    reason: str
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "details": dict(self.details),
        }


class DispatchInterlockError(RuntimeError):
    def __init__(self, decision: DispatchInterlockDecision):
        super().__init__(f"Superloop dispatch blocked: {decision.reason}")
        self.decision = decision


@contextmanager
def loop_dispatch_lock(store: SuperloopStore, loop_id: str) -> Iterator[None]:
    """Serialize dispatch acceptance with pause/resume state transitions."""

    loop_dir = store.loop_dir(loop_id)
    if not (loop_dir / "state.json").is_file():
        raise FileNotFoundError(f"Loop state not found: {loop_id}")
    lock_path = loop_dir / ".dispatch.lock"
    with open(lock_path, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def guarded_dispatch(store: SuperloopStore, loop_id: str) -> Iterator[DispatchInterlockDecision]:
    """Hold the loop dispatch lock from final preflight through acceptance.

    A transport must keep this context open until its outbound request has
    either been rejected or accepted and durably recorded. This makes a
    completed pause a hard boundary: no later packet can cross it.
    """

    with loop_dispatch_lock(store, loop_id):
        decision = evaluate_dispatch_interlock(store, loop_id)
        if not decision.allowed:
            raise DispatchInterlockError(decision)
        yield decision


def evaluate_dispatch_interlock(
    store: SuperloopStore,
    loop_id: str,
    *,
    state: dict[str, Any] | None = None,
    check_status: bool = True,
    check_pause_signals: bool = True,
) -> DispatchInterlockDecision:
    """Return the fail-closed decision that must precede a worker dispatch.

    The gate is intentionally independent of the runner and scheduler so every
    dispatch transport can call the same contract immediately before sending a
    packet. Missing optional candidate metadata stays backwards compatible, but
    an explicitly invalid candidate always blocks.
    """

    loop_state = dict(state) if isinstance(state, dict) else store.load_loop_state(loop_id)
    status = str(loop_state.get("status") or "").strip().lower()
    if check_status and status != "running":
        reason = "paused" if status == "paused" else "loop_not_running"
        return DispatchInterlockDecision(False, reason, {"status": status or "missing"})

    if check_pause_signals:
        control_reason = _state_control_signal(loop_state)
        if control_reason:
            return DispatchInterlockDecision(False, control_reason, {"source": "state"})
        file_signal = _file_control_signal(store.loop_dir(loop_id))
        if file_signal:
            reason, path = file_signal
            return DispatchInterlockDecision(False, reason, {"source": "file", "path": path.name})

    candidate_reason = _candidate_block_reason(loop_state)
    if candidate_reason:
        return DispatchInterlockDecision(
            False,
            "candidate_invalid",
            {"candidate_reason": candidate_reason},
        )

    issue_ids = _blocking_issue_ids(store, loop_id, loop_state)
    if issue_ids:
        return DispatchInterlockDecision(
            False,
            "open_blocker_issues",
            {"issue_ids": issue_ids, "phase": str(loop_state.get("current_phase") or "")},
        )

    return DispatchInterlockDecision(True, "ready")


def _state_control_signal(state: dict[str, Any]) -> str | None:
    control = state.get("control") if isinstance(state.get("control"), dict) else {}
    pause = control.get("pause") if isinstance(control.get("pause"), dict) else {}
    requested_action = str(control.get("requested_action") or "").strip().lower()
    if requested_action in {"halt", "abort", "stop"}:
        return "halt_requested"
    if requested_action in {"pause", "drain"}:
        return "pause_requested"
    if state.get("halt_requested") is True or control.get("halt_requested") is True:
        return "halt_requested"
    if (
        state.get("pause_requested") is True
        or control.get("pause_requested") is True
        or pause.get("requested_at")
    ):
        return "pause_requested"
    return None


def _file_control_signal(loop_dir: Path) -> tuple[str, Path] | None:
    for filename in HALT_SIGNAL_FILENAMES:
        path = loop_dir / filename
        if path.exists():
            return "halt_requested", path
    for filename in PAUSE_SIGNAL_FILENAMES:
        path = loop_dir / filename
        if path.exists():
            return "pause_requested", path
    return None


def _candidate_block_reason(state: dict[str, Any]) -> str | None:
    candidate = state.get("candidate")
    if not isinstance(candidate, dict):
        return None
    candidate_has_identity = any(
        candidate.get(key)
        for key in (
            "hash",
            "candidate_hash",
            "hashi_commit",
            "source_commit",
            "package_sha256",
            "frozen_at",
        )
    )
    if not candidate_has_identity and state.get("candidate_required_for_dispatch") is not True:
        # Templates may carry an empty candidate placeholder whose booleans are
        # false until the build/freeze step. It is not an invalidated candidate
        # and must not block preflight or deterministic preparation work.
        return None
    if candidate.get("evidence_valid") is False:
        return "evidence_valid=false"
    if candidate.get("valid") is False:
        return "valid=false"
    candidate_status = str(candidate.get("status") or "").strip().lower()
    if candidate_status in {"invalid", "invalidated", "superseded", "stale"}:
        return f"status={candidate_status}"
    if candidate.get("invalidated_at"):
        return "invalidated_at is set"
    if state.get("candidate_invalidated") is True:
        return "state candidate_invalidated=true"
    return None


def _blocking_issue_ids(
    store: SuperloopStore,
    loop_id: str,
    state: dict[str, Any],
) -> list[str]:
    issues_path = store.resolve_loop_path(loop_id, state.get("issues_path"), "issues.json")
    phase_keys = _phase_block_keys(str(state.get("current_phase") or ""))
    blocked: list[str] = []
    for index, issue in enumerate(store.load_loop_json_list(issues_path)):
        if str(issue.get("status") or "").strip().lower() not in ACTIVE_ISSUE_STATUSES:
            continue
        explicit = issue.get("blocks_dispatch") is True or issue.get("blocks_current_phase") is True
        phase_block = any(issue.get(key) is True for key in phase_keys)
        if explicit or phase_block:
            blocked.append(str(issue.get("issue_id") or f"issues[{index}]"))
    return blocked


def _phase_block_keys(raw_phase: str) -> set[str]:
    normalized = "_".join(raw_phase.strip().lower().replace("-", "_").split())
    if not normalized:
        return set()
    aliases = {normalized}
    for prefix in ("layer_a", "stage_1", "stage_2", "final"):
        if normalized == prefix or normalized.startswith(prefix + "_"):
            aliases.add(prefix)
    return {f"blocks_{alias}" for alias in aliases}
