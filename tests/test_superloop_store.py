from __future__ import annotations

import json
from pathlib import Path

from orchestrator.superloop_store import SuperloopStore, agent_actor


def test_store_create_recording_and_append_event(tmp_path: Path) -> None:
    store = SuperloopStore(tmp_path / "superloops")
    state = store.create_recording(
        recording_id="slrec-test-001",
        goal="test goal",
        owner_agent="zelda",
        owner_instance="HASHI1",
        source_mode="one_shot_prompt",
    )
    assert state["recording_id"] == "slrec-test-001"

    store.append_recording_event(
        "slrec-test-001",
        event_type="step.tried",
        data={"step_id": "step-001"},
        actor={"agent": "zelda", "instance": "HASHI1"},
    )
    events_path = tmp_path / "superloops" / "recordings" / "slrec-test-001" / "events.jsonl"
    lines = events_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    payload = json.loads(lines[-1])
    assert payload["kind"] == "step.tried"


def test_store_create_compiled_loop(tmp_path: Path) -> None:
    store = SuperloopStore(tmp_path / "superloops")
    paths = store.create_compiled_loop(
        loop_id="sl-test-001",
        loop_state={"loop_id": "sl-test-001", "status": "paused"},
        taskboard=[],
        issues=[],
        waits=[],
        operator_summary="# summary\n",
    )
    assert Path(paths["state"]).exists()
    loaded = store.load_loop_state("sl-test-001")
    assert loaded["loop_id"] == "sl-test-001"
    events_path = tmp_path / "superloops" / "loops" / "sl-test-001" / "events.jsonl"
    lines = events_path.read_text(encoding="utf-8").strip().splitlines()
    assert lines
    payload = json.loads(lines[-1])
    assert payload["kind"] == "loop.created"


def test_loop_event_without_actor_gets_auditable_default(tmp_path: Path) -> None:
    store = SuperloopStore(tmp_path / "superloops")
    store.create_compiled_loop(
        loop_id="sl-test-actor",
        loop_state={"loop_id": "sl-test-actor", "status": "running"},
        taskboard=[],
        issues=[],
        waits=[],
        operator_summary="# summary\n",
    )

    event = store.append_loop_event("sl-test-actor", event_type="task.started", data={"task_id": "t1"})

    assert event["actor"]["agent"] == "superloop_unknown_writer"
    assert event["actor"]["kind"] == "system"
    assert event["actor"]["reason"] == "actor_not_supplied"


def test_attach_evidence_bundle_updates_loop_state_and_taskboard(tmp_path: Path) -> None:
    store = SuperloopStore(tmp_path / "superloops")
    store.create_compiled_loop(
        loop_id="sl-test-evidence",
        loop_state={
            "loop_id": "sl-test-evidence",
            "status": "running",
            "taskboard_path": "superloops/loops/sl-test-evidence/taskboard.json",
        },
        taskboard=[{"task_id": "task-001", "title": "write report", "status": "completed"}],
        issues=[],
        waits=[],
        operator_summary="# summary\n",
    )

    event = store.attach_evidence_bundle(
        "sl-test-evidence",
        task_id="task-001",
        evidence_bundle_id="evb-001",
        summary="final closeout evidence",
        actor=agent_actor("zelda"),
    )

    assert event["kind"] == "evidence.bundle_attached"
    assert event["data"]["state_changed"] is True
    assert event["data"]["taskboard_changed"] is True

    state = store.load_loop_state("sl-test-evidence")
    assert state["evidence_bundles"] == [
        {
            "task_id": "task-001",
            "evidence_bundle_id": "evb-001",
            "attached_at": state["evidence_bundles"][0]["attached_at"],
            "summary": "final closeout evidence",
        }
    ]

    taskboard_path = tmp_path / "superloops" / "loops" / "sl-test-evidence" / "taskboard.json"
    taskboard = json.loads(taskboard_path.read_text(encoding="utf-8"))
    assert taskboard[0]["evidence_bundle_ids"] == ["evb-001"]
    assert taskboard[0]["enterprise_evidence_bundle_id"] == "evb-001"


def test_attach_evidence_bundle_is_idempotent(tmp_path: Path) -> None:
    store = SuperloopStore(tmp_path / "superloops")
    store.create_compiled_loop(
        loop_id="sl-test-evidence-repeat",
        loop_state={"loop_id": "sl-test-evidence-repeat", "status": "running"},
        taskboard=[{"task_id": "task-001", "status": "completed"}],
        issues=[],
        waits=[],
        operator_summary="# summary\n",
    )

    store.attach_evidence_bundle("sl-test-evidence-repeat", task_id="task-001", evidence_bundle_id="evb-001")
    event = store.attach_evidence_bundle("sl-test-evidence-repeat", task_id="task-001", evidence_bundle_id="evb-001")

    assert event["data"]["state_changed"] is False
    assert event["data"]["taskboard_changed"] is False
    state = store.load_loop_state("sl-test-evidence-repeat")
    assert len(state["evidence_bundles"]) == 1

    taskboard_path = tmp_path / "superloops" / "loops" / "sl-test-evidence-repeat" / "taskboard.json"
    taskboard = json.loads(taskboard_path.read_text(encoding="utf-8"))
    assert taskboard[0]["evidence_bundle_ids"] == ["evb-001"]
