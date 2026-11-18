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
        "options": {"style": {"type": "string", "max_length": 160}}
        if kind == "tts"
        else {},
    }
    if kind == "tts":
        value.update(
            voices=["Achernar"], voice_styles={"Achernar": "Soft"}, audio_format="pcm"
        )
    return value


@pytest.mark.asyncio
async def test_lan_resolver_filters_public_and_metadata_addresses():
    from orchestrator.lan_transport import LanDestinationError, PrivateLanResolver

    class Delegate:
        def __init__(self, rows): self.rows, self.closed = rows, False
        async def resolve(self, host, port=0, family=0):
            return [{"host": item} for item in self.rows]
        async def close(self): self.closed = True

    delegate = Delegate(["8.8.8.8", "169.254.169.254", "fd00:ec2::254", "10.2.3.4"])
    resolver = PrivateLanResolver(delegate)
    assert await resolver.resolve("gpu.home.arpa", 80) == [{"host": "10.2.3.4"}]
    await resolver.close()
    assert delegate.closed
    for values in ([], ["8.8.8.8"], ["100.100.100.200"], ["fd00:ec2::254"]):
        with pytest.raises(LanDestinationError, match="no permitted private address"):
            await PrivateLanResolver(Delegate(values)).resolve("gpu.home.arpa", 80)


