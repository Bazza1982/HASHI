"""OpenRouter media options preserve credentials and Gemini speech metadata."""

import asyncio
import base64
import io
import json
import wave
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
        value.update(voices=["Achernar"], voice_styles={"Achernar": "Soft"}, audio_format="pcm")
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
    with pytest.raises(CallError):
        validate_target({**target, "model": "other/tts-model"})


def test_gemini_style_is_provider_metadata_and_never_spoken():
    calls = []
    target = _target("tts", "google/gemini-3.8-flash-lite-tts")
    adapter = MediaAdapters(
        session_factory=lambda **_kw: _Session(calls, b"\x00\x01" * 4800, "audio/pcm"),
        secret_resolver=SimpleNamespace(resolve=lambda _ref: SimpleNamespace(value="test-key")),
    )
    result = asyncio.run(
        adapter.synthesize(target, {"voice_id": "Achernar", "options": {"style": "warm and clear"}}, "Hello.")
    )
    assert result["media_type"] == "audio/wav"
    with wave.open(io.BytesIO(base64.b64decode(result["content_b64"])), "rb") as audio:
        assert (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) == (1, 2, 24000)
        assert audio.getnframes() == 4800
    url, request = calls[0]
    assert url.endswith("/audio/speech")
    assert request["headers"]["Authorization"] == "Bearer test-key"
    body = request["json"]
    assert body["input"] == "Hello."
    assert body["voice"] == "Achernar"
    assert body["response_format"] == "pcm"
    assert "style" not in body
    assert body["provider"]["options"]["google-ai-studio"]["speech_metadata"]["style"] == "warm and clear"


def test_gemini_pcm_rejects_invalid_or_truncated_audio():
    target = _target("tts", "google/gemini-3.8-flash-lite-tts")
    for payload, mime in ((b"\0" * 4801, "audio/pcm"), (b"\0" * 4800, "application/json")):
        adapter = MediaAdapters(
            session_factory=lambda **_kw: _Session([], payload, mime),
            secret_resolver=SimpleNamespace(resolve=lambda _ref: SimpleNamespace(value="test-key")),
        )
        with pytest.raises(CallError, match="call_speech_format_invalid"):
            asyncio.run(adapter.synthesize(target, {"voice_id": "Achernar", "options": {}}, "Hello."))


def test_whisper_transcription_uses_openrouter_multipart_and_secret_reference():
    calls = []
    adapter = MediaAdapters(
        session_factory=lambda **_kw: _Session(calls, json.dumps({"text": "你好"}).encode(), "application/json"),
        secret_resolver=SimpleNamespace(resolve=lambda _ref: SimpleNamespace(value="test-key")),
    )
    result = asyncio.run(adapter.transcribe(_target("stt", "openai/whisper-large-v3"), {"options": {}}, b"RIFFtest"))
    assert result["text"] == "你好"
    assert result["provider_receipt"] is None
    url, request = calls[0]
    assert url.endswith("/audio/transcriptions")
    assert request["headers"]["Authorization"] == "Bearer test-key"
    assert request["data"]._fields[1][2] == "openai/whisper-large-v3"
    fields = {field[0]["name"]: field[2] for field in request["data"]._fields}
    assert fields["response_format"] == "verbose_json"
    assert "language" not in fields


@pytest.mark.parametrize("payload", [
    {"text": ""},
    {"text": "谢谢", "segments": [{"no_speech_prob": .97, "avg_logprob": -2.1}]},
])
def test_stt_preserves_empty_cloud_result_and_speech_evidence(payload):
    adapter = MediaAdapters(
        session_factory=lambda **_kw: _Session([], json.dumps(payload).encode(), "application/json"),
    )
    target = {**_target("stt", "openai/whisper-large-v3"), "credential_ref": None}
    result = asyncio.run(adapter.transcribe(target, {"options": {}}, b"RIFFtest"))
    assert result["text"] == payload["text"]
    assert result.get("segments") == payload.get("segments")


@pytest.mark.parametrize("payload", [{}, {"text": None}, {"text": []}, {"text": "x" * 12001}])
def test_stt_malformed_cloud_result_remains_a_service_error(payload):
    adapter = MediaAdapters(
        session_factory=lambda **_kw: _Session([], json.dumps(payload).encode(), "application/json"),
    )
    target = {**_target("stt", "openai/whisper-large-v3"), "credential_ref": None}
    with pytest.raises(CallError, match="call_transcription_empty_or_invalid"):
        asyncio.run(adapter.transcribe(target, {"options": {}}, b"RIFFtest"))


@pytest.mark.parametrize("verdict", [
    {"has_speech": False, "text": ""},
    {"has_speech": True, "text": "现在我说7"},
])
def test_audio_chat_transcribes_and_judges_speech_in_one_request(verdict):
    calls = []
    response = {"choices": [{"message": {"content": json.dumps(verdict)}}]}
    adapter = MediaAdapters(session_factory=lambda **_kw: _Session(calls, json.dumps(response).encode(), "application/json"))
    target = {**_target("stt", "google/gemini-2.5-flash-lite"),
              "credential_ref": None, "stt_protocol": "audio_chat"}
    target = validate_target(target)
    result = asyncio.run(adapter.transcribe(target, {"options": {}}, b"RIFFtest"))
    assert result["text"] == verdict["text"] and result["has_speech"] is verdict["has_speech"]
    assert len(calls) == 1 and calls[0][0].endswith("/chat/completions")
    body = calls[0][1]["json"]
    audio = body["messages"][1]["content"][0]["input_audio"]
    assert audio == {"data": base64.b64encode(b"RIFFtest").decode(), "format": "wav"}
    schema = body["response_format"]["json_schema"]["schema"]
    assert schema["properties"]["has_speech"]["type"] == "boolean"
    assert set(schema["required"]) == {"has_speech", "text"}
    assert "language" not in body and "tools" not in body


@pytest.mark.parametrize("verdict", [
    {"text": "hello"}, {"has_speech": "false", "text": ""},
    {"has_speech": True, "text": None}, {"has_speech": True, "text": "x" * 12001},
    {"has_speech": True, "text": ""}, {"has_speech": True, "text": "  \n "},
    {"has_speech": False, "text": "hello"},
])
def test_audio_chat_missing_or_invalid_speech_judgment_is_a_service_error(verdict):
    response = {"choices": [{"message": {"content": json.dumps(verdict)}}]}
    adapter = MediaAdapters(session_factory=lambda **_kw: _Session([], json.dumps(response).encode(), "application/json"))
    target = {**_target("stt", "google/gemini-2.5-flash-lite"),
              "credential_ref": None, "stt_protocol": "audio_chat"}
    with pytest.raises(CallError, match="call_transcription_empty_or_invalid"):
        asyncio.run(adapter.transcribe(target, {"options": {}}, b"RIFFtest"))


def test_speech_protocol_is_instance_owned_and_not_available_to_other_modalities():
    with pytest.raises(CallError, match="call_adapter_unsupported"):
        validate_target({**_target("stt", "other/model"), "stt_protocol": "unconfigured"})
    with pytest.raises(CallError, match="call_adapter_unsupported"):
        validate_target({**_target("tts", "google/gemini-3.8-flash-lite-tts"), "stt_protocol": "audio_chat"})
