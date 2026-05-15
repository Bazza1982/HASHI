from __future__ import annotations

from pathlib import Path

from orchestrator.superloop_recording import SuperloopRecordingService
from orchestrator.superloop_store import SuperloopStore


def test_recording_start_and_trial(tmp_path: Path) -> None:
    store = SuperloopStore(tmp_path / "superloops")
    service = SuperloopRecordingService(store)

    started = service.start_recording(
        goal="coordinate review loop",
        owner_agent="zelda",
        owner_instance="HASHI1",
        recording_id="slrec-test-001",
        source_mode="one_shot_prompt",
    )
    assert started["ok"] is True

    service.set_intent_summary(
        "slrec-test-001",
        intent_summary="Use peer review until all tasks are done.",
        actor_agent="zelda",
        actor_instance="HASHI1",
    )
    service.set_exit_condition(
        "slrec-test-001",
        exit_condition={"kind": "all_tasks_completed", "details": {"task_ids": ["step-001"]}},
        actor_agent="zelda",
        actor_instance="HASHI1",
    )
    trial = service.record_trial_step(
        "slrec-test-001",
        title="Request review from INTEL",
        step_kind="remote_hchat",
        owner_agent="agent1",
        owner_instance="INTEL",
    )
    assert trial["ok"] is True
    state = store.load_recording_state("slrec-test-001")
    assert state["finish_ready"] is True
    assert len(state["candidate_steps"]) == 1
