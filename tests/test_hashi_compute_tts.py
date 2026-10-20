from __future__ import annotations

import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.compute_services import ComputeReference
from orchestrator.tts_providers import build_provider
from orchestrator.tts_providers.hashi_compute import HashiComputeTTSProvider


def _wav_bytes(path: Path) -> bytes:
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(b"\x00\x00" * 240)
    return path.read_bytes()


def test_provider_is_available_through_the_shared_tts_registry():
    assert isinstance(build_provider("hashi_compute"), HashiComputeTTSProvider)


@pytest.mark.asyncio
async def test_provider_resolves_exact_compute_reference_and_returns_voice_asset(
    tmp_path, monkeypatch
):
    wav = _wav_bytes(tmp_path / "source.wav")
    observed = {}

    class Catalogue:
        def resolve(self, reference, requirements):
            observed["reference"] = reference
            observed["requirements"] = requirements
            return SimpleNamespace(
                offering=SimpleNamespace(model_id="kokoro-82m"),
            )

    class Directory:
        def __init__(self, config, *, client):
            observed["config_path"] = config.path
            observed["client"] = client

        async def refresh(self):
            return SimpleNamespace(catalogue=Catalogue())

    class Client:
        async def invoke_bytes(self, resolved, protocol, payload):
            observed["resolved"] = resolved
            observed["protocol"] = protocol
            observed["payload"] = payload
            return wav, "audio/wav"

    configured_ffmpeg = tmp_path / "ffmpeg"

    def fake_media_executable(name, *, bridge_home=None):
        observed["media_name"] = name
        observed["media_home"] = bridge_home
        return str(configured_ffmpeg)

    async def fake_convert(ffmpeg, wav_path, ogg_path):
        assert ffmpeg == str(configured_ffmpeg)
        assert wav_path.read_bytes() == wav
        ogg_path.write_bytes(b"OggScompute")

    monkeypatch.setattr(
        "orchestrator.tts_providers.hashi_compute.configured_media_executable",
        fake_media_executable,
    )
    monkeypatch.setattr(
        "orchestrator.tts_providers.hashi_compute.convert_wav_to_ogg",
        fake_convert,
    )
    provider = HashiComputeTTSProvider(
        config_path=tmp_path / "compute_services.json",
        directory_factory=Directory,
        client_factory=Client,
    )
    asset = await provider.synthesize(
        text="你好，我的勇者。",
        output_dir=tmp_path / "voice",
        stem="reply",
        voice_name="zf_001",
        provider_options={
            "compute_reference": {
                "node_id": "hashi-compute",
                "service_id": "tts",
                "model_id": "kokoro-82m",
            },
            "response_format": "wav",
            "speed": 1.0,
        },
    )

    assert observed["reference"] == ComputeReference(
        "hashi-compute", "tts", "kokoro-82m"
    )
    assert observed["requirements"].locations == frozenset({"local", "lan"})
    assert observed["protocol"] == "openai.audio.speech"
    assert observed["payload"] == {
        "model": "kokoro-82m",
        "input": "你好，我的勇者。",
        "voice": "zf_001",
        "response_format": "wav",
        "speed": 1.0,
    }
    assert observed["media_name"] == "ffmpeg"
    assert observed["media_home"] == tmp_path
    assert asset.provider == "hashi_compute"
    assert asset.wav_path.read_bytes() == wav
    assert asset.ogg_path.read_bytes() == b"OggScompute"


@pytest.mark.asyncio
async def test_provider_requires_an_exact_configured_reference(tmp_path):
    provider = HashiComputeTTSProvider()
    with pytest.raises(RuntimeError, match="exact compute_reference"):
        await provider.synthesize(
            text="hello",
            output_dir=tmp_path,
            stem="reply",
            voice_name="zf_001",
            provider_options={},
        )
