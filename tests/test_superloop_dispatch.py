from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.superloop_dispatch import SuperloopDispatchLedger
from orchestrator.superloop_store import SuperloopStore


def _create_loop(store: SuperloopStore) -> None:
    store.create_compiled_loop(
        loop_id="sl-dispatch",
        loop_state={
            "loop_id": "sl-dispatch",
            "status": "running",
            "active_dispatch_id": "dispatch-1",
        },
        taskboard=[],
        issues=[],
        waits=[],
        operator_summary="# summary\n",
    )


def test_dispatch_terminal_outcome_is_explicit_and_clears_active_state(tmp_path: Path) -> None:
    store = SuperloopStore(tmp_path / "superloops")
    _create_loop(store)
    ledger = SuperloopDispatchLedger(store)
    ledger.record_started(
        "sl-dispatch",
        dispatch_instance_id="dispatch-1",
        task_id="task-1",
        request_id="req-1",
    )

    row = ledger.record_terminal(
        "sl-dispatch",
        dispatch_instance_id="dispatch-1",
        task_id="task-1",
        request_id="req-1",
        outcome="aborted",
        reason="operator_stop",
        classification="excluded_from_certification",
    )

    assert row["terminal"] is True
    assert row["outcome"] == "aborted"
    assert "verdict" not in row
    assert store.load_loop_state("sl-dispatch")["active_dispatch_id"] is None
    rows = [
        json.loads(line)
        for line in (store.loop_dir("sl-dispatch") / "dispatches.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [item["status"] for item in rows] == ["accepted", "aborted"]


def test_aborted_dispatch_requires_reason(tmp_path: Path) -> None:
    store = SuperloopStore(tmp_path / "superloops")
    _create_loop(store)

    with pytest.raises(ValueError, match="requires an explicit reason"):
        SuperloopDispatchLedger(store).record_terminal(
            "sl-dispatch",
            dispatch_instance_id="dispatch-1",
            task_id="task-1",
            request_id="req-1",
            outcome="aborted",
        )
