"""Shared speech: complete input, streamed assets, cleanup and accurate errors."""
import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import pytest

from orchestrator.tts_providers.openai_compatible import OpenAICompatibleTTSProvider, OpenAICompatibleTTSError
from orchestrator.voice_catalog import VoiceCatalog
from orchestrator.voice_manager import VoiceManager, _provider_failure
from orchestrator.voice_synthesizer import VoiceAsset, VoiceProviderFailure


def catalogue(tmp_path):
    path = tmp_path / "voice_catalog.json"
    path.write_text(json.dumps({"version": 1, "services": [{
        "id": "complete-speech", "adapter": "openai_compatible",
        "label": "Complete speech", "location": "lan",
        "base_url": "http://192.168.10.22:19446/v1", "model": "speech",
        "audio_format": "mp3", "fallbacks": [],
        "timeout_seconds": None, "max_response_bytes": None, "max_input_chars": None,
    }], "voices": [{"id": "complete_voice", "service": "complete-speech",
        "voice_name": "zf_xiaoxiao", "label": "Voice", "language": "Chinese",
        "locale": "zh-CN", "gender": "female"}]}), encoding="utf-8")
    return path


def test_catalogue_explicit_streaming_bounds_preserve_null(tmp_path):
    service = VoiceCatalog.load(instance_path=catalogue(tmp_path), project_root=tmp_path).service("complete-speech")
    assert all(service[key] is None for key in ("timeout_seconds", "max_response_bytes", "max_input_chars"))


@pytest.mark.asyncio
@pytest.mark.parametrize("han", [2000, 5000])
async def test_streamed_speech_keeps_entire_input_and_cleans_processing_source(tmp_path, monkeypatch, han):
    service = VoiceCatalog.load(instance_path=catalogue(tmp_path), project_root=tmp_path).service("complete-speech")
    chunk = b"\xff\xf3\x84\xc0" + bytes(16380)
    count = 300
    digest = hashlib.sha256(chunk * count).hexdigest()
    observed = {}

    class Content:
        async def iter_chunked(self, size):
            assert size <= 65536
            for _ in range(count):
                yield chunk

    class Response:
        status = 200
        headers = {"Content-Type": "audio/mpeg"}
        content = Content()
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return False

    class Session:
        def __init__(self, *, timeout, trust_env):
            observed.update(total=timeout.total, idle=timeout.sock_read, trust_env=trust_env)
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return False
        def post(self, url, *, json, headers, allow_redirects):
            observed.update(input=json["input"], redirects=allow_redirects)
            return Response()

    async def convert(ffmpeg, source, ogg):
        assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
        observed["source"] = source
        ogg.write_bytes(b"OggS-complete")

    monkeypatch.setattr("orchestrator.tts_providers.openai_compatible.convert_audio_to_ogg", convert)
    monkeypatch.setattr("orchestrator.tts_providers.openai_compatible.configured_media_executable", lambda *a, **k: None)
    provider = OpenAICompatibleTTSProvider(service_id="complete-speech", service=service, session_factory=Session)
    text = "汉" * han + "最后一句。"
    asset = await provider.synthesize(text=text, output_dir=tmp_path / "out", stem="reply",
        voice_name="zf_xiaoxiao", max_chars=len(text))
    assert observed["input"] == text
    assert observed["total"] is None and 0 < observed["idle"] <= 120
    assert observed["trust_env"] is False and observed["redirects"] is False
    assert not observed["source"].exists()
    assert asset.ogg_path.read_bytes() == b"OggS-complete"
    assert list((tmp_path / "out").iterdir()) == [asset.ogg_path]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [aiohttp.ClientPayloadError("incomplete response"), asyncio.CancelledError()])
async def test_interrupted_stream_never_publishes_partial_audio(tmp_path, failure):
    service = VoiceCatalog.load(instance_path=catalogue(tmp_path), project_root=tmp_path).service("complete-speech")

    class Content:
        async def iter_chunked(self, size):
            yield b"ID3" + bytes(16381)
            raise failure

    class Response:
        status = 200
        headers = {"Content-Type": "audio/mpeg"}
        content = Content()
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return False

    class Session:
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return False
        def post(self, *a, **kw): return Response()

    provider = OpenAICompatibleTTSProvider(service_id="complete-speech", service=service, session_factory=Session)
    with pytest.raises(asyncio.CancelledError if isinstance(failure, asyncio.CancelledError) else OpenAICompatibleTTSError):
        await provider.synthesize(text="汉" * 5000, output_dir=tmp_path / "out", stem="reply", voice_name="zf_xiaoxiao")
    assert not list((tmp_path / "out").iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["workbench", "tui", "telegram"])
async def test_shared_voice_manager_uses_complete_input_for_each_consumer(tmp_path, monkeypatch, surface):
    manager = VoiceManager(tmp_path / "workspace", tmp_path / "media", catalog_path=catalogue(tmp_path), project_root=tmp_path)
    manager.apply_voice_preset("complete_voice")
    observed = []

    class Provider:
        async def synthesize(self, **kwargs):
            observed.append(kwargs["max_chars"])
            target = tmp_path / "test.ogg"
            target.write_bytes(b"OggS-test")
            return VoiceAsset(provider="complete-speech", text=kwargs["text"], spoken_text=kwargs["text"], wav_path=None, ogg_path=target)

    monkeypatch.setattr("orchestrator.voice_manager.build_provider", lambda *a, **kw: Provider())
    text = "汉" * 5541
    await manager.synthesize_reply("agent", "full-reply", text, force=True, source_surface=surface)
    assert observed == [len(text)]
    observed.clear()
    await manager.synthesize_reply("agent", "bounded-consumer", text, force=True, max_chars_override=128)
    assert observed == [128]


def test_one_oversized_response_does_not_cool_down_a_healthy_service(tmp_path):
    manager = VoiceManager(tmp_path / "workspace", tmp_path / "media", catalog_path=catalogue(tmp_path), project_root=tmp_path)
    manager._remember_provider_failure(VoiceProviderFailure("complete-speech", "tts_response_limit", "This response is too large.", False))
    assert manager._active_provider_failure("complete-speech") is None


@pytest.mark.parametrize("status", [400, 404, 413, 422])
def test_protocol_errors_keep_actual_http_status_instead_of_inventing_403(status):
    error = OpenAICompatibleTTSError("tts_protocol_rejected",
        f"Speech rejected the request (HTTP {status}).", status=status)
    failure = _provider_failure("complete-speech", error)
    assert f"HTTP {status}" in failure.reason
    assert "HTTP 403" not in failure.reason
    assert failure.retryable is False
