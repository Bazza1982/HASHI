"""Reachable media setup: real persistence/failure and API-probe boundaries."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from orchestrator.config_json import read_config_json, write_config_json, new_config_json
from orchestrator.frontend_media_setup import MediaServiceSetup, VoiceSetupError, CurrentVoiceSecrets

SCOPE={"agent_id":"test-agent","session_id":"session-1","context_generation":1}
def service(tmp_path):
    path=tmp_path/"secrets.json"
    write_config_json(path,new_config_json(path,{"unrelated":"preserved","openai_api_key":"old-key"}))
    adapter=SimpleNamespace(provider_id="openai",credential=lambda s:s.get("openai_api_key",""))
    phone={"provider":"openai","model":"gpt-live-1","public":{"revision":"a"*64,"provider_label":"OpenAI"},"adapter":adapter}
    manager=SimpleNamespace(_phone_session=lambda *a,**k:phone,has_foreground_call=lambda owner:False,
        providers={"openai":adapter})
    api=SimpleNamespace(config_path=tmp_path/"agents.json",global_config=SimpleNamespace(secrets_path=path),
        secrets={},live_voice_manager=manager,call_service=SimpleNamespace(busy=lambda owner:False))
    setup=MediaServiceSetup(api,http_probe=AsyncMock(return_value=None))
    return setup,path,phone

def test_key_save_is_private_revision_checked_and_immediately_readable(tmp_path):
    setup,path,_=service(tmp_path)
    before=setup.status("phone","owner",SCOPE)
    assert "old-key" not in json.dumps(before)
    after=setup.save_key("phone","owner",SCOPE,expected_revision=before["revision"],service_id="openai",api_key="new-key")
    assert read_config_json(path)["unrelated"]=="preserved"
    assert CurrentVoiceSecrets(path).get("openai_api_key")=="new-key"
    assert "new-key" not in json.dumps(after)
    assert before["revision"]!=after["revision"]
    with pytest.raises(VoiceSetupError,match="changed"):
        setup.save_key("phone","owner",SCOPE,expected_revision=before["revision"],service_id="openai",api_key="stale-key")
    assert read_config_json(path)["openai_api_key"]=="new-key"

def test_revision_binds_owner_agent_session_and_selected_configuration(tmp_path):
    setup,path,phone=service(tmp_path)
    initial=setup.status("phone","owner",SCOPE)["revision"]
    for owner,scope in [("someone-else",SCOPE),("owner",{**SCOPE,"agent_id":"another"}),("owner",{**SCOPE,"context_generation":2})]:
        with pytest.raises(VoiceSetupError,match="changed"):
            setup.save_key("phone",owner,scope,expected_revision=initial,service_id="openai",api_key="wrong-scope")
    phone["public"]["revision"]="b"*64
    with pytest.raises(VoiceSetupError,match="changed"):
        setup.save_key("phone","owner",SCOPE,expected_revision=initial,service_id="openai",api_key="wrong-selection")
    assert read_config_json(path)["openai_api_key"]=="old-key"

@pytest.mark.asyncio
async def test_probe_uses_selected_api_and_never_starts_a_call(tmp_path):
    setup,_,_=service(tmp_path)
    status=setup.status("phone","owner",SCOPE)
    result=await setup.test("phone","owner",SCOPE,expected_revision=status["revision"])
    assert result["results"][0]["ok"] is True
    assert result["results"][0]["scope"]=="api_access"
    args=setup.http_probe.await_args
    assert args.args[0].endswith("/models/gpt-live-1")
    assert args.kwargs["key"]=="old-key"
    assert "old-key" not in json.dumps(result)

@pytest.mark.asyncio
async def test_api_rejection_keeps_a_safe_cause_and_does_not_retry(tmp_path):
    setup,_,_=service(tmp_path)
    setup.http_probe.side_effect=VoiceSetupError("setup_auth_rejected",401)
    status=setup.status("phone","owner",SCOPE)
    result=await setup.test("phone","owner",SCOPE,expected_revision=status["revision"])
    assert result["results"][0]["ok"] is False
    assert result["results"][0]["error_code"]=="setup_auth_rejected"
    assert setup.http_probe.await_count==1

def test_corrupt_or_concurrently_changed_secret_document_is_not_replaced(tmp_path):
    setup,path,_=service(tmp_path)
    initial=setup.status("phone","owner",SCOPE)["revision"]
    path.write_text('{"broken":',encoding="utf-8")
    with pytest.raises((VoiceSetupError,ValueError)):
        setup.save_key("phone","owner",SCOPE,expected_revision=initial,service_id="openai",api_key="replacement")
    assert path.read_text(encoding="utf-8")=='{"broken":'

def test_arbitrary_secret_names_and_writes_during_a_call_are_rejected(tmp_path):
    setup,path,_=service(tmp_path)
    status=setup.status("phone","owner",SCOPE)
    with pytest.raises(VoiceSetupError):
        setup.save_key("phone","owner",SCOPE,expected_revision=status["revision"],service_id="workbench_admin_token",api_key="forbidden")
    setup.api.live_voice_manager.has_foreground_call=lambda owner:True
    with pytest.raises(VoiceSetupError,match="busy"):
        setup.save_key("phone","owner",SCOPE,expected_revision=status["revision"],service_id="openai",api_key="during-call")
    assert read_config_json(path)["openai_api_key"]=="old-key"
@pytest.mark.asyncio
@pytest.mark.parametrize("recovery",["save","probe"])
async def test_explicit_recovery_discards_only_selected_cached_failure_without_claiming_voice_verified(tmp_path,recovery):
    from orchestrator.frontend_live_voice.manager import LiveVoiceManager
    setup,_,phone=service(tmp_path)
    manager=object.__new__(LiveVoiceManager)
    manager._provider_availability={}
    manager._feature_enabled=True
    manager._availability_override=None
    manager.secrets=setup.current
    manager.providers={"openai":phone["adapter"]}
    manager._phone_session=lambda *args,**kwargs:phone
    manager.has_foreground_call=lambda owner:False
    phone["adapter"].is_available=lambda current:bool(current.get("openai_api_key"))
    setup.api.live_voice_manager=manager
    manager._remember_provider_availability(phone,"old-key","live_provider_key_invalid")
    manager._remember_provider_availability(phone,"unrelated-key","live_provider_key_invalid")
    assert manager._phone_availability(phone,None)["available"] is False
    status=setup.status("phone","owner",SCOPE)
    if recovery=="save":
        setup.save_key("phone","owner",SCOPE,expected_revision=status["revision"],service_id="openai",api_key="old-key")
    else:
        setup.http_probe.side_effect=VoiceSetupError("setup_auth_rejected",401)
        await setup.test("phone","owner",SCOPE,expected_revision=status["revision"])
        assert manager._phone_availability(phone,None)["available"] is False
        setup.http_probe.side_effect=None
        await setup.test("phone","owner",SCOPE,expected_revision=status["revision"])
    recovered=manager._phone_availability(phone,None)
    assert recovered["available"] is True
    assert recovered["availability"]=={"state":"unverified","reason":None}
    assert manager._provider_availability[manager._provider_availability_key(phone,"unrelated-key")]=="live_provider_key_invalid"
    manager._remember_provider_availability(phone,"old-key")
    status=setup.status("phone","owner",SCOPE)
    await setup.test("phone","owner",SCOPE,expected_revision=status["revision"])
    assert manager._phone_availability(phone,None)["availability"]["state"]=="available"

# HASHI-Fingerprint: hfp_a828535c67754c359adeee4d33352cab
