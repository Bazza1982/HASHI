from __future__ import annotations

import io
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.compute_services import ComputeNode
from orchestrator.gemini_tts_catalog import GEMINI_TTS_MODEL
from orchestrator.tts_providers import hashi_compute as hashi_compute_module
from orchestrator.tts_providers import openrouter as openrouter_module
from orchestrator.tts_providers.hashi_compute import HashiComputeTTSProvider
from orchestrator.tts_providers.openrouter import OpenRouterTTSProvider


def _wav_bytes() -> bytes:
    stream = io.BytesIO()
    with wave.open(stream, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24000)
        output.writeframes(b"\0\0" * 2400)
    return stream.getvalue()


async def _fake_convert(_ffmpeg, _wav_path, ogg_path):
    Path(ogg_path).write_bytes(b"OggS-test")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("style", "expected_path", "input_text", "expected_input"),
    [
        (None, "/v1/audio/speech", "语" * 1500, "语" * 999 + "."),
        (
            "温柔而自然",
            "/v1/audio/speech/expressive",
            "语音测试",
            "语音测试",
        ),
    ],
)
async def test_hashi_compute_provider_uses_exact_directory_route(
    tmp_path, monkeypatch, style, expected_path, input_text, expected_input
):
    node = ComputeNode(
        id="hashi-compute",
        label="Hashi Compute",
        location="lan",
        base_url="http://192.168.1.50:19446",
        manifest_path="/v1/manifest",
    ).validated()
    receipt = {}

    class Client:
        async def post_json_resource_bytes(self, selected_node, path, body):
            receipt.update(node=selected_node, path=path, body=body)
            return _wav_bytes(), "audio/wav"

    class Offering:
        audio = SimpleNamespace(output_formats=("wav", "mp3", "pcm"))

        def interface(self, protocol):
            paths = {
                "openai.audio.speech": "/v1/audio/speech",
                "hashi.audio.speech.expressive": "/v1/audio/speech/expressive",
            }
            return SimpleNamespace(path=paths[protocol])

    class Catalogue:
        def resolve(self, reference, requirements, *, allow_busy):
            assert reference.node_id == "hashi-compute"
            assert reference.service_id == "advanced_tts"
            assert reference.model_id == "minicpm-o-4.5-zh-female"
            assert requirements.locations == frozenset({"local", "lan", "internet"})
            assert allow_busy is True
            return SimpleNamespace(
                snapshot=SimpleNamespace(node=node),
                offering=Offering(),
            )

    class Directory:
        def __init__(self, _config, *, client):
            assert isinstance(client, Client)

        async def refresh(self):
            return SimpleNamespace(catalogue=Catalogue())

    monkeypatch.setattr(hashi_compute_module, "ComputeServiceDirectory", Directory)
    monkeypatch.setattr(hashi_compute_module, "convert_wav_to_ogg", _fake_convert)
    options = {
        "service": "hashi_compute",
        "node_id": "hashi-compute",
        "offering_id": "advanced_tts",
        "model": "minicpm-o-4.5-zh-female",
        "speed": 1.25,
    }
    if style:
        options["style"] = style

    asset = await HashiComputeTTSProvider(
        project_root=tmp_path,
        client=Client(),
    ).synthesize(
        text=input_text,
        output_dir=tmp_path / "audio",
        stem="route",
        voice_name="zf_001",
        provider_options=options,
    )

    assert receipt["node"] == node
    assert receipt["path"] == expected_path
    assert receipt["body"] == {
        "model": "minicpm-o-4.5-zh-female",
        "input": expected_input,
        "voice": "zf_001",
        "response_format": "mp3",
        "speed": 1.25,
        **({"instructions": style} if style else {}),
    }
    assert asset.provider == "hashi_compute"
    assert asset.ogg_path.read_bytes().startswith(b"OggS")


class _Content:
    def __init__(self, payload: bytes):
        self.payload = payload

    async def iter_chunked(self, _size):
        yield self.payload


class _Response:
    def __init__(self, payload: bytes, *, media_type="audio/pcm", status=200):
        self.status = status
        self.headers = {"Content-Type": media_type}
        self.content = _Content(payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _Session:
    def __init__(self, response, receipt):
        self.response = response
        self.receipt = receipt

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def post(self, url, **kwargs):
        self.receipt.update(url=url, request=kwargs)
        return self.response


class _SessionFactory:
    def __init__(self, response):
        self.response = response
        self.receipt = {}

    def __call__(self, **kwargs):
        self.receipt["session"] = kwargs
        return _Session(self.response, self.receipt)


@pytest.mark.asyncio
async def test_openrouter_provider_reuses_qualified_gemini_pcm_contract(
    tmp_path, monkeypatch
):
    factory = _SessionFactory(_Response(b"\0\0" * 2400))
    monkeypatch.setattr(openrouter_module, "convert_wav_to_ogg", _fake_convert)

    asset = await OpenRouterTTSProvider(
        secrets={"openrouter-api_key": "test-token"},
        session_factory=factory,
    ).synthesize(
        text="语音测试",
        output_dir=tmp_path / "audio",
        stem="openrouter",
        voice_name="Aoede",
        provider_options={
            "service": "openrouter",
            "model": GEMINI_TTS_MODEL,
            "style": "温柔而自然",
        },
    )

    assert factory.receipt["url"] == "https://openrouter.ai/api/v1/audio/speech"
    assert factory.receipt["request"]["allow_redirects"] is False
    assert factory.receipt["request"]["headers"] == {
        "Authorization": "Bearer test-token"
    }
    assert factory.receipt["request"]["json"] == {
        "model": GEMINI_TTS_MODEL,
        "input": "语音测试",
        "voice": "Aoede",
        "response_format": "pcm",
        "provider": {
            "options": {
                "google-ai-studio": {
                    "speech_metadata": {"style": "温柔而自然"}
                }
            }
        },
    }
    with wave.open(str(asset.wav_path), "rb") as source:
        assert source.getframerate() == 24000
        assert source.getnchannels() == 1
        assert source.getsampwidth() == 2
    assert asset.ogg_path.read_bytes().startswith(b"OggS")


@pytest.mark.asyncio
async def test_openrouter_provider_rejects_tiny_pcm_error_body(tmp_path):
    factory = _SessionFactory(_Response(b"{}"))
    provider = OpenRouterTTSProvider(
        secrets={"openrouter-api_key": "test-token"},
        session_factory=factory,
    )

    with pytest.raises(RuntimeError, match="malformed PCM"):
        await provider.synthesize(
            text="语音测试",
            output_dir=tmp_path / "audio",
            stem="openrouter",
            voice_name="Aoede",
            provider_options={
                "service": "openrouter",
                "model": GEMINI_TTS_MODEL,
            },
        )
