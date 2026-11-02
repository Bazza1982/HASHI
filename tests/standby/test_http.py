"""Actual aiohttp/authenticated S1 protocol: no media/inference admission."""

import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from orchestrator.session_store import SessionStore
from orchestrator.standby.routes import register_standby_api


@pytest.fixture
def api(tmp_path):
    store = SessionStore(tmp_path / "sessions.sqlite3", instance_id="SB-HTTP")
    session = store.ensure_default_session(owner_id="user:7", agent_id="alice")
    runtime = SimpleNamespace(get_runtime_metadata=lambda: {"privacy_level": 1})
    owner = SimpleNamespace(app=web.Application(), session_store=store,
        config_path=tmp_path / "agents.json", admin_token="synthetic-test-token",
        _is_governed_profile=lambda: False,
        _check_admin_auth=lambda request: request.headers.get("Authorization") == "Bearer synthetic-test-token",
        _v1_owner_id=lambda request: "user:7", _persistent_session_v1_ready=lambda: True,
        _runtime_map=lambda: {"alice": runtime})
    register_standby_api(owner)
    return owner, {key:session[key] for key in ("agent_id","session_id","context_generation")}


async def test_http_auth_strict_body_no_store_and_default_closed(api, tmp_path):
    owner, scope = api
    async with TestClient(TestServer(owner.app)) as client:
        response = await client.post("/api/v1/standby/operation", json={"operation":"capabilities"})
        assert response.status == 403
        headers = {"Authorization":"Bearer synthetic-test-token","Content-Type":"application/json"}
        for body in ('{"operation":"capabilities","operation":"arm"}',
                     '{"operation":NaN}', '{"operation":[]}', '{"operation":"capabilities","audio":"canary"}',
                     '[]', '42', '{"binding":{}}'):
            response = await client.post("/api/v1/standby/operation", data=body, headers=headers)
            assert response.status == 400
            assert "canary" not in await response.text()
            assert "no-store" in response.headers["Cache-Control"]
        response = await client.post("/api/v1/standby/operation", json={"operation":"capabilities"},headers=headers)
        value = await response.json()
        assert response.status == 200 and value["enabled"] is False
        assert value["control_available"] is True and value["capture_started"] is False
        response = await client.post("/api/v1/standby/operation",json={"operation":"arm","binding":scope},headers=headers)
        assert response.status == 501
        response = await client.post("/api/v1/standby/operation",data=" " * 65537,headers=headers)
        assert response.status == 413
        assert not (tmp_path / "standby_profiles.json").exists()
        with owner.session_store._connection() as connection:
            assert connection.execute("SELECT count(*) FROM standby_controls").fetchone()[0] == 0
            assert connection.execute("SELECT count(*) FROM messages").fetchone()[0] == 0


async def test_http_profiles_keep_owner_immutable_and_show_exact_preflight_blockers(api):
    owner, scope = api
    async with TestClient(TestServer(owner.app)) as client:
        headers = {"Authorization":"Bearer synthetic-test-token"}
        raw = {"name":"synthetic","raw_goal":"help if asked","confirmed_interpretation":"direct requests only",
               "targets":[{"id":"help","description":"a direct request","action":"wake_agent"}]}
        response = await client.post("/api/v1/standby/operation",json={"operation":"save_profile",
            "binding":scope,"profile_id":"quiet","profile":raw,"expected_revision":None},headers=headers)
        assert response.status == 200
        saved = await response.json()
        response = await client.post("/api/v1/standby/operation",json={"operation":"preflight","binding":scope,
            "profile_id":"quiet"},headers=headers)
        preflight = await response.json()
        assert response.status == 200 and preflight["ready"] is False
        assert set(preflight["data_flow"].values()) == {"disabled"}
        response = await client.post("/api/v1/standby/operation",json={"operation":"save_profile",
            "binding":scope,"owner_id":"other","profile_id":"quiet","profile":raw,
            "expected_revision":saved["revision"]},headers=headers)
        assert response.status == 400
        response = await client.post("/api/v1/standby/operation",json={"operation":"profiles",
            "binding":{**scope,"agent_id":"bob"}},headers=headers)
        assert response.status == 409


async def test_governed_deployment_does_not_advertise_live_control(api):
    owner, scope = api
    owner._is_governed_profile = lambda: True
    async with TestClient(TestServer(owner.app)) as client:
        response = await client.post("/api/v1/standby/operation",json={"operation":"capabilities"},
            headers={"Authorization":"Bearer synthetic-test-token"})
        assert response.status == 501
        assert (await response.json())["error_code"] == "standby_profile_unsupported"

async def test_authenticated_end_survives_receipt_capacity_and_offline_agent(api):
    owner,scope = api
    owner.standby_service.now = lambda: 1000
    first = owner.session_store.standby_control(owner_id="user:7",body={
        "operation":"configure","operation_id":"http-full-configure","issued_at":1000,
        "binding":scope,"profile_id":"quiet","profile_revision":"a"*64},now=1000)
    with owner.session_store._connection() as connection:
        connection.executemany("INSERT INTO standby_control_operations VALUES (?,?,?,?,?,?,?)",[
            (owner.session_store.instance_id,"user:7","alice","fill-http-"+str(index),
             "b"*64,json.dumps(first),1000) for index in range(4095)])
    # Ending an existing control must not depend on a model, a live Agent, or
    # re-reading a now-corrupt optional profile document.
    owner._runtime_map = lambda: {}
    owner.standby_service.profiles.path.write_text("{broken",encoding="utf-8")
    body = {"operation":"end","operation_id":"http-stop-under-load","issued_at":1000,
        "binding":scope,"standby_id":first["standby_id"],
        "expected_revision":first["revision"],"epoch":first["epoch"]}
    async with TestClient(TestServer(owner.app)) as client:
        denied = await client.post("/api/v1/standby/operation",json=body)
        assert denied.status == 403
        assert owner.session_store.standby_snapshot(owner_id="user:7",
            binding=scope,standby_id=first["standby_id"]) == first
        headers = {"Authorization":"Bearer synthetic-test-token"}
        response = await client.post("/api/v1/standby/operation",json=body,headers=headers)
        assert response.status == 200 and "no-store" in response.headers["Cache-Control"]
        result = await response.json()
        assert result["session"]["lifecycle"] == "STOPPED"
        assert result["session"]["epoch"] == first["epoch"]+1
        again = await client.post("/api/v1/standby/operation",json=body,headers=headers)
        assert await again.json() == result
        query = await client.post("/api/v1/standby/operation",json={
            "operation":"operation_status","binding":scope,
            "operation_id":body["operation_id"]},headers=headers)
        assert (await query.json())["session"] == result["session"]
        denied_scope = await client.post("/api/v1/standby/operation",json={
            **body,"operation_id":"wrong-scope",
            "binding":{**scope,"context_generation":scope["context_generation"]+1}},headers=headers)
        assert denied_scope.status == 404
        with owner.session_store._connection() as connection:
            assert connection.execute("SELECT count(*) FROM standby_control_operations").fetchone()[0] == 4096
            assert connection.execute("SELECT count(*) FROM standby_safety_operations").fetchone()[0] == 1
            assert connection.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
            assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
