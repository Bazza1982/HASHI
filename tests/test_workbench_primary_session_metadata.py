"""Current conversation discovery must invalidate old frontend Session caches."""
from __future__ import annotations

import json
from types import SimpleNamespace as NS

import pytest

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
