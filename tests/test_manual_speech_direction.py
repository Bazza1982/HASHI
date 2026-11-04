from pathlib import Path
from unittest.mock import AsyncMock
import pytest
from tests.test_speech_performance import manager
import orchestrator.voice_manager as voice_module
from orchestrator.voice_synthesizer import VoiceAsset
from orchestrator.manual_speech_direction import validate, input_button
from orchestrator.command_interactions import Binding, Capture, MenuStore, perform_action, InteractionError

@pytest.mark.asyncio
async def test_manual_persists_skips_planner_reuses_all_parts_and_off_restores_auto(tmp_path, monkeypatch):
    value=manager(tmp_path)
    prepare=AsyncMock(return_value={"version":1,"model":"advanced-model","applied":True,"instructions":"automatic"})
    synthesis=AsyncMock(return_value=VoiceAsset("advanced","body","body",None,Path("test.ogg")))
    provider=type("Provider",(),{"prepare_performance":prepare,"synthesize":synthesis})()
    monkeypatch.setattr(voice_module,"build_provider",lambda *a,**k:provider)
    with pytest.raises(ValueError):value.set_manual_direction(True," ")
    value.set_performance(True)
    value.set_manual_direction(True,"温柔自然，略带笑意。")
    context=await value.prepare_performance("完整正文")
    assert context["source"]=="manual" and prepare.await_count==0
    for body in ("第一段","第二段"):
        asset=await value.synthesize_reply("agent","id",body,force=True,max_retries=0,performance_context=context)
        opts=synthesis.await_args.kwargs["provider_options"]
        assert opts["performance_plan"]["instructions"]=="温柔自然，略带笑意。" and opts["performance"] is True
        assert "instructions" not in opts and synthesis.await_args.kwargs["voice_name"]=="chosen"
        assert asset.performance["applied"] and asset.performance["source"]=="manual"
    assert prepare.await_count==0
    value.set_manual_direction(False)
    assert value.get_manual_direction()["instructions"]=="温柔自然，略带笑意。"
    auto=await value.prepare_performance("new")
    assert auto["plan"]["instructions"]=="automatic" and prepare.await_count==1
    assert validate({"enabled":False,"instructions":"x"*500})["instructions"]=="x"*500
    with pytest.raises(ValueError):validate({"enabled":True,"instructions":"x"*501})

@pytest.mark.asyncio
async def test_issued_input_is_bounded_and_only_attached_to_its_action():
    store=MenuStore(); binding=Binding("instance","agent","7","session",1,"clientabcdefghijkl","connectionabcdefghijkl")
    captured=[]
    async def callback(query):
        captured.append(query.input_text)
    resolve=lambda data: (("registry","voice:","generation"),callback)
    capture=Capture(store,binding,"voice",resolve)
    await capture.capture_reply("manual",reply_markup={"inline_keyboard":[[input_button("voice:manual:save",{"instructions":"saved"})]]})
    menu=next(iter(store.menus.values())) if hasattr(store,"menus") else None
    if menu is None:
        menu=capture.menu
    button=menu.rows[0][0]
    assert button["text_input"]["value"]=="saved"
    payload={"menu_id":menu.id,"revision":menu.revision,"button_id":button["button_id"],"input_text":" "}
    async def invoke(resolved,query):await resolved[1](query)
    with pytest.raises(InteractionError):
        await perform_action(store,binding,payload,capture,actor_id=7,authorize=lambda _:True,invoke=invoke)
    payload["input_text"]="gentle"
    await perform_action(store,binding,payload,capture,actor_id=7,authorize=lambda _:True,invoke=invoke)
    assert captured==["gentle"]


@pytest.mark.asyncio
async def test_call_frozen_direction_is_independent_from_voice_and_reaches_all_segments(tmp_path, monkeypatch):
    from tests.frontend_call.test_call_shared_tts import _shared_config, _voice_catalog
    from orchestrator.frontend_call.service import Call
    from orchestrator.frontend_call.settings import CallSettings
    from orchestrator.frontend_call.tts import CatalogCallTTS
    from orchestrator.voice_manager import VoiceManager
    catalog=_voice_catalog()
    catalog["services"][0]["automatic_performance"]=True
    catalog["services"][0]["language_voices"]={"zh":"premium_one","ja":"premium_ja"}
    catalog["voices"].append({
        "id":"premium_ja","service":"compute-premium","voice_name":"premium-ja",
        "label":"Japanese","language":"Japanese","locale":"ja-JP","gender":"female",
        "provider_options":{"temperature": .4}})
    config=_shared_config(tmp_path,catalog_payload=catalog)
    settings=CallSettings.__new__(CallSettings)
    settings.config=config;settings.owner="owner";settings.agent="agent"
    settings.command(["manual","calm call direction"])
    ctx=config.context("owner","agent")
    assert ctx["profile"]["manual_direction"]=={"enabled":True,"instructions":"calm call direction"}
    profile,targets,frozen=config.freeze_runtime("owner","agent",ctx["revision"])
    manager=VoiceManager(tmp_path/"workspace",tmp_path/"media",catalog_path=tmp_path/"voice_catalog.json",project_root=tmp_path)
    manager.set_manual_direction(True,"different voice direction")
    built=[]
    class Provider:
        async def prepare_performance(self,*a):
            pytest.fail("Call must not invoke a planner in manual mode")
        async def synthesize(self,**kw):
            built.append(kw)
            output=Path(kw["output_dir"])/"result.ogg";output.write_bytes(b"OggS")
            return VoiceAsset("compute-premium",kw["text"],kw["text"],None,output)
    monkeypatch.setattr(voice_module,"build_provider",lambda *a,**kw:Provider())
    call=Call(owner="owner",binding={"call_id":"call-1","agent_id":"agent"},profile=profile,targets=targets,
        fingerprint="fingerprint",expires=100,started=0,locale="zh-CN",tts_catalog=frozen)
    tts=CatalogCallTTS(manager,temp_root=tmp_path/"call-media")
    texts=("这是第一段中文。","これは日本語の文章です。","这是最后一段中文。")
    for i,text in enumerate(texts):
        await tts.synthesize(call,text,privacy_level=0,request_id=str(i))
    assert len(built)==3
    assert [k["voice_name"] for k in built]==["premium-1","premium-ja","premium-1"]
    assert [k["text"] for k in built]==list(texts)
    assert call.profile["tts"]["voice_id"]=="premium_one"
    assert all(k["provider_options"]["performance_plan"]["instructions"]=="calm call direction" for k in built)
    assert manager.get_manual_direction()["instructions"]=="different voice direction"
    settings.command(["manual","off"])
    assert config.context("owner","agent")["profile"]["manual_direction"]["enabled"] is False
    assert profile["manual_direction"]["enabled"] is True
