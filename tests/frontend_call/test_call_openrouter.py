"""OpenRouter media options preserve credentials and Gemini speech metadata."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from orchestrator.frontend_call.adapters import MediaAdapters
from orchestrator.frontend_call.config import options_for, validate_target
from orchestrator.frontend_call.contract import CallError


class _Content:
    def __init__(self, payload):
        self.payload = payload

    async def iter_chunked(self, _size):
        yield self.payload


class _Response:
    status = 200

    def __init__(self, payload, mime):
        self.headers = {"Content-Type": mime}
        self.content = _Content(payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class _Session:
    def __init__(self, calls, payload, mime):
        self.calls = calls
        self.payload = payload
        self.mime = mime

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _Response(self.payload, self.mime)


def _target(kind, model):
    value = {
        "id": f"openrouter-{kind}",
        "kind": kind,
        "adapter": "openai_compatible",
        "location": "cloud",
        "base_url": "https://openrouter.ai/api/v1",
        "model": model,
        "credential_ref": "secrets://openrouter-api_key",
        "options": {"style": {"type": "string", "max_length": 160}} if kind == "tts" else {},
    }
    if kind == "tts":
        value.update(voices=["Achernar"], voice_styles={"Achernar": "Soft"}, audio_format="mp3")
    return value


def test_openrouter_target_keeps_secret_reference_private_and_validates_voice_styles():
    target = validate_target(_target("tts", "google/gemini-3.8-flash-lite-tts"))
    assert target["voice_styles"] == {"Achernar": "Soft"}
    with pytest.raises(CallError):
        validate_target({**target, "voice_styles": {"Unknown": "Soft"}})
    with pytest.raises(CallError):
        validate_target({**target, "credential_ref": "file:///tmp/key"})
    with pytest.raises(CallError):
        options_for(target, {"provider": {"only": ["groq"]}})


def test_gemini_style_is_provider_metadata_and_never_spoken():
    calls = []
    target = _target("tts", "google/gemini-3.8-flash-lite-tts")
    adapter = MediaAdapters(
        session_factory=lambda **_kw: _Session(calls, b"ID3audio", "audio/mpeg"),
        secret_resolver=SimpleNamespace(resolve=lambda _ref: SimpleNamespace(value="test-key")),
    )
    result = asyncio.run(
        adapter.synthesize(target, {"voice_id": "Achernar", "options": {"style": "warm and clear"}}, "Hello.")
    )
    assert result["media_type"] == "audio/mpeg"
    url, request = calls[0]
    assert url.endswith("/audio/speech")
    assert request["headers"]["Authorization"] == "Bearer test-key"
    body = request["json"]
    assert body["input"] == "Hello."
    assert body["voice"] == "Achernar"
    assert "style" not in body
    assert body["provider"]["options"]["google-ai-studio"]["speech_metadata"]["style"] == "warm and clear"


def test_whisper_transcription_uses_openrouter_multipart_and_secret_reference():
    calls = []
    adapter = MediaAdapters(
        session_factory=lambda **_kw: _Session(calls, json.dumps({"text": "你好"}).encode(), "application/json"),
        secret_resolver=SimpleNamespace(resolve=lambda _ref: SimpleNamespace(value="test-key")),
    )
    result = asyncio.run(adapter.transcribe(_target("stt", "openai/whisper-large-v3"), {"options": {}}, b"RIFFtest"))
    assert result["text"] == "你好"
    assert result["provider_receipt"]["verification"] == "unverified"
    url, request = calls[0]
    assert url.endswith("/audio/transcriptions")
    assert request["headers"]["Authorization"] == "Bearer test-key"
    assert request["data"]._fields[1][2] == "openai/whisper-large-v3"
