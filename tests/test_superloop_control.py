from __future__ import annotations

import json
from pathlib import Path
from threading import Event, Thread

import orchestrator.superloop_interlock as superloop_interlock

from orchestrator.superloop_control import SuperloopControlService
from orchestrator.superloop_interlock import DispatchInterlockError, guarded_dispatch
from orchestrator.superloop_store import SuperloopStore, agent_actor


def _create_loop(
    store: SuperloopStore,
    loop_id: str,
    *,
    issues: list[dict[str, object]] | None = None,
    active_dispatch_id: str | None = None,
) -> None:
    store.create_compiled_loop(
        loop_id=loop_id,
        loop_state={
            "loop_id": loop_id,
            "status": "running",
            "current_phase": "stage_1_flash_cheap",
            "current_step": "task-001",
            "active_dispatch_id": active_dispatch_id,
            "next_action": {"kind": "dispatch_task", "task_id": "task-001"},
            "taskboard_path": f"superloops/loops/{loop_id}/taskboard.json",
            "issues_path": f"superloops/loops/{loop_id}/issues.json",
            "waits_path": f"superloops/loops/{loop_id}/waits.json",
        },
        taskboard=[{"task_id": "task-001", "status": "in_progress"}],
        issues=issues or [],
        waits=[],
        operator_summary="# summary\n",
    )


def test_pause_persists_hard_boundary_and_drain_state(tmp_path: Path) -> None:
    store = SuperloopStore(tmp_path / "superloops")
    _create_loop(store, "sl-control-pause", active_dispatch_id="req-0082")

    result = SuperloopControlService(store).pause(
        "sl-control-pause",
        mode="drain",
        actor=agent_actor("controller"),
    )

    assert result["status"] == "paused"
    assert result["drain_complete"] is False
    assert result["active_request_ids"] == ["req-0082"]
    state = store.load_loop_state("sl-control-pause")
    assert state["status"] == "paused"
    assert state["next_action"]["kind"] == "await_operator_resume"
    assert state["control"]["pause"]["resume_action"] == {
        "kind": "dispatch_task",
        "task_id": "task-001",
    }


def test_resume_requires_drain_and_restores_prior_action(tmp_path: Path) -> None:
    store = SuperloopStore(tmp_path / "superloops")
    _create_loop(store, "sl-control-resume", active_dispatch_id="req-0082")
    control = SuperloopControlService(store)
    control.pause("sl-control-resume")

    blocked = control.resume("sl-control-resume")
    assert blocked["ok"] is False
    assert blocked["reason"] == "pause_not_drained"

    control.mark_drained("sl-control-resume")
    resumed = control.resume("sl-control-resume")

    assert resumed["ok"] is True
    state = store.load_loop_state("sl-control-resume")
    assert state["status"] == "running"
    assert state["next_action"] == {"kind": "dispatch_task", "task_id": "task-001"}
    assert "pause" not in state.get("control", {})


def test_resume_is_blocked_by_open_phase_issue(tmp_path: Path) -> None:
    store = SuperloopStore(tmp_path / "superloops")
    _create_loop(
        store,
        "sl-control-blocked",
        issues=[
            {
                "issue_id": "issue-live",
                "status": "open",
                "blocks_stage_1": True,
            }
        ],
    )
    control = SuperloopControlService(store)
    control.pause("sl-control-blocked")

    result = control.resume("sl-control-blocked")

    assert result["ok"] is False
    assert result["reason"] == "open_blocker_issues"
    assert result["details"]["issue_ids"] == ["issue-live"]
    assert store.load_loop_state("sl-control-blocked")["status"] == "paused"
    events = [
        json.loads(line)
        for line in (store.loop_dir("sl-control-blocked") / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert events[-1]["kind"] == "loop.resume_blocked"


def test_completed_pause_is_a_hard_boundary_for_dispatch_acceptance(tmp_path: Path) -> None:
    store = SuperloopStore(tmp_path / "superloops")
    _create_loop(store, "sl-control-race")
    entered_dispatch = Event()
    release_dispatch = Event()
    pause_finished = Event()

    def hold_accepted_dispatch() -> None:
        with guarded_dispatch(store, "sl-control-race"):
            entered_dispatch.set()
            assert release_dispatch.wait(timeout=2)

    def pause_loop() -> None:
        SuperloopControlService(store).pause("sl-control-race")
        pause_finished.set()

    dispatch_thread = Thread(target=hold_accepted_dispatch)
    pause_thread = Thread(target=pause_loop)
    dispatch_thread.start()
    assert entered_dispatch.wait(timeout=2)
    pause_thread.start()
    assert not pause_finished.wait(timeout=0.1)

    release_dispatch.set()
    dispatch_thread.join(timeout=2)
    pause_thread.join(timeout=2)
    assert pause_finished.is_set()

    try:
        with guarded_dispatch(store, "sl-control-race"):
            raise AssertionError("paused loop admitted a new dispatch")
    except DispatchInterlockError as exc:
        assert exc.decision.reason == "paused"


def test_dispatch_lock_uses_windows_file_lock_fallback(tmp_path: Path, monkeypatch) -> None:
    store = SuperloopStore(tmp_path / "superloops")
    _create_loop(store, "sl-control-windows-lock")

    class FakeMsvcrt:
        LK_LOCK = 1
        LK_UNLCK = 2

        def __init__(self) -> None:
            self.calls: list[tuple[int, int]] = []

        def locking(self, _fileno: int, mode: int, size: int) -> None:
            self.calls.append((mode, size))

    fake_msvcrt = FakeMsvcrt()
    monkeypatch.setattr(superloop_interlock, "fcntl", None)
    monkeypatch.setattr(superloop_interlock, "msvcrt", fake_msvcrt)

    with superloop_interlock.loop_dispatch_lock(store, "sl-control-windows-lock"):
        assert fake_msvcrt.calls == [(fake_msvcrt.LK_LOCK, 1)]

    assert fake_msvcrt.calls == [
        (fake_msvcrt.LK_LOCK, 1),
        (fake_msvcrt.LK_UNLCK, 1),
    ]
