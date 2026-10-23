"""Fixed character voices must be identical across voice, audio Call and VideoTTS."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.voice_catalog import VoiceCatalog, VoiceCatalogError
from orchestrator.voice_manager import VoiceManager
from orchestrator.voice_synthesizer import VoiceAsset
from orchestrator.frontend_call.tts import CatalogCallTTS
from orchestrator.frontend_call.output import ExternalCallOutput
from orchestrator.frontend_call.video_tts import VideoTTSSelection
import orchestrator.voice_manager as voice_module


def document():
    return {
        "version": 1,
        "services": [{
            "id": "character-kokoro", "adapter": "openai_compatible",
            "label": "Character Kokoro", "location": "lan",
            "base_url": "http://192.168.10.22:19446/v1", "model": "kokoro-82m",
            "audio_format": "wav", "fallbacks": [],
            "language_voices": {"zh": "character_zh", "en": "character_en", "ja": "character_ja"},
        }],
        "voices": [{
            "id": alias, "service": "character-kokoro", "voice_name": voice,
            "label": alias, "language": language, "locale": locale, "gender": "female",
        } for alias, voice, language, locale in [
            ("other_en", "af_alloy", "English", "en-US"),
            ("character_zh", "zf_027", "Chinese", "zh-CN"),
            ("character_en", "af_heart", "English", "en-US"),
            ("character_ja", "jf_alpha", "Japanese", "ja-JP"),
        ]],
    }


def catalogue(tmp_path, payload=None):
    path = tmp_path / "voice_catalog.json"
    path.write_text(json.dumps(payload or document()), encoding="utf-8")
    return VoiceCatalog.load(instance_path=path, project_root=tmp_path)


@pytest.mark.asyncio
async def test_voice_and_call_keep_exact_language_voices_between_segments(tmp_path, monkeypatch):
    cat = catalogue(tmp_path)
    manager = VoiceManager(tmp_path / "workspace", tmp_path / "media",
                           catalog_path=tmp_path / "voice_catalog.json", project_root=tmp_path)
    requests = []

    class Provider:
        async def synthesize(self, **kw):
            requests.append(kw["voice_name"])
            out = Path(kw["output_dir"]) / "test.ogg"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"OggS-test")
            return VoiceAsset(provider="character-kokoro", text=kw["text"],
                              spoken_text=kw["text"], wav_path=None, ogg_path=out)

    monkeypatch.setattr(voice_module, "build_provider", lambda *a, **k: Provider())
    manager.apply_voice_preset("character_zh")
    manager.set_enabled(True)
    samples = [("您好，我们开始吧。", "zf_027"),
               ("Good morning, I am ready to help you.", "af_heart"),
               ("おはようございます。よろしくお願いします。", "jf_alpha"),
               ("我们继续工作吧。", "zf_027")]
    for i, (text, voice) in enumerate(samples):
        result = await manager.synthesize_reply(
            agent_name="character", request_id=f"voice-{i}", text=text, source_language="zh-CN")
        assert result is not None and requests[-1] == voice
    call = SimpleNamespace(profile={"tts": {"target_id": "character-kokoro", "voice_id": "character_zh"}},
                           binding={"agent_id": "character"}, locale="zh-CN", tts_catalog=cat,
                           tts_effective_service=None, tts_effective_voice=None, tts_effective_options={},
                           tts_fallback_reason_code="")
    call_tts = CatalogCallTTS(manager, temp_root=tmp_path / "call-media")
    for i, (text, voice) in enumerate(samples):
        await call_tts.synthesize(call, text, privacy_level=0, request_id=f"call-{i}")
        assert requests[-1] == voice
    assert requests == [v for _, v in samples] * 2


@pytest.mark.asyncio
async def test_missing_english_voice_does_not_disable_other_languages(tmp_path, monkeypatch):
    import io
    import wave
    from aiohttp import web
    from orchestrator.voice_synthesizer import VoiceSynthesisError
    import orchestrator.tts_providers.openai_compatible as adapter

    requested = []
    async def handle(request):
        body = await request.json()
        requested.append(body["voice"])
        if body["voice"] == "af_heart":
            return web.json_response({"error": {"code": "voice_not_found"}}, status=400)
        data = io.BytesIO()
        with wave.open(data, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(24000)
            wav.writeframes(bytes(4800))
        return web.Response(body=data.getvalue(), content_type="audio/wav")

    async def convert(_command, _source, destination):
        destination.write_bytes(b"OggS-test-conversion")

    monkeypatch.setattr(adapter, "convert_wav_to_ogg", convert)
    app = web.Application()
    app.router.add_post("/v1/audio/speech", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        port = site._server.sockets[0].getsockname()[1]
        doc = document()
        doc["services"][0].update(location="local", base_url=f"http://127.0.0.1:{port}/v1")
        catalogue(tmp_path, doc)
        manager = VoiceManager(tmp_path / "workspace", tmp_path / "media",
                               catalog_path=tmp_path / "voice_catalog.json", project_root=tmp_path)
        manager.apply_voice_preset("character_zh")
        with pytest.raises(VoiceSynthesisError):
            await manager.synthesize_reply("character", "english", "Good morning, I am here.",
                                           force=True, max_retries=0, source_language="zh-CN")
        asset = await manager.synthesize_reply("character", "chinese", "您好，我们开始吧。",
                                               force=True, max_retries=0, source_language="zh-CN")
        assert asset.voice_name == "zf_027"
        assert requested == ["af_heart", "zf_027"]
    finally:
        await runner.cleanup()


@pytest.mark.parametrize("mutation", ["missing", "different_service", "disabled", "language_mismatch", "bad_language", "not_mapping"])
def test_language_map_rejects_unusable_or_cross_service_aliases(tmp_path, mutation):
    doc = document()
    row = doc["voices"][2]
    if mutation == "missing":
        doc["services"][0]["language_voices"]["en"] = "absent"
    elif mutation == "different_service":
        row["service"] = "edge"
    elif mutation == "disabled":
        row["enabled"] = False
    elif mutation == "language_mismatch":
        row["language"] = "Chinese"
    elif mutation == "bad_language":
        doc["services"][0]["language_voices"]["xx"] = "character_en"
    elif mutation == "not_mapping":
        doc["services"][0]["language_voices"] = []
    with pytest.raises(VoiceCatalogError):
        catalogue(tmp_path, doc)


@pytest.mark.asyncio
async def test_video_output_uses_frozen_call_voice_policy_without_ap_choosing_a_voice(tmp_path):
    cat = catalogue(tmp_path)
    call = SimpleNamespace(profile={"tts": {"target_id": "character-kokoro", "voice_id": "character_zh"}},
                           binding={"agent_id": "character"}, locale="zh-CN", tts_catalog=cat,
                           turn={}, phase="active", owner="owner")
    selection = VideoTTSSelection.parse({
        "enabled": True, "reference": {"node_id": "studio", "service_id": "video", "model_id": "talker"},
        "voice": "legacy", "voice_source": "call_tts"})
    requested = []

    async def synthesize(selected, reference, text, **kwargs):
        requested.append(selected.voice)
        assert reference == b"AP reference"
        return {"media": b"video-and-audio"}

    async def prepare(*a, **k): return {}
    connector = SimpleNamespace(epoch=0, reference=b"AP reference", require_active=lambda e: None, prepare=prepare)
    ports = SimpleNamespace(validate=lambda *a: None, privacy_level=lambda *a: 0)
    output = ExternalCallOutput(call, ports, connector, SimpleNamespace(synthesize=synthesize), selection)
    for i, text in enumerate(["您好，我们开始吧。", "Good morning, I am ready to help you.", "おはようございます。よろしくお願いします。"]):
        await output.synthesize(text, request_id=f"video-{i}")
    assert requested == ["zf_027", "af_heart", "jf_alpha"]
