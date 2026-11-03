import asyncio
from pathlib import Path
import pytest
from tests.test_speech_tasks import settled
from tests.test_speech_performance import manager
from orchestrator.speech_tasks import SpeechTasks, SpeechTaskError
from orchestrator.voice_synthesizer import VoiceAsset
import orchestrator.voice_manager as module

@pytest.mark.asyncio
async def test_new_attempt_requires_confirmed_failure_and_same_payload(tmp_path):
    owner = SpeechTasks(tmp_path)
    calls = []
    async def failed(_):
        calls.append("failed")
        raise SpeechTaskError("native_failed", accepted=True)
    async def success(_):
        calls.append("success")
        return {"audio": "saved"}
    try:
        owner.start("first", "body", "generate", {}, failed)
        assert (await settled(owner, "first"))["state"] == "failed"
        owner.start("first", "body", "generate", {}, success)
        assert calls == ["failed"]
        owner.start("second", "body", "generate", {}, success, retry_of="first")
        assert (await settled(owner, "second"))["result"] == {"audio": "saved"}
        owner.start("second", "body", "generate", {}, success, retry_of="first")
        with pytest.raises(SpeechTaskError):
            owner.start("wrong", "different", "generate", {}, success, retry_of="first")
        async def uncertain(_): raise ConnectionError("delivery lost")
        owner.start("unknown", "body", "generate", {}, uncertain)
        assert (await settled(owner, "unknown"))["state"] == "uncertain"
        with pytest.raises(SpeechTaskError):
            owner.start("unsafe", "body", "generate", {}, success, retry_of="unknown")
        owner.start("active", "body", "generate", {}, success)
        with pytest.raises(SpeechTaskError):
            owner.start("duplicate", "body", "generate", {}, success, retry_of="active")
        assert calls == ["failed", "success"]
    finally:
        await owner.close()

@pytest.mark.asyncio
async def test_logical_reading_freezes_voice_rate_options_and_manual_direction(tmp_path, monkeypatch):
    value = manager(tmp_path)
    value.set_manual_direction(True, "温柔，清晰")
    built = []
    class Provider:
        async def synthesize(self, **kw):
            built.append(kw)
            return VoiceAsset("advanced", kw["text"], kw["text"], None, Path("fixture.ogg"))
        async def prepare_performance(self, *a):
            pytest.fail("manual mode must skip planner")
    monkeypatch.setattr(module, "build_provider", lambda *a, **kw: Provider())
    context = await value.prepare_reading("完整中文，然后日本語です。", source_language="zh-CN")
    await value.synthesize_reply("agent", "one", "第一段", force=True, max_retries=0, performance_context=context)
    state = value._load()
    state.update(voice_name="changed", rate=70, provider_options={"temperature": .9})
    state["manual_direction"] = {"enabled": True, "instructions": "changed direction"}
    value._update(lambda current: current.update(state))
    await value.synthesize_reply("agent", "two", "日本語です。", force=True, max_retries=0, performance_context=context)
    for key in ("voice_name", "rate", "provider_options"):
        assert built[0][key] == built[1][key]
    assert [x["text"] for x in built] == ["第一段", "日本語です。"]
    assert built[0]["provider_options"]["performance_plan"]["instructions"] == "温柔，清晰"

