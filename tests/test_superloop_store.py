from __future__ import annotations

import json
from pathlib import Path

from orchestrator.superloop_store import SuperloopStore


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
