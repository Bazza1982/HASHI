from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

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


@pytest.mark.parametrize("phase", ["initialize", "current", "advance", "rollback"])
def test_stop_fence_releases_database_after_operation(tmp_path, monkeypatch, phase):
    connections = []
    original = AgentStopFenceStore._connect

    def track(store):
        connection = original(store)
        connections.append(connection)
        return connection

    monkeypatch.setattr(AgentStopFenceStore, "_connect", track)
    path = tmp_path / "fences.db"
    store = AgentStopFenceStore(path)
    if phase != "initialize":
        for connection in connections:
            connection.close()
        connections.clear()
    if phase == "current":
        assert store.current("probe").epoch == 0
    elif phase == "advance":
        assert store.advance("probe").epoch == 1
    elif phase == "rollback":
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute(
                "CREATE TRIGGER reject_stop BEFORE INSERT ON agent_stop_fences "
                "BEGIN SELECT RAISE(ABORT, 'injected stop write failure'); END"
            )
        with pytest.raises(sqlite3.IntegrityError, match="injected stop write failure"):
            store.advance("probe")
    with closing(sqlite3.connect(path)) as connection:
        row = connection.execute(
            "SELECT epoch FROM agent_stop_fences WHERE agent = ?", ("probe",)
        ).fetchone()
        assert row == ((1,) if phase == "advance" else None)
    assert connections
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")
