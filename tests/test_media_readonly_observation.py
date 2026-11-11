"""Read-only media transport keeps auth/scope and never substitutes for active-call protection."""
from __future__ import annotations
from contextlib import contextmanager
import sqlite3
from types import SimpleNamespace
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from orchestrator.frontend_call.routes import register_call_api
from orchestrator.frontend_call.service import CallService
from orchestrator.frontend_live_voice.manager import LiveVoiceManager
from orchestrator.runtime_app_host import RuntimeAppHost

@pytest.mark.asyncio
async def test_call_get_observations_keep_auth_and_cannot_dispatch_writes(tmp_path):
    api=SimpleNamespace(app=web.Application(),config_path=tmp_path/"agents.json",
        admin_token="fixture",live_voice_manager=SimpleNamespace(),
        _is_governed_profile=lambda:False,
        _check_admin_auth=lambda r:r.headers.get("X-Workbench-Token")=="fixture",
        _v1_owner_id=lambda r:"user:7")
    service=register_call_api(api)
    observed=[]
    async def invoke(owner,body):
        observed.append((owner,body))
        return {"ok":True,"generation":"fixture"}
    service.invoke=invoke
    async with TestClient(TestServer(api.app)) as client:
        query="client_id=client&agent_id=ying&session_id=ses_test&context_generation=1"
        assert (await client.get("/api/v1/call/route?"+query)).status==403
        for operation in ("route","context"):
            response=await client.get("/api/v1/call/"+operation+"?"+query,headers={"X-Workbench-Token":"fixture"})
            assert response.status==200,await response.text()
            assert response.headers["Cache-Control"].startswith("no-store")
            assert observed[-1]==("user:7",dict(client_id="client",agent_id="ying",session_id="ses_test",
                context_generation=1,operation=operation))
        before=len(observed)
        for suffix in ("&operation=start","&context_generation=2","&owner_id=user:8"):
            assert (await client.get("/api/v1/call/route?"+query+suffix,headers={"X-Workbench-Token":"fixture"})).status==400
        assert (await client.get("/api/v1/call/start?"+query,headers={"X-Workbench-Token":"fixture"})).status==404
        assert len(observed)==before

@pytest.mark.asyncio
async def test_live_get_context_uses_real_session_authority_and_rejects_mutations():
    from tests.test_live_voice_integration import live_voice_http_client
    async with live_voice_http_client() as (client,session,agent):
        query=f"session_id={session}&agent_id={agent}&context_generation=1"
        response=await client.get("/api/v1/live-voice/context?"+query)
        assert response.status==200,await response.text()
        assert (await response.json())["binding"]["session_id"]==session
        for suffix in ("&operation=start","&owner_id=user:8","&context_generation=2"):
            assert (await client.get("/api/v1/live-voice/context?"+query+suffix)).status==400
        response=await client.get("/api/v1/live-voice/context?"+query,headers={"Authorization":"Bearer bad-token"})
        assert response.status==401

def test_active_media_blocks_quiescence_without_http_request_and_errors_fail_closed():
    app=SimpleNamespace(workbench_api=SimpleNamespace(
        call_service=SimpleNamespace(active_call_count=lambda:1),
        live_voice_manager=SimpleNamespace(active_call_count=lambda:0)))
    with pytest.raises(RuntimeError,match="media_calls=1"):
        RuntimeAppHost._require_shared_drain_idle(app)
    app.workbench_api.call_service.active_call_count=lambda:0
    RuntimeAppHost._require_shared_drain_idle(app)
    def broken():raise OSError("activity unavailable")
    app.workbench_api.live_voice_manager.active_call_count=broken
    with pytest.raises(OSError):
        RuntimeAppHost._require_shared_drain_idle(app)

def test_media_activity_counters_include_reserved_and_recovering_calls():
    service=CallService.__new__(CallService)
    service.calls={"a":SimpleNamespace(phase="active"),"b":SimpleNamespace(phase="ended")}
    assert service.active_call_count()==1
    db=sqlite3.connect(":memory:")
    db.executescript("CREATE TABLE live_calls(call_id TEXT, phase TEXT);"
        "CREATE TABLE live_call_attempts(call_id TEXT, state TEXT);"
        "INSERT INTO live_calls VALUES('a','recovering'),('b','ended');"
        "INSERT INTO live_call_attempts VALUES('a','provider_created'),(NULL,'reserved'),('b','provider_created');")
    @contextmanager
    def connection():yield db
    import threading
    manager=LiveVoiceManager.__new__(LiveVoiceManager)
    manager.session_store=SimpleNamespace(_lock=threading.RLock(),_connection=connection)
    assert manager.active_call_count()==2  # Recovering Call plus unassociated reservation.
    db.close()
# HASHI-Fingerprint: hfp_46b83d11e4314f4b8e88a66307d2ae96
