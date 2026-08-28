"""Red-phase contracts for HASHI Native Audio Chat.

These tests intentionally describe the approved target interfaces before the
runtime implementation exists.  The normal fast ``pytest`` gate does not
collect ``tests/contract``; run this file explicitly while implementing Phases
1-4 of ``docs/HASHI_NATIVE_AUDIO_CHAT_DESIGN.md``.

The eight scenarios are deliberately smaller than the 34-item acceptance
matrix.  Existing retained tests continue to cover Safe Voice confirmation,
local media fallback, TTS, exact input routing, and HER first-ready text
resolution.  This file freezes only the new audio-specific seams.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from adapters.base import BackendCapabilities
from adapters.openrouter_api import OpenRouterAdapter
from orchestrator import multimodal_contract, runtime_media, workbench_api
from orchestrator.multimodal_contract import (
    InputCapability,
    canonical_request_content,
)
from orchestrator.session_store import SessionStore, SessionStoreError
from tests.test_runtime_media import _runtime as telegram_runtime
from tests.test_runtime_media import _update as telegram_update
from tests.test_session_api import _Request, _server

pytestmark = pytest.mark.contract


def _write_wav(path: Path, *, frames: bytes = b"\x00\x00" * 80) -> bytes:
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16_000)
        audio.writeframes(frames)
    return path.read_bytes()


def _canonical_voice_content(path: Path) -> dict:
    payload = path.read_bytes()
    return canonical_request_content(
        [
            {
                "type": "media",
                "item_index": 1,
                "attachment_id": "att-voice",
                "modality": "audio",
                "kind": "voice",
                "semantic_role": "voice_message",
                "mime_type": "audio/wav",
                "filename": path.name,
                "caption": "",
                "duration_ms": 10,
                "local_ref": str(path),
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "transport": {"message_id": 7},
            }
        ]
    )


def _audio_capable_backend() -> SimpleNamespace:
    capability = InputCapability(
        provider="openrouter-api",
        model="configured-audio-model",
        input_modalities=frozenset({"text", "audio"}),
        input_transports={"audio": ("inline",)},
        source="test-fixture",
    )
    return SimpleNamespace(
        config=SimpleNamespace(engine="her-v2"),
        capabilities=SimpleNamespace(
            supports_files=False,
            input_modalities=capability.input_modalities,
            output_modalities=frozenset({"text", "audio"}),
            output_formats={"audio": ("wav",)},
            output_streaming="sse",
            api_surface="chat_completions",
        ),
        resolve_input_capability=lambda: capability,
        accepts_media_input=lambda modality: modality == "audio",
        tool_registry=SimpleNamespace(is_allowed=lambda _name: False),
    )


def test_nac_001_014_voice_origin_is_semantic_and_persistent_safe(tmp_path):
    """Voice role, not an audio MIME alone, controls spoken-response routing."""

    voice_path = tmp_path / "voice.wav"
    _write_wav(voice_path)
    voice_content = _canonical_voice_content(voice_path)
    voice_part = voice_content["parts"][0]

    assert voice_part.get("semantic_role") == "voice_message"
    assert voice_part.get("duration_ms") == 10

    is_voice_origin = getattr(
        multimodal_contract,
        "request_content_is_voice_origin",
        None,
    )
    assert callable(is_voice_origin), (
        "NAC requires request_content_is_voice_origin() at the canonical boundary"
    )
    assert is_voice_origin(voice_content) is True

    attachment = dict(voice_part)
    attachment["attachment_id"] = "att-sound"
    attachment["semantic_role"] = "audio_attachment"
    generic_audio = canonical_request_content([attachment])
    assert is_voice_origin(generic_audio) is False

    persisted = json.dumps(voice_content, sort_keys=True)
    assert "data:" not in persisted
    assert "base64" not in persisted.casefold()


def test_nac_031_capability_requires_explicit_audio_output_dimensions():
    """Input audio never implies output audio, an endpoint, or a format."""

    capability = BackendCapabilities(
        supports_sessions=False,
        supports_files=False,
        supports_tool_use=True,
        supports_thinking_stream=True,
        supports_headless_mode=True,
        input_modalities=frozenset({"text", "audio"}),
        output_modalities=frozenset({"text", "audio"}),
        api_surface="chat_completions",
        input_formats={"audio": ("wav",)},
        output_formats={"audio": ("wav",)},
        supported_voices=("configured-voice",),
        output_streaming="sse",
        provider_output_transcript=True,
        function_calling=True,
    )

    assert capability.input_modalities == frozenset({"text", "audio"})
    assert capability.output_modalities == frozenset({"text", "audio"})
    assert capability.input_formats["audio"] == ("wav",)
    assert capability.output_formats["audio"] == ("wav",)
    assert capability.api_surface == "chat_completions"
    assert capability.provider_output_transcript is True


def test_nac_001_003_audio_only_session_run_is_committed_and_idempotent(tmp_path):
    """A committed attachment can form a Run without a synthetic text prompt."""

    store = SessionStore(tmp_path / "sessions.sqlite3", instance_id="HASHI1")
    owner = "user:7"
    session = store.create_session(owner_id=owner, agent_id="lily", title="Voice")
    payload = b"OggSvoice"
    staged = store.stage_attachment(
        session_id=session["session_id"],
        owner_id=owner,
        filename="voice.ogg",
        media_type="audio/ogg",
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )
    uploaded = store.upload_attachment_bytes(
        session_id=session["session_id"],
        owner_id=owner,
        attachment_id=staged["attachment_id"],
        payload=payload,
    )
    assert uploaded["asset_id"] == staged["attachment_id"]
    committed = store.commit_attachment(
        session_id=session["session_id"],
        owner_id=owner,
        attachment_id=staged["attachment_id"],
    )
    content = [
        {
            "type": "audio",
            "attachment_id": committed["attachment_id"],
            "semantic_role": "voice_message",
            "mime_type": "audio/ogg",
        }
    ]

    accepted = store.accept_run(
        session_id=session["session_id"],
        owner_id=owner,
        agent_id="lily",
        request_id="req-audio",
        text="",
        source="session-api",
        idempotency_key="audio-key",
        content=content,
    )
    replayed = store.accept_run(
        session_id=session["session_id"],
        owner_id=owner,
        agent_id="lily",
        request_id="req-audio-retry",
        text="",
        source="session-api",
        idempotency_key="audio-key",
        content=content,
    )

    assert accepted.replayed is False
    assert replayed.replayed is True
    assert replayed.run_id == accepted.run_id
    assert (
        store.messages(session["session_id"], owner_id=owner)[0]["content"] == content
    )

    other = store.create_session(owner_id=owner, agent_id="lily", title="Other")
    with pytest.raises(SessionStoreError):
        store.accept_run(
            session_id=other["session_id"],
            owner_id=owner,
            agent_id="lily",
            request_id="req-cross-session",
            text="",
            source="session-api",
            idempotency_key="cross-session-key",
            content=content,
        )


def test_nac_023_026_audio_only_model_output_is_a_completed_session_event(tmp_path):
    """A valid audio asset is visible success even when no text is available."""

    store = SessionStore(tmp_path / "sessions.sqlite3", instance_id="HASHI1")
    owner = "user:7"
    session = store.create_session(owner_id=owner, agent_id="lily", title="Output")
    accepted = store.accept_run(
        session_id=session["session_id"],
        owner_id=owner,
        agent_id="lily",
        request_id="req-output",
        text="say hello",
        source="test",
        idempotency_key="output-key",
    )
    store.mark_request_running(accepted.request_id, worker_id="test")
    audio_content = [
        {
            "type": "audio",
            "asset_id": "media-output",
            "mime_type": "audio/wav",
            "format": "wav",
            "duration_ms": 900,
            "sha256": "b" * 64,
            "retention_expires_at": "2026-08-28T02:00:00Z",
        }
    ]

    run = store.finish_request(
        accepted.request_id,
        success=True,
        assistant_text="",
        assistant_content=audio_content,
        assistant_source="configured-audio-model",
    )

    assert run is not None and run["state"] == "completed"
    assistant = store.messages(session["session_id"], owner_id=owner)[-1]
    assert assistant["role"] == "assistant"
    assert assistant["content"] == audio_content
    event_kinds = [
        event["kind"] for event in store.events(session["session_id"], owner_id=owner)
    ]
    assert "assistant.output.available" in event_kinds
    assert "run.completed" in event_kinds
    serialized = json.dumps(assistant["content"], sort_keys=True)
    assert "data:" not in serialized
    assert "base64" not in serialized.casefold()


@pytest.mark.asyncio
async def test_nac_005_018_native_voice_admission_does_not_wait_for_stt_or_safevoice(
    tmp_path,
    monkeypatch,
):
    """The original audio Turn is admitted while local STT is still pending."""

    runtime = telegram_runtime(tmp_path)
    runtime.config = SimpleNamespace(active_backend="her-v2")
    runtime.backend_manager.current_backend = _audio_capable_backend()
    runtime._safevoice_enabled = True

    class _OggFile:
        async def download_to_drive(self, local_path):
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_bytes(b"OggSvoice")

    runtime.app.bot.file = _OggFile()
    stt_started = asyncio.Event()
    release_stt = asyncio.Event()

    class _BlockingTranscriber:
        async def transcribe(self, _local_path):
            stt_started.set()
            await release_stt.wait()
            return "remember this transcript"

    monkeypatch.setattr(
        "orchestrator.voice_transcriber.get_transcriber",
        lambda: _BlockingTranscriber(),
    )
    update = telegram_update(voice=SimpleNamespace(file_id="voice-1"))
    handler = asyncio.create_task(
        runtime_media.handle_voice(runtime, update, SimpleNamespace())
    )
    try:
        await asyncio.wait_for(stt_started.wait(), timeout=1)
        await asyncio.sleep(0)

        assert len(runtime.enqueued) == 1
        request = runtime.enqueued[0]
        assert request["source"] == "voice"
        assert "Voice message transcription" not in request["prompt"]
        media = [
            part
            for part in request["request_content"]["parts"]
            if part["type"] == "media"
        ]
        assert len(media) == 1
        assert media[0]["semantic_role"] == "voice_message"
        assert runtime.replies == []
    finally:
        release_stt.set()
        await handler

    assert len(runtime.enqueued) == 1


@pytest.mark.asyncio
async def test_nac_016_stt_failure_warns_without_a_second_audio_request(
    tmp_path,
    monkeypatch,
):
    """STT failure must not resurrect the retired media_read retry path."""

    runtime = telegram_runtime(tmp_path)
    runtime.config = SimpleNamespace(active_backend="her-v2")
    runtime.backend_manager.current_backend = _audio_capable_backend()
    runtime._safevoice_enabled = False

    class _OggFile:
        async def download_to_drive(self, local_path):
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_bytes(b"OggSvoice")

    runtime.app.bot.file = _OggFile()

    class _FailedTranscriber:
        async def transcribe(self, _local_path):
            return "[Transcription error] decoder rejected input"

    monkeypatch.setattr(
        "orchestrator.voice_transcriber.get_transcriber",
        lambda: _FailedTranscriber(),
    )
    update = telegram_update(voice=SimpleNamespace(file_id="voice-1"))

    await runtime_media.handle_voice(runtime, update, SimpleNamespace())

    assert len(runtime.enqueued) == 1
    assert "media_read" not in runtime.enqueued[0]["prompt"]
    assert "normalize the audio" not in runtime.enqueued[0]["prompt"]
    assert any(
        "transcri" in reply["text"].casefold()
        and any(word in reply["text"].casefold() for word in ("failed", "unavailable"))
        for reply in runtime.replies
    )


class _AudioStreamResponse:
    def __init__(self, *, output_audio: bytes):
        split = max(1, len(output_audio) // 2)
        self._chunks = (output_audio[:split], output_audio[split:])

    def raise_for_status(self):
        return None

    async def aiter_lines(self):
        first, second = self._chunks
        yield "data: " + json.dumps(
            {
                "choices": [
                    {
                        "delta": {
                            "audio": {
                                "data": base64.b64encode(first).decode("ascii"),
                                "transcript": "Hello ",
                            }
                        },
                        "finish_reason": None,
                    }
                ]
            }
        )
        yield "data: " + json.dumps(
            {
                "choices": [
                    {
                        "delta": {
                            "audio": {
                                "data": base64.b64encode(second).decode("ascii"),
                                "transcript": "there.",
                            }
                        },
                        "finish_reason": "stop",
                    }
                ]
            }
        )
        yield "data: [DONE]"


class _AudioStreamContext:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *_args):
        return False


class _AudioStreamClient:
    is_closed = False

    def __init__(self, response):
        self.response = response
        self.payload = None

    def stream(self, _method, _url, *, json, headers):
        del headers
        self.payload = json
        return _AudioStreamContext(self.response)

    async def post(self, *_args, **_kwargs):
        raise AssertionError("native audio output must use the configured SSE path")

    async def aclose(self):
        self.is_closed = True


@pytest.mark.asyncio
async def test_nac_020_023_029_openrouter_audio_round_trip_is_typed_and_tool_free(
    tmp_path,
):
    """OpenRouter is one configured adapter, never a model-name special case."""

    input_path = tmp_path / "input.wav"
    _write_wav(input_path)
    output_path = tmp_path / "provider-output.wav"
    output_audio = _write_wav(output_path, frames=b"\x01\x00" * 120)
    config = SimpleNamespace(
        name="native-audio-contract",
        engine="openrouter-api",
        model="configured-audio-model",
        workspace_dir=tmp_path,
        system_md=None,
        extra={
            "input_modalities": ["text", "audio"],
            "input_transports": {"audio": ["inline"]},
            "output_modalities": ["text", "audio"],
            "api_surface": "chat_completions",
            "input_formats": {"audio": ["wav"]},
            "output_formats": {"audio": ["wav"]},
            "output_streaming": "sse",
            "provider_output_transcript": True,
            "native_audio_voice": "configured-voice",
            "native_audio_format": "wav",
            "audio_model_tools": False,
        },
    )
    adapter = OpenRouterAdapter(
        config,
        SimpleNamespace(
            openrouter_url="https://openrouter.invalid/v1/chat/completions",
            base_media_dir=tmp_path,
        ),
        api_key="test-key",
    )
    client = _AudioStreamClient(_AudioStreamResponse(output_audio=output_audio))
    adapter.client = client

    async def on_event(_event):
        return None

    response = await adapter.generate_response(
        "",
        "request-audio",
        request_content=_canonical_voice_content(input_path),
        on_stream_event=on_event,
    )

    assert client.payload is not None
    assert client.payload["modalities"] == ["text", "audio"]
    assert client.payload["audio"] == {
        "voice": "configured-voice",
        "format": "wav",
    }
    assert client.payload["stream"] is True
    assert "tools" not in client.payload
    user_content = client.payload["messages"][1]["content"]
    assert [part["type"] for part in user_content] == ["input_audio"]

    assert response.is_success is True
    assert response.text == "Hello there."
    assert [part["type"] for part in response.content] == ["text", "audio"]
    assert response.content[0]["provenance"] == "provider_audio_transcript"
    audio_part = response.content[1]
    assert audio_part["asset_id"]
    assert audio_part["format"] == "wav"
    assert audio_part["sha256"] == hashlib.sha256(output_audio).hexdigest()
    serialized = json.dumps(response.content, sort_keys=True)
    assert base64.b64encode(output_audio).decode("ascii") not in serialized


@pytest.mark.asyncio
async def test_nac_013_027_032_session_capability_publication_is_complete_or_absent(
    tmp_path,
    monkeypatch,
):
    """Generic terminals learn the whole contract from one capability record."""

    server, _runtime = _server(tmp_path)
    monkeypatch.setattr(workbench_api, "PERSISTENT_SESSION_V1_QUALIFIED", True)
    monkeypatch.setattr(
        workbench_api,
        "NATIVE_AUDIO_CHAT_V1_QUALIFIED",
        True,
        raising=False,
    )
    server.global_config.persistent_session_v1 = True
    server.global_config.native_audio_chat_v1 = True

    capabilities = json.loads((await server.handle_v1_capabilities(_Request())).text)

    assert capabilities["message_content_schema_version"] == "1.1"
    assert capabilities["audio_turn_schema_version"] == "1.0"
    assert capabilities["media_output_schema_version"] == "1.0"
    assert capabilities["voice_control_schema_version"] == "1.0"
    assert capabilities["audio"] == {
        "input": True,
        "output": True,
        "semantic_roles": ["voice_message", "audio_attachment"],
        "event_delivery": "ordered-at-least-once",
        "volatile_audio_delta": False,
        "retention_min_seconds": 60,
        "retention_default_seconds": 3600,
        "retention_indefinite": True,
    }
