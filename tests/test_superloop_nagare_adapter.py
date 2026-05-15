from __future__ import annotations

from pathlib import Path

from orchestrator.superloop_nagare_adapter import SuperloopNagareAdapter
from orchestrator.superloop_store import SuperloopStore


def test_nagare_adapter_add_and_complete_child(tmp_path: Path) -> None:
    store = SuperloopStore(tmp_path / "superloops")
    store.create_compiled_loop(
        loop_id="sl-test-001",
        loop_state={"loop_id": "sl-test-001", "status": "running", "child_runs": []},
        taskboard=[],
        issues=[],
        waits=[],
        operator_summary="# summary\n",
    )
    adapter = SuperloopNagareAdapter(store)
    child = adapter.add_child_workflow("sl-test-001", workflow_path="flow/workflows/library/smoke_test.yaml")
    assert child["child_id"] == "child-001"
    assert child["status"] == "planned"
    assert adapter.mark_child_completed("sl-test-001", "child-001", run_id="run-smoke-001") is True
    state = store.load_loop_state("sl-test-001")
    assert state["child_runs"][0]["status"] == "completed"
    assert state["child_runs"][0]["run_id"] == "run-smoke-001"
