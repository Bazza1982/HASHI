"""Desktop entry reuses the AP route and canonical primary Session owner."""
import asyncio
import threading

import pytest
from aiohttp.test_utils import TestClient, TestServer

from orchestrator import agent_presence_notify, agent_presence_settings
from tests.test_agentpresence_controls import server_at


@pytest.mark.asyncio
async def test_local_desktop_context_is_primary_and_follows_route(tmp_path, monkeypatch):
    api = server_at(tmp_path)
    monkeypatch.setattr(agent_presence_notify, "token_matches", lambda token: token == "secret")
    headers = {"X-AP-Token": "secret"}
    async with TestClient(TestServer(api.app)) as client:
        path = "/api/agentpresence/desktop-context?instance_id=TEST"
        assert (await client.get(path)).status == 401
        assert (await client.get(path.replace("TEST", "OTHER"), headers=headers)).status == 409
        first = await client.get(path, headers=headers)
        assert first.status == 200
        body = await first.json()
        primary = api.session_store.resolve_primary_session(owner_id="user:7", agent_id="agent1", establish=True)
        assert body["agent_id"] == "agent1"
        assert body["session_id"] == primary["session_id"]
        assert body["context_generation"] == primary["context_generation"]
        next_session = api.session_store.create_session(owner_id="user:7", agent_id="agent1")
        api.session_store.bind_primary_session(owner_id="user:7", agent_id="agent1", session_id=next_session["session_id"])
        assert (await (await client.get(path, headers=headers)).json())["session_id"] == next_session["session_id"]
        agent_presence_settings.set_default_agent(tmp_path, "agent2")
        changed = await (await client.get(path, headers=headers)).json()
        assert changed["agent_id"] == "agent2"
        assert changed["route_revision"] > body["route_revision"]


@pytest.mark.asyncio
async def test_remote_context_uses_existing_auth_and_rejects_wrong_instance(tmp_path, monkeypatch):
    api = server_at(tmp_path)
    async with TestClient(TestServer(api.app)) as client:
        path = "/api/v1/agentpresence/desktop-context?instance_id=TEST"
        assert (await client.get(path)).status == 401
        monkeypatch.setattr(api, "_strict_authenticated_owner", lambda request: "user:7")
        response = await client.get(path)
        assert response.status == 200
        assert (await response.json())["instance_id"] == "TEST"
        assert (await client.get(path.replace("TEST", "OTHER"))).status == 409
        monkeypatch.setattr(api, "_strict_authenticated_owner", lambda request: "user:8")
        assert (await client.get(path)).status == 403


@pytest.mark.asyncio
async def test_primary_lock_wait_does_not_block_other_api_requests(tmp_path, monkeypatch):
    api = server_at(tmp_path)
    monkeypatch.setattr(agent_presence_notify, "token_matches", lambda token: token == "secret")
    entered, release = threading.Event(), threading.Event()
    original = api.session_store.resolve_primary_session
    def locked(**kwargs):
        entered.set()
        assert release.wait(10)
        return original(**kwargs)
    monkeypatch.setattr(api.session_store, "resolve_primary_session", locked)
    async with TestClient(TestServer(api.app)) as client:
        request = asyncio.create_task(client.get("/api/agentpresence/desktop-context?instance_id=TEST",
                                               headers={"X-AP-Token": "secret"}))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            assert not request.done()
            unrelated = await asyncio.wait_for(client.get("/api/agentpresence/controls?instance_id=OTHER",
                                                         headers={"X-AP-Token": "secret"}), 5)
            assert unrelated.status == 409
            assert not request.done() and not release.is_set()
        finally:
            release.set()
            assert (await request).status == 200


@pytest.mark.asyncio
async def test_route_changed_during_primary_resolution_is_conflict(tmp_path, monkeypatch):
    api = server_at(tmp_path)
    monkeypatch.setattr(agent_presence_notify, "token_matches", lambda token: token == "secret")
    original = api.session_store.resolve_primary_session
    def moved(**kwargs):
        result = original(**kwargs)
        agent_presence_settings.set_default_agent(tmp_path, "agent2")
        return result
    monkeypatch.setattr(api.session_store, "resolve_primary_session", moved)
    async with TestClient(TestServer(api.app)) as client:
        response = await client.get("/api/agentpresence/desktop-context?instance_id=TEST",
                                    headers={"X-AP-Token": "secret"})
        assert response.status == 409
        assert (await response.json())["error_code"] == "agentpresence_route_changed"
# HASHI-Fingerprint: hfp_4bc909b25ee5454ea29a793ceaa505b2
