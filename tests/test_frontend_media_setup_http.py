import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient,TestServer
from orchestrator.config_json import read_config_json,write_config_json
from orchestrator.frontend_media_setup import MediaServiceSetup,VoiceSetupError,register_media_setup_api,probe_api
from orchestrator.frontend_live_voice.protocol import LiveVoiceError
from orchestrator.frontend_live_voice.simulmt_provider import SimulMTProvider
from orchestrator.compute_services import ComputeServiceDirectory,ComputeServicesConfig
from tests.test_frontend_media_setup import service,SCOPE

def test_translate_connection_uses_canonical_directory_and_live_private_credentials(tmp_path):
    setup,path,phone=service(tmp_path)
    directory=ComputeServiceDirectory(ComputeServicesConfig(tmp_path/"compute_services.json"))
    adapter=SimulMTProvider(directory=directory,node_id="",service_id="")
    setup.api.live_voice_manager.providers["hy-simulmt"]=adapter
    setup=MediaServiceSetup(setup.api)
    phone.update(provider="hy-simulmt",model="hy-simulmt-en-zh",adapter=adapter)
    phone["public"].update(provider_label="Interpretation")
    before=setup.status("translate","owner",SCOPE)
    binding={"node_id":"translate-local","service_id":"speech-translation","base_url":"http://127.0.0.1:9422",
        "location":"local","authenticated":True}
    saved=setup.save_binding("translate","owner",SCOPE,expected_revision=before["revision"],binding=binding)
    assert adapter.connection_selection()==("translate-local","speech-translation")
    assert saved["services"][0]["credential_editable"] is True
    assert saved["configured"] is False
    ready=setup.save_key("translate","owner",SCOPE,expected_revision=saved["revision"],
        service_id="hy-simulmt",api_key="translate-key")
    _,nodes=directory.config.read()
    assert directory.client.request_headers(nodes[0])["Authorization"]=="Bearer translate-key"
    assert ready["configured"] is True
    assert "translate-key" not in json.dumps(ready)
    doc=read_config_json(directory.config.path);doc["unrelated"]={"keep":True};write_config_json(directory.config.path,doc)
    with pytest.raises(VoiceSetupError,match="changed"):
        setup.save_binding("translate","owner",SCOPE,expected_revision=ready["revision"],binding=binding)
    assert read_config_json(directory.config.path)["unrelated"]=={"keep":True}

@pytest.mark.asyncio
async def test_setup_http_checks_existing_authority_session_and_admin_before_writes(tmp_path):
    setup,path,_=service(tmp_path);api=setup.api
    api.app=web.Application()
    api._authorize_live_voice=AsyncMock(return_value={"owner_id":"owner","admin":False})
    api._require_live_voice_session_scope=lambda **kwargs:None
    register_media_setup_api(api)
    async with TestClient(TestServer(api.app)) as client:
        base={**SCOPE,"entrypoint":"phone","operation":"status"}
        response=await client.post("/api/v1/media-services/operation",json=base)
        assert response.status==200
        status=await response.json()
        assert "old-key" not in json.dumps(status)
        response=await client.post("/api/v1/media-services/operation",json={**base,"operation":"save_key",
            "expected_revision":status["revision"],"service_id":"openai","api_key":"denied"})
        assert response.status==403
        assert read_config_json(path)["openai_api_key"]=="old-key"
        response=await client.post("/api/v1/media-services/operation",json={**base,"owner_id":"someone"})
        assert response.status==400
        response=await client.post("/api/v1/media-services/operation",json={**base,"context_generation":True})
        assert response.status==400
        def changed(**kwargs):raise LiveVoiceError("live_scope_changed",409)
        api._require_live_voice_session_scope=changed
        response=await client.post("/api/v1/media-services/operation",json=base)
        assert response.status==409
        api._authorize_live_voice.side_effect=LiveVoiceError("live_not_authenticated",401)
        response=await client.post("/api/v1/media-services/operation",json=base)
        assert response.status==401
        assert read_config_json(path)["openai_api_key"]=="old-key"

@pytest.mark.asyncio
async def test_connection_probe_is_authenticated_bounded_and_does_not_follow_redirects():
    calls=[];app=web.Application()
    async def model(request):
        calls.append(request.path)
        assert request.headers["Authorization"]=="Bearer fixture-key"
        return web.json_response({"id":"configured-model"})
    async def redirect(request):
        calls.append(request.path)
        raise web.HTTPFound("/not-followed")
    async def denied(request):
        calls.append(request.path)
        return web.json_response({"private":"provider-internal"},status=401)
    app.router.add_get("/model",model);app.router.add_get("/redirect",redirect);app.router.add_get("/denied",denied)
    async with TestServer(app) as server:
        await probe_api(str(server.make_url("/model")),key="fixture-key")
        with pytest.raises(VoiceSetupError,match="not_supported"):
            await probe_api(str(server.make_url("/redirect")),key="fixture-key")
        with pytest.raises(VoiceSetupError,match="auth_rejected") as error:
            await probe_api(str(server.make_url("/denied")),key="fixture-key")
        assert "provider-internal" not in str(error.value)
    assert calls==["/model","/redirect","/denied"]

@pytest.mark.asyncio
async def test_missing_key_does_not_report_public_api_reachability_as_authentication(tmp_path,monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY",raising=False)
    setup,path,_=service(tmp_path)
    doc=read_config_json(path);doc.pop("openai_api_key");write_config_json(path,doc)
    status=setup.status("phone","owner",SCOPE)
    result=await setup.test("phone","owner",SCOPE,expected_revision=status["revision"])
    assert result["results"][0]["error_code"]=="setup_credential_missing"
    assert setup.http_probe.await_count==0

@pytest.mark.asyncio
async def test_default_openrouter_call_checks_key_api_without_inference(tmp_path):
    from orchestrator.frontend_call.config import OPENROUTER_API_BASE
    setup,path,_=service(tmp_path)
    doc=read_config_json(path);doc["openrouter-api_key"]="router-fixture";write_config_json(path,doc)
    target={"id":"stt-selected","kind":"stt","model":"provider/selected-model","base_url":OPENROUTER_API_BASE,
        "credential_ref":"secrets://openrouter-api_key"}
    setup.api.call_service.config=SimpleNamespace(context=lambda *args:{"call_ready":True,"revision":"r1"},
        freeze_runtime=lambda *args:({},{"stt":target},None))
    status=setup.status("call","owner",SCOPE)
    result=await setup.test("call","owner",SCOPE,expected_revision=status["revision"])
    assert result["results"][0]["ok"] is True
    assert setup.http_probe.await_args.args[0]==OPENROUTER_API_BASE.rstrip("/")+"/key"
    assert setup.http_probe.await_args.kwargs["key"]=="router-fixture"

@pytest.mark.asyncio
async def test_unconfigured_call_test_explains_missing_service_instead_of_empty_success(tmp_path):
    setup,_,_=service(tmp_path)
    setup.api.call_service.config=SimpleNamespace(context=lambda *args:{"call_ready":False,"revision":"r1"})
    status=setup.status("call","owner",SCOPE)
    with pytest.raises(VoiceSetupError,match="setup_service_not_configured"):
        await setup.test("call","owner",SCOPE,expected_revision=status["revision"])
    assert setup.http_probe.await_count==0

# HASHI-Fingerprint: hfp_ec1c8c35051e4723805ce5c8363f13fd
