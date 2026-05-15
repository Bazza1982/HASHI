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
    assert created["wait_id"].startswith("wait-")
    assert waits.has_open_waits("sl-test-001") is True
    assert waits.satisfy_wait("sl-test-001", created["wait_id"], source="test") is True
    assert waits.has_open_waits("sl-test-001") is False


def test_waits_path_must_stay_under_superloops_root(tmp_path: Path) -> None:
    store = SuperloopStore(tmp_path / "superloops")
    store.create_compiled_loop(
        loop_id="sl-test-escape",
        loop_state={"loop_id": "sl-test-escape", "status": "running", "waits_path": "../escape.json"},
        taskboard=[],
        issues=[],
        waits=[],
        operator_summary="# summary\n",
    )
    waits = SuperloopWaitsService(store)
    try:
        waits.list_waits("sl-test-escape")
        assert False, "expected ValueError for escaping waits path"
    except ValueError:
        pass
