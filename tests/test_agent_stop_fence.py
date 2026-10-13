from __future__ import annotations

from orchestrator.agent_stop_fence import (
    AgentStopFenceStore,
    record_predates_fence,
)


def test_stop_fence_is_monotonic_and_survives_store_reopen(tmp_path):
    path = tmp_path / "state" / "runtime_control" / "agent_stop_fences.db"
    store = AgentStopFenceStore(path)

    first = store.advance("zelda", request_id="req-1", source="telegram")
    second = store.advance("zelda", request_id="req-2", source="workbench")
    restored = AgentStopFenceStore(path).current("zelda")

    assert first.epoch == 1
    assert second.epoch == 2
    assert restored == second


def test_stop_fence_rejects_only_older_origin_epoch(tmp_path):
    store = AgentStopFenceStore(tmp_path / "fences.db")
    fence = store.advance("zelda")

    assert record_predates_fence(
        fence,
        origin={"agent_stop_epoch": fence.epoch - 1},
    )
    assert not record_predates_fence(
        fence,
        origin={"agent_stop_epoch": fence.epoch},
    )