@pytest.mark.asyncio
async def test_lan_hostname_uses_verified_addresses_for_the_actual_request(monkeypatch):
    import socket
    from aiohttp import web
    from orchestrator import lan_transport

    calls, resolved = [], []
    async def handler(request):
        calls.append({"host": request.host, "path": request.path})
        if request.path.endswith("/redirect"):
            raise web.HTTPFound("http://8.8.8.8/should-not-be-called")
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_post("/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    class Delegate:
        async def resolve(self, host, selected_port=0, family=socket.AF_UNSPEC):
            resolved.append(host)
            # A second DNS lookup would return a public address.
            address = "127.0.0.1" if len(resolved) == 1 else "8.8.8.8"
            return [{"hostname": host, "host": address, "port": selected_port,
                     "family": socket.AF_INET, "proto": 0, "flags": 0}]
        async def close(self): pass

    # The fixture's only listener is loopback. Keep the real private-address
    # validation for production while allowing this single synthetic endpoint.
    private = lan_transport.is_private_lan_address
    monkeypatch.setattr(lan_transport, "is_private_lan_address", lambda host: host == "127.0.0.1" or private(host))
    monkeypatch.setattr(lan_transport.aiohttp.resolver, "DefaultResolver", Delegate)
    target = {"location": "lan", "base_url": f"http://vision.test:{port}/v1", "model": "fixture"}
    try:
        payload, mime, _ = await MediaAdapters()._request(target, "/complete", json_body={})
        assert json.loads(payload) == {"ok": True}
        assert mime.split(";", 1)[0] == "application/json"
        assert resolved == ["vision.test"]
        assert calls == [{"host": f"vision.test:{port}", "path": "/v1/complete"}]

        # A fresh connection resolves again, sees the public address, and never
        # makes another request or follows it to an external destination.
        with pytest.raises(CallError, match="call_lan_target_not_private"):
            await MediaAdapters()._request(target, "/complete", json_body={})
        assert len(calls) == 1
        for address in ("8.8.8.8", "169.254.169.254", "100.100.100.200"):
            with pytest.raises(CallError, match="call_lan_target_not_private"):
                await MediaAdapters()._request(
                    {**target, "base_url": f"http://{address}/v1"}, "/complete", json_body={},
                )
        assert len(calls) == 1
        resolved.clear()
        with pytest.raises(CallError, match="call_provider_rejected"):
            await MediaAdapters()._request(target, "/redirect", json_body={})
        assert [call["path"] for call in calls] == ["/v1/complete", "/v1/redirect"]
    finally:
        await runner.cleanup()


def test_openrouter_target_keeps_secret_reference_private_and_validates_voice_styles():
    target = validate_target(_target("tts", "google/gemini-3.8-flash-lite-tts"))
    assert target["voice_styles"] == {"Achernar": "Soft"}
    with pytest.raises(CallError):
        validate_target({**target, "voice_styles": {"Unknown": "Soft"}})
    with pytest.raises(CallError):
        validate_target({**target, "credential_ref": "file:///tmp/key"})
    with pytest.raises(CallError):
        options_for(target, {"provider": {"only": ["groq"]}})
    assert validate_target({**target, "model": "other/tts-model"})["model"] == "other/tts-model"
    with pytest.raises(CallError, match="call_audio_format_unsupported"):
        validate_target({**_target("tts", "other/tts-model")})


def test_gemini_style_is_provider_metadata_and_never_spoken():
    calls = []
    target = _target("tts", "google/gemini-3.8-flash-lite-tts")
    adapter = MediaAdapters(
        session_factory=lambda **_kw: _Session(calls, b"\x00\x01" * 4800, "audio/pcm"),
        secret_resolver=SimpleNamespace(
            resolve=lambda _ref: SimpleNamespace(value="test-key")
        ),
    )
    result = asyncio.run(
        adapter.synthesize(
            target,
            {"voice_id": "Achernar", "options": {"style": "warm and clear"}},
            "Hello.",
        )
    )
    assert result["media_type"] == "audio/wav"
    with wave.open(io.BytesIO(base64.b64decode(result["content_b64"])), "rb") as audio:
        assert (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) == (
            1,
            2,
            24000,
        )
        assert audio.getnframes() == 4800
    url, request = calls[0]
    assert url.endswith("/audio/speech")
    assert request["headers"]["Authorization"] == "Bearer test-key"
    body = request["json"]
    assert body["input"] == "Hello."
    assert body["voice"] == "Achernar"
    assert body["response_format"] == "pcm"
    assert "style" not in body
    assert (
        body["provider"]["options"]["google-ai-studio"]["speech_metadata"]["style"]
        == "warm and clear"
    )


@pytest.mark.parametrize("base_url", [
    "https://compatible.example/v1", "https://proxy.example/v1",
])
def test_declared_pcm_codec_decodes_identically_for_direct_and_proxy_endpoints(base_url):
    raw = b"\x01\x02\x03\x04" * 1600
    target = validate_target({
        **_target("tts", "compatible/tts"), "base_url": base_url,
        "pcm_sample_rate_hz": 16000, "pcm_sample_width_bytes": 2,
        "pcm_channels": 2, "pcm_encoding": "s16le",
    })
    calls = []
    adapter = MediaAdapters(
        session_factory=lambda **_kw: _Session(
            calls, raw, "audio/pcm; rate=16000; channels=2; bits=16; encoding=s16le"),
        secret_resolver=SimpleNamespace(resolve=lambda _ref: SimpleNamespace(value="test-key")),
    )
    result = asyncio.run(adapter.synthesize(
        target, {"voice_id": "Achernar", "options": {}}, "Hello."))
    with wave.open(io.BytesIO(base64.b64decode(result["content_b64"])), "rb") as audio:
        assert (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) == (2, 2, 16000)
        assert audio.readframes(audio.getnframes()) == raw
    assert result["_billing_usage"]["audio_seconds"] == pytest.approx(0.1)
    assert calls[0][0] == base_url + "/audio/speech"


@pytest.mark.parametrize("change", [
    {"pcm_sample_rate_hz": None}, {"pcm_sample_rate_hz": 0},
    {"pcm_channels": 3}, {"pcm_sample_width_bytes": 4}, {"pcm_encoding": "f32le"},
])
def test_pcm_rejects_missing_or_unsupported_codec_before_request(change):
    target = {
        **_target("tts", "compatible/tts"), "base_url": "https://proxy.example/v1",
        "pcm_sample_rate_hz": 16000, "pcm_sample_width_bytes": 2,
        "pcm_channels": 2, "pcm_encoding": "s16le", **change,
    }
    with pytest.raises(CallError, match="call_audio_format_unsupported"):
        validate_target(target)
    adapter = MediaAdapters(session_factory=lambda **_kw: pytest.fail("must not contact provider"))
    with pytest.raises(CallError, match="call_audio_format_unsupported"):
        asyncio.run(adapter.synthesize(target, {"voice_id": "Achernar", "options": {}}, "Hello."))


@pytest.mark.parametrize("mime", [
    "audio/pcm; rate=24000; channels=2", "audio/pcm; rate=16000; channels=1",
    "audio/pcm; rate=16000; channels=2; bits=8",
    "audio/pcm; rate=16000; channels=2; encoding=f32le",
    "audio/pcm; rate=16000; rate=24000; channels=2",
])
def test_pcm_response_metadata_must_match_declared_codec(mime):
    target = validate_target({
        **_target("tts", "compatible/tts"), "base_url": "https://proxy.example/v1",
        "pcm_sample_rate_hz": 16000, "pcm_sample_width_bytes": 2,
        "pcm_channels": 2, "pcm_encoding": "s16le",
    })
    adapter = MediaAdapters(
        session_factory=lambda **_kw: _Session([], b"\x00" * 6400, mime),
        secret_resolver=SimpleNamespace(resolve=lambda _ref: SimpleNamespace(value="test-key")),
    )
    with pytest.raises(CallError, match="call_speech_format_invalid"):
        asyncio.run(adapter.synthesize(target, {"voice_id": "Achernar", "options": {}}, "Hello."))


def test_pcm_accepts_complete_short_audio_but_rejects_an_incomplete_stereo_frame():
    target = validate_target({
        **_target("tts", "compatible/tts"), "base_url": "https://proxy.example/v1",
        "pcm_sample_rate_hz": 16000, "pcm_sample_width_bytes": 2,
        "pcm_channels": 2, "pcm_encoding": "s16le",
    })
    def adapter(raw):
        return MediaAdapters(
            session_factory=lambda **_kw: _Session([], raw, "audio/pcm"),
            secret_resolver=SimpleNamespace(resolve=lambda _ref: SimpleNamespace(value="test-key")),
        )
    result = asyncio.run(adapter(b"\x01\x02\x03\x04").synthesize(
        target, {"voice_id": "Achernar", "options": {}}, "Hello."))
    with wave.open(io.BytesIO(base64.b64decode(result["content_b64"])), "rb") as audio:
        assert audio.getnframes() == 1 and audio.readframes(1) == b"\x01\x02\x03\x04"
    with pytest.raises(CallError, match="call_speech_format_invalid"):
        asyncio.run(adapter(b"\x01\x02").synthesize(
            target, {"voice_id": "Achernar", "options": {}}, "Hello."))


def test_gemini_pcm_rejects_invalid_or_truncated_audio():
    target = _target("tts", "google/gemini-3.8-flash-lite-tts")
    for payload, mime in (
        (b"\0" * 4801, "audio/pcm"),
        (b"\0" * 4800, "application/json"),
    ):
        adapter = MediaAdapters(
            session_factory=lambda **_kw: _Session([], payload, mime),
            secret_resolver=SimpleNamespace(
                resolve=lambda _ref: SimpleNamespace(value="test-key")
            ),
        )
        with pytest.raises(CallError, match="call_speech_format_invalid"):
            asyncio.run(
                adapter.synthesize(
                    target, {"voice_id": "Achernar", "options": {}}, "Hello."
                )
            )


def test_whisper_transcription_uses_openrouter_multipart_and_secret_reference():
    calls = []
    adapter = MediaAdapters(
        session_factory=lambda **_kw: _Session(
            calls, json.dumps({"text": "你好"}).encode(), "application/json"
        ),
        secret_resolver=SimpleNamespace(
            resolve=lambda _ref: SimpleNamespace(value="test-key")
        ),
    )
    result = asyncio.run(
        adapter.transcribe(
            _target("stt", "openai/whisper-large-v3"), {"options": {}}, b"RIFFtest"
        )
    )
    assert result["text"] == "你好"
    assert result["provider_receipt"] is None
    url, request = calls[0]
    assert url.endswith("/audio/transcriptions")
    assert request["headers"]["Authorization"] == "Bearer test-key"
    assert request["data"]._fields[1][2] == "openai/whisper-large-v3"
    fields = {field[0]["name"]: field[2] for field in request["data"]._fields}
    assert fields["response_format"] == "verbose_json"
    assert "language" not in fields


@pytest.mark.parametrize(
    "payload",
    [
        {"text": ""},
        {"text": "谢谢", "segments": [{"no_speech_prob": 0.97, "avg_logprob": -2.1}]},
    ],
)
def test_stt_preserves_empty_cloud_result_and_speech_evidence(payload):
    adapter = MediaAdapters(
        session_factory=lambda **_kw: _Session(
            [], json.dumps(payload).encode(), "application/json"
        ),
    )
    target = {**_target("stt", "openai/whisper-large-v3"), "credential_ref": None}
    result = asyncio.run(adapter.transcribe(target, {"options": {}}, b"RIFFtest"))
    assert result["text"] == payload["text"]
    assert result.get("segments") == payload.get("segments")


@pytest.mark.parametrize(
    "payload", [{}, {"text": None}, {"text": []}, {"text": "x" * 12001}]
)
def test_stt_malformed_cloud_result_remains_a_service_error(payload):
    adapter = MediaAdapters(
        session_factory=lambda **_kw: _Session(
            [], json.dumps(payload).encode(), "application/json"
        ),
    )
    target = {**_target("stt", "openai/whisper-large-v3"), "credential_ref": None}
    with pytest.raises(CallError, match="call_transcription_empty_or_invalid"):
        asyncio.run(adapter.transcribe(target, {"options": {}}, b"RIFFtest"))


@pytest.mark.parametrize(
    "verdict",
    [
        {"has_speech": False, "text": ""},
        {"has_speech": True, "text": "现在我说7"},
    ],
)
def test_audio_chat_transcribes_and_judges_speech_in_one_request(verdict):
    calls = []
    response = {"choices": [{"message": {"content": json.dumps(verdict)}}]}
    adapter = MediaAdapters(
        session_factory=lambda **_kw: _Session(
            calls, json.dumps(response).encode(), "application/json"
        )
    )
    target = {
        **_target("stt", "google/gemini-2.5-flash-lite"),
        "credential_ref": None,
        "stt_protocol": "audio_chat",
    }
    target = validate_target(target)
    result = asyncio.run(adapter.transcribe(target, {"options": {}}, b"RIFFtest"))
    assert (
        result["text"] == verdict["text"]
        and result["has_speech"] is verdict["has_speech"]
    )
    assert len(calls) == 1 and calls[0][0].endswith("/chat/completions")
    body = calls[0][1]["json"]
    audio = body["messages"][1]["content"][0]["input_audio"]
    assert audio == {"data": base64.b64encode(b"RIFFtest").decode(), "format": "wav"}
    schema = body["response_format"]["json_schema"]["schema"]
    assert schema["properties"]["has_speech"]["type"] == "boolean"
    assert set(schema["required"]) == {"has_speech", "text"}
    assert "language" not in body and "tools" not in body


@pytest.mark.parametrize(
    "verdict",
    [
        {"text": "hello"},
        {"has_speech": "false", "text": ""},
        {"has_speech": True, "text": None},
        {"has_speech": True, "text": "x" * 12001},
        {"has_speech": True, "text": ""},
        {"has_speech": True, "text": "  \n "},
        {"has_speech": False, "text": "hello"},
    ],
)
def test_audio_chat_missing_or_invalid_speech_judgment_is_a_service_error(verdict):
    response = {"choices": [{"message": {"content": json.dumps(verdict)}}]}
    adapter = MediaAdapters(
        session_factory=lambda **_kw: _Session(
            [], json.dumps(response).encode(), "application/json"
        )
    )
    target = {
        **_target("stt", "google/gemini-2.5-flash-lite"),
        "credential_ref": None,
        "stt_protocol": "audio_chat",
    }
    with pytest.raises(CallError, match="call_transcription_empty_or_invalid"):
        asyncio.run(adapter.transcribe(target, {"options": {}}, b"RIFFtest"))


def test_speech_protocol_is_instance_owned_and_not_available_to_other_modalities():
    with pytest.raises(CallError, match="call_adapter_unsupported"):
        validate_target(
            {**_target("stt", "other/model"), "stt_protocol": "unconfigured"}
        )
    with pytest.raises(CallError, match="call_adapter_unsupported"):
        validate_target(
            {
                **_target("tts", "google/gemini-3.8-flash-lite-tts"),
                "stt_protocol": "audio_chat",
            }
        )
# HASHI-Fingerprint: hfp_271598a1417547cda05126e340855584
