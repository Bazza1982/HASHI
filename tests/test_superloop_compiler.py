from __future__ import annotations

from pathlib import Path

from orchestrator.superloop_compiler import SuperloopCompiler
from orchestrator.superloop_recording import SuperloopRecordingService
from orchestrator.superloop_store import SuperloopStore


def test_compile_blocked_when_missing_required_fields(tmp_path: Path) -> None:
    store = SuperloopStore(tmp_path / "superloops")
    service = SuperloopRecordingService(store)
    compiler = SuperloopCompiler(store)
    service.start_recording(
        goal="",
        owner_agent="zelda",
        owner_instance="HASHI1",
        recording_id="slrec-test-001",
    )
    result = compiler.compile_recording("slrec-test-001")
    assert result["ok"] is False
    assert result["code"] == "compile_blocked"


def test_compile_success(tmp_path: Path) -> None:
    store = SuperloopStore(tmp_path / "superloops")
    service = SuperloopRecordingService(store)
    compiler = SuperloopCompiler(store)
    service.start_recording(
        goal="coordinate review loop",
        owner_agent="zelda",
        owner_instance="HASHI1",
        recording_id="slrec-test-001",
    )
    service.set_intent_summary(
        "slrec-test-001",
        intent_summary="Loop until all review tasks are complete.",
        actor_agent="zelda",
        actor_instance="HASHI1",
    )
    service.set_exit_condition(
        "slrec-test-001",
        exit_condition={"kind": "all_tasks_completed", "details": {"task_ids": ["step-001"]}},
        actor_agent="zelda",
        actor_instance="HASHI1",
    )
    service.record_trial_step(
        "slrec-test-001",
        title="Request INTEL review",
        step_kind="remote_hchat",
        owner_agent="agent1",
        owner_instance="INTEL",
    )
    result = compiler.compile_recording("slrec-test-001", loop_id="sl-test-001")
    assert result["ok"] is True
    assert result["loop_id"] == "sl-test-001"
    loop_state = store.load_loop_state("sl-test-001")
    assert loop_state["recording_id"] == "slrec-test-001"
