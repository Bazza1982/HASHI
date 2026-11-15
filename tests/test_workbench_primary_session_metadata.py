"""Current conversation discovery must invalidate old frontend Session caches."""
from __future__ import annotations

import json
import sqlite3
import time
from types import SimpleNamespace as NS

import pytest

import orchestrator.workbench_api as workbench_api
from orchestrator.workbench_api import WorkbenchApiServer


def _server(tmp_path, *, online):
    config_path = tmp_path / "agents.json"
    config_path.write_text(json.dumps({
        "global": {},
        "agents": [{"name": "agent", "workspace_dir": "workspaces/agent"}],
    }), encoding="utf-8")
    runtime = NS(
        name="agent",
        get_runtime_metadata=lambda: {
            "session_id": "legacy-worker-boot",
            "context_generation": 99,
            "frontend_session_id": "stale-worker-cache",
            "online": True,
        },
    )
    return WorkbenchApiServer(
        config_path=config_path,
        global_config=NS(
            bridge_home=tmp_path, project_root=tmp_path,
            instance_id="fixture", authorized_id=7,
            deployment_profile="personal",
        ),
        runtimes=[runtime] if online else [],
    )


async def _agents(server):
    response = await server.handle_agents(NS(query={}, headers={}))
    assert response.status == 200
    return json.loads(response.text)["agents"][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("online", [True, False])
async def test_agent_discovery_follows_primary_session_and_context_changes(tmp_path, online):
    server = _server(tmp_path, online=online)
    store = server.session_store
    old = store.resolve_primary_session(
        owner_id="user:7", agent_id="agent", establish=True,
    )
    first = await _agents(server)
    assert first["frontend_session_id"] == old["session_id"]
    assert first["frontend_context_generation"] == old["context_generation"]

    current = store.create_session(owner_id="user:7", agent_id="agent")
    store.bind_primary_session(
        owner_id="user:7", agent_id="agent", session_id=current["session_id"],
    )
    updated = await _agents(server)
    assert updated["frontend_session_id"] == current["session_id"]
    assert updated["frontend_session_id"] != first["frontend_session_id"]

    fresh = store.start_fresh_generation(current["session_id"])
    refreshed = await _agents(server)
    assert refreshed["frontend_session_id"] == fresh["session_id"]
    assert refreshed["frontend_context_generation"] == fresh["context_generation"]
    if online:
        # Native Worker identity is a different contract, not the Chat identity.
        assert refreshed["session_id"] == "legacy-worker-boot"
        assert refreshed["context_generation"] == 99


@pytest.mark.asyncio
async def test_agent_discovery_scopes_current_conversation_to_authenticated_owner(tmp_path):
    server = _server(tmp_path, online=True)
    sessions = {}
    for owner in ("user:7", "user:8"):
        session = server.session_store.create_session(owner_id=owner, agent_id="agent")
        server.session_store.bind_primary_session(
            owner_id=owner, agent_id="agent", session_id=session["session_id"],
        )
        sessions[owner] = session["session_id"]

    first = await _agents(server)
    assert first["frontend_session_id"] == sessions["user:7"]
    server.global_config.authorized_id = 8
    second = await _agents(server)
    assert second["frontend_session_id"] == sessions["user:8"]
    assert first["frontend_session_id"] != second["frontend_session_id"]


@pytest.mark.asyncio
async def test_agent_discovery_batches_sessions_and_reuses_unchanged_config(
    tmp_path, monkeypatch,
):
    config_path = tmp_path / "agents.json"
    configured = [
        {"name": f"agent-{number:02d}", "workspace_dir": f"workspaces/agent-{number:02d}"}
        for number in range(24)
    ]
    config_path.write_text(
        json.dumps({"global": {}, "agents": configured}), encoding="utf-8"
    )
    server = WorkbenchApiServer(
        config_path=config_path,
        global_config=NS(
            bridge_home=tmp_path,
            project_root=tmp_path,
            instance_id="fixture",
            authorized_id=7,
            deployment_profile="personal",
        ),
        reconcile_session_runs=False,
    )
    for row in configured:
        server.session_store.ensure_default_session(
            owner_id="user:7", agent_id=row["name"]
        )

    connection_count = 0
    original_connect = server.session_store._connect

    def counted_connect():
        nonlocal connection_count
        connection_count += 1
        return original_connect()

    config_read_count = 0
    original_read_config = workbench_api.read_config_json

    def counted_read_config(path):
        nonlocal config_read_count
        config_read_count += 1
        return original_read_config(path)

    monkeypatch.setattr(server.session_store, "_connect", counted_connect)
    monkeypatch.setattr(workbench_api, "read_config_json", counted_read_config)

    first = json.loads((await server.handle_agents(NS(query={}, headers={}))).text)
    second = json.loads((await server.handle_agents(NS(query={}, headers={}))).text)

    assert len(first["agents"]) == len(second["agents"]) == 24
    assert all(row.get("frontend_session_id") for row in first["agents"])
    assert connection_count == 2
    assert config_read_count == 1


@pytest.mark.asyncio
async def test_agent_discovery_does_not_wait_for_long_database_busy_timeout(tmp_path):
    server = _server(tmp_path, online=True)
    server.session_store.ensure_default_session(owner_id="user:7", agent_id="agent")
    with sqlite3.connect(server.session_store.db_path) as connection:
        connection.execute("PRAGMA journal_mode=DELETE")
    blocker = sqlite3.connect(server.session_store.db_path, timeout=0, isolation_level=None)
    blocker.execute("PRAGMA locking_mode=EXCLUSIVE")
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        started = time.perf_counter()
        response = await server.handle_agents(NS(query={}, headers={}))
        elapsed = time.perf_counter() - started
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()

    payload = json.loads(response.text)
    assert response.status == 200
    assert payload["session_metadata_degraded"] is True
    assert len(payload["agents"]) == 1
    assert elapsed < 1.0
