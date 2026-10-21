from __future__ import annotations

import json
import wave
from pathlib import Path

import pytest

from orchestrator.tts_providers import build_provider
from orchestrator.tts_providers.openai_compatible import (
    OpenAICompatibleTTSError,
    OpenAICompatibleTTSProvider,
)
from orchestrator.voice_catalog import VoiceCatalog


def _wav_bytes(path: Path) -> bytes:
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(b"\x00\x00" * 240)
    return path.read_bytes()


def _catalog(tmp_path: Path, *, credential_ref: str | None = None) -> VoiceCatalog:
    service = {
        "id": "compatible-speech",
        "adapter": "openai_compatible",
        "label": "Compatible speech",
        "location": "lan",
        "base_url": "http://192.168.10.22:19446/v1",
        "model": "speech-model",
        "audio_format": "wav",
        "request_options": {"temperature": 0.2},
        "fallbacks": [],
    }
    if credential_ref:
        service["credential_ref"] = credential_ref
    path = tmp_path / "voice_catalog.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "services": [service],
                "voices": [
                    {
                        "id": "compatible_voice",
                        "service": "compatible-speech",
                        "voice_name": "voice-1",
                        "label": "Voice 1",
                        "language": "English",
                        "locale": "en-US",
                        "gender": "female",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return VoiceCatalog.load(instance_path=path, project_root=tmp_path)


def test_configured_service_builds_the_shared_openai_compatible_adapter(tmp_path):
    catalog = _catalog(tmp_path)
    provider = build_provider(
        "compatible-speech",
        provider_config=catalog.service("compatible-speech"),
        bridge_home=tmp_path,
    )

    assert isinstance(provider, OpenAICompatibleTTSProvider)
    assert provider.service_id == "compatible-speech"


@pytest.mark.asyncio
async def test_adapter_posts_standard_speech_payload_and_returns_voice_asset(
    tmp_path, monkeypatch
):
    wav = _wav_bytes(tmp_path / "source.wav")
    observed = {}

    class Content:
        async def iter_chunked(self, _size):
            yield wav

    class Response:
        status = 200
        headers = {"Content-Type": "audio/wav"}
        content = Content()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    class Session:
        def __init__(self, *, timeout, trust_env):
            observed["timeout"] = timeout.total
            observed["trust_env"] = trust_env

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def post(self, url, *, json, headers, allow_redirects):
            observed.update(
                url=url,
                json=json,
                headers=headers,
                allow_redirects=allow_redirects,
            )
            return Response()

    async def fake_convert(ffmpeg, wav_path, ogg_path):
        observed["ffmpeg"] = ffmpeg
        assert wav_path.read_bytes() == wav
        ogg_path.write_bytes(b"OggS-compatible")

    monkeypatch.setattr(
        "orchestrator.tts_providers.openai_compatible.configured_media_executable",
        lambda _name, *, bridge_home=None: str(tmp_path / "ffmpeg"),
    )
    monkeypatch.setattr(
        "orchestrator.tts_providers.openai_compatible.convert_wav_to_ogg",
        fake_convert,
    )
    catalog = _catalog(tmp_path, credential_ref="secrets://tts_key")
    provider = OpenAICompatibleTTSProvider(
        service_id="compatible-speech",
        service=catalog.service("compatible-speech"),
        secrets={"tts_key": "secret-value"},
        bridge_home=tmp_path,
        session_factory=Session,
    )

    asset = await provider.synthesize(
        text="Hello.",
        output_dir=tmp_path / "voice",
        stem="reply",
        voice_name="voice-1",
        provider_options={"speed": 1.1},
    )

    assert observed["url"] == "http://192.168.10.22:19446/v1/audio/speech"
    assert observed["json"] == {
        "model": "speech-model",
        "input": "Hello.",
        "voice": "voice-1",
        "response_format": "wav",
        "temperature": 0.2,
        "speed": 1.1,
    }
    assert observed["headers"] == {"Authorization": "Bearer secret-value"}
    assert observed["allow_redirects"] is False
    assert observed["trust_env"] is False
    assert observed["ffmpeg"] == str(tmp_path / "ffmpeg")
    assert asset.provider == "compatible-speech"
    assert asset.wav_path.read_bytes() == wav
    assert asset.ogg_path.read_bytes() == b"OggS-compatible"


@pytest.mark.asyncio
async def test_adapter_fails_before_network_when_credential_reference_is_missing(
    tmp_path,
):
    catalog = _catalog(tmp_path, credential_ref="secrets://tts_key")
    provider = OpenAICompatibleTTSProvider(
        service_id="compatible-speech",
        service=catalog.service("compatible-speech"),
        secrets={},
        bridge_home=tmp_path,
    )

    with pytest.raises(OpenAICompatibleTTSError, match="credential"):
        await provider.synthesize(
            text="Hello.",
            output_dir=tmp_path,
            stem="reply",
            voice_name="voice-1",
        )
