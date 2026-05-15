from __future__ import annotations

from pathlib import Path

from orchestrator.superloop_store import SuperloopStore
from orchestrator.superloop_waits import SuperloopWaitsService


def test_waits_add_and_satisfy(tmp_path: Path) -> None:
    store = SuperloopStore(tmp_path / "superloops")
    store.create_compiled_loop(
        loop_id="sl-test-001",
        loop_state={"loop_id": "sl-test-001", "status": "running", "waits_path": "superloops/loops/sl-test-001/waits.json"},
        taskboard=[],
        issues=[],
        waits=[],
        operator_summary="# summary\n",
    )
    waits = SuperloopWaitsService(store)
    created = waits.add_wait("sl-test-001", kind="await_hchat_reply")
    assert created["wait_id"] == "wait-001"
    assert waits.has_open_waits("sl-test-001") is True
    assert waits.satisfy_wait("sl-test-001", "wait-001", source="test") is True
    assert waits.has_open_waits("sl-test-001") is False
