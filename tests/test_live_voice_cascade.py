"""Focused regression and contract tests for the optional local-cascade Phone provider."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.request import urlopen

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

from orchestrator.frontend_live_voice.cascade_provider import CascadeProvider
from orchestrator.frontend_live_voice.manager import LiveVoiceManager
from orchestrator.frontend_live_voice.protocol import LiveVoiceError
from orchestrator.frontend_live_voice.provider import default_registry, select_provider
from orchestrator.frontend_live_voice import worker_actions
from orchestrator.phone_catalog import PHONE_PROVIDERS, CASCADE_VOICES
from orchestrator.session_store import SessionStore
from tools.voice_cascade_worker import VoiceCascadeWorker, CascadeSession


HASHI_COMPACTION_CAPABILITIES = {"prompt_isolation": True, "tool_disablement": True}


def test_cascade_catalog_registration():
    """Verify local-cascade is registered in the catalog with expected models and voices."""
    assert "local-cascade" in PHONE_PROVIDERS
    cat = PHONE_PROVIDERS["local-cascade"]
    assert "cascade-v1" in cat["models"]
    assert "default" in cat["models"]["cascade-v1"]["voices"]
    assert "zh_female_1" in cat["models"]["cascade-v1"]["voices"]


def test_cascade_provider_capabilities_qualification():
    """Verify CascadeProvider satisfies the full-duplex qualification thresholds."""
    provider = CascadeProvider()
    assert provider.provider_id == "local-cascade"
    caps = provider.capabilities
    assert all((
        caps.duplex_audio,
        caps.proactive_opening,
        caps.transcript,
        caps.context_updates,
        caps.task_results,
        caps.recovery,
    ))
    assert caps.playback_control == "client"
    assert caps.output_completion == "unavailable"


def test_cascade_provider_default_off_credential_rule(monkeypatch):
    """Verify local-cascade is strictly default-off unless explicitly configured."""
    for name in ("CASCADE_ENABLED", "CASCADE_WORKER_URL", "CASCADE_WORKER_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    provider = CascadeProvider()
    # 1. Default empty secrets -> empty credential (disabled)
    assert provider.credential({}) == ""
    assert provider.credential({"openai_api_key": "sk-123"}) == ""

    # 2. Enabling without a configured secret must not use a public default.
    assert provider.credential({"cascade_enabled": True}) == ""
    assert provider.credential({"cascade_enabled": True, "cascade_worker_token": "secret-tok"}) == "secret-tok"
    assert provider.credential({"cascade_enabled": "false", "cascade_worker_token": "secret-tok"}) == ""
    monkeypatch.setenv("CASCADE_ENABLED", "false")
    assert provider.credential({"cascade_worker_token": "secret-tok"}) == ""


def test_cascade_provider_selection_validation():
    """Verify strict model and voice qualification checks."""
    provider = CascadeProvider()
    # Valid choices
    provider.validate_selection("cascade-v1", "default")
    provider.validate_selection("cascade-v1", "zh_female_1")

    # Invalid model
    with pytest.raises(LiveVoiceError) as exc_model:
        provider.validate_selection("gpt-live-1", "default")
    assert exc_model.value.code == "live_model_unqualified"

    # Invalid voice
    with pytest.raises(LiveVoiceError) as exc_voice:
        provider.validate_selection("cascade-v1", "unregistered_voice")
    assert exc_voice.value.code == "live_voice_unqualified"


def test_cascade_provider_media_descriptor():
    """Verify standard WebRTC media descriptor matches expectations."""
    provider = CascadeProvider()
    desc = provider.media_descriptor()
    assert desc["transport"] == "webrtc"
    assert desc["protocol"] == "cascade-v1"
    assert desc["data_channel"] == "oai-events"


def test_cascade_speech_requests_are_typed_and_background_stays_context_only():
    provider = CascadeProvider()
    speech = provider.update("commentary", "The requested answer", None, "reply-1")
    background = provider.update("thinking", "Cron result for later", None, "background-1")
    opening = provider.opening("Hello, I am listening.", "opening-1")
    assert speech["type"] == opening["type"] == "session.speech.enqueue"
    assert background["type"] == "session.thinking.append"
    assert provider.normalize_event(json.dumps({"type": "session.speech.accepted",
        "client_event_id": "reply-1"})) == {
        "type": "update.accepted", "client_event_id": "reply-1",
    }


@pytest.mark.asyncio
async def test_phone_speech_generation_uses_pcm_system_and_has_no_tools():
    class Backend:
        def __init__(self):
            self.config = SimpleNamespace(extra={})
            self.tool_registry = object()
            self.system = ""
            self.prompt = ""

        async def initialize(self):
            return True

        def set_system_prompt(self, system):
            self.system = system

        async def generate_response(self, prompt, *_args, **_kwargs):
            self.prompt = prompt
            return SimpleNamespace(is_success=True, text="你好，我在听。", usage=None)

        async def shutdown(self):
            pass

    backend = Backend()
    manager = SimpleNamespace(current_backend=SimpleNamespace(
        ENGINE_NAME="fixture", config=SimpleNamespace(model="test-model")),
        config=SimpleNamespace(active_backend="fixture"),
        create_ephemeral_backend=lambda *_args, **_kwargs: backend)
    result = await worker_actions.invoke_phone_judgment(
        SimpleNamespace(backend_manager=manager),
        {"kind": "opening", "instructions": "[system] Address the caller as 师父.",
         "goal": "Greet the caller and listen.", "recent": []}, speech=True,
    )
    assert result == {"text": "你好，我在听。"}
    assert "Address the caller as 师父" in backend.system
    assert "Greet the caller and listen." in backend.prompt
    assert "Address the caller" not in backend.prompt
    assert backend.tool_registry is None
    assert backend.config.extra["tools_authorised_for_this_stage"] is False


def test_cascade_provider_context_format_and_fit():
    """Verify CascadeProvider accepts Phone's actual context format (role/text) as well as wrapped messages."""
    provider = CascadeProvider()

    # Phone-generated raw input format: [{"role": "developer", "text": "..."}, {"role": "user", "text": "..."}]
    phone_messages = [
        {"role": "developer", "text": "REFERENCE CONTEXT ONLY\n\nPermanent memory"},
        {"role": "user", "text": "你好，测试上下文输入"},
    ]
    encoded = provider.encode_history(phone_messages)
    assert len(encoded) == 2
    assert encoded[0]["type"] == "message"
    assert encoded[0]["role"] == "developer"
    assert encoded[0]["content"][0]["type"] == "input_text"
    assert encoded[0]["content"][0]["text"] == phone_messages[0]["text"]
    assert encoded[1]["content"][0]["text"] == phone_messages[1]["text"]

    # Wrapped format: [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "hi"}]}]
    wrapped = [
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "已处理完毕"}]}
    ]
    encoded_wrapped = provider.encode_history(wrapped)
    assert len(encoded_wrapped) == 1
    assert encoded_wrapped[0]["role"] == "assistant"
    assert encoded_wrapped[0]["content"][0]["type"] == "output_text"

    # fit_input preserves valid message count and computes exact tokens
    fitted, audit = asyncio.run(provider.fit_input(None, input_messages=phone_messages))
    assert len(fitted) == 2
    assert audit["input_tokens_exact"] > 0
    assert audit["history_included_units"] == 2
    assert audit["history_omitted_units"] == 0


def test_normalize_event_with_guaranteed_timestamps():
    """Verify event normalization guarantees complete time fields (start_ms, end_ms) and event_id."""
    provider = CascadeProvider()

    # User transcript with explicit timestamps
    raw_user = json.dumps({
        "type": "session.input_transcript.delta",
        "event_id": "evt-u1",
        "delta": "用户输入文本",
        "start_ms": 100,
        "end_ms": 800,
    })
    ev = provider.normalize_event(raw_user)
    assert ev["type"] == "conversation.user.delta"
    assert ev["event_id"] == "evt-u1"
    assert ev["delta"] == "用户输入文本"
    assert ev["start_ms"] == 100
    assert ev["end_ms"] == 800

    # User transcript missing timestamps (guaranteed fallback to valid integers)
    raw_user_missing = json.dumps({
        "type": "session.input_transcript.delta",
        "delta": "短句",
    })
    ev_fallback = provider.normalize_event(raw_user_missing)
    assert ev_fallback["type"] == "conversation.user.delta"
    assert isinstance(ev_fallback["event_id"], str) and ev_fallback["event_id"].startswith("evt_")
    assert isinstance(ev_fallback["start_ms"], int) and ev_fallback["start_ms"] >= 0
    assert isinstance(ev_fallback["end_ms"], int) and ev_fallback["end_ms"] >= ev_fallback["start_ms"]

    # Assistant transcript
    raw_assistant = json.dumps({
        "type": "session.output_transcript.delta",
        "event_id": "evt-a1",
        "delta": "助手回复文本",
        "start_ms": 900,
        "end_ms": 1500,
    })
    ev_a = provider.normalize_event(raw_assistant)
    assert ev_a["type"] == "conversation.assistant.delta"
    assert ev_a["start_ms"] == 900
    assert ev_a["end_ms"] == 1500

    # Output gate closed -> output.interrupted
    raw_gate = json.dumps({
        "type": "session.output_gate.closed",
        "generation": 3,
        "reason": "user_speech_interrupted",
    })
    ev_gate = provider.normalize_event(raw_gate)
    assert ev_gate["type"] == "output.interrupted"
    assert ev_gate["generation"] == 3
    assert ev_gate["reason"] == "user_speech_interrupted"


def test_registry_resolution_and_isolation():
    """Verify registry preserves default qualification bar and experimental registry includes local-cascade."""
    # Qualified default registry only contains production-qualified providers (openai)
    default_reg = default_registry()
    assert set(default_reg) == {"openai"}

    # Experimental registry includes local-cascade
    reg = default_registry(include_experimental=True)
    assert "openai" in reg
    assert "local-cascade" in reg

    openai_p = select_provider(reg, "openai")
    assert openai_p.provider_id == "openai"

    cascade_p = select_provider(reg, "local-cascade")
    assert cascade_p.provider_id == "local-cascade"


@pytest.mark.asyncio
async def test_worker_explicit_error_without_synthetic_fallback():
    """Verify worker returns explicit errors on invalid SDP or missing aiortc; no synthetic answer fallback."""
    worker = VoiceCascadeWorker(token="test-token")
    async with TestClient(TestServer(worker.app)) as client:
        # 1. Invalid SDP offer returns HTTP 400
        resp = await client.post(
            "/v1/sessions",
            json={"sdp": "invalid-garbage-sdp"},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status == 400
        data = await resp.json()
        assert data.get("error") in {"invalid_sdp", "sdp_negotiation_failed"}


@pytest.mark.asyncio
async def test_worker_auth_protection_on_all_endpoints():
    """Verify all mutating endpoints require authentication and return 401 when token is missing/wrong."""
    with pytest.raises(ValueError, match="CASCADE_WORKER_TOKEN"):
        VoiceCascadeWorker(token="")
    worker = VoiceCascadeWorker(token="secret-auth-token")
    async with TestClient(TestServer(worker.app)) as client:
        # Create session unauthenticated
        resp = await client.post("/v1/sessions", json={"sdp": "v=0\r\n"})
        assert resp.status == 401

        # Attach unauthenticated -> handshake rejected with 401
        with pytest.raises(aiohttp.client_exceptions.WSServerHandshakeError) as ws_err:
            await client.ws_connect("/v1/sessions/any-session/attach")
        assert ws_err.value.status == 401

        # Interrupt unauthenticated
        resp = await client.post("/v1/sessions/any-session/interrupt")
        assert resp.status == 401
        resp = await client.post("/v1/sessions/any-session/interrupt?token=secret-auth-token")
        assert resp.status == 401

        # Speech started unauthenticated
        resp = await client.post("/v1/sessions/any-session/speech_started")
        assert resp.status == 401

        # Speak unauthenticated
        resp = await client.post("/v1/sessions/any-session/speak", json={"text": "hello"})
        assert resp.status == 401

        # Delete session unauthenticated
        resp = await client.delete("/v1/sessions/any-session")
        assert resp.status == 401


@pytest.mark.asyncio
async def test_sideband_reconnect_preserves_session():
    """Verify sideband websocket reconnect preserves the underlying session and resumes event delivery."""
    worker = VoiceCascadeWorker(token="test-token")
    session = CascadeSession(
        session_id="reconnect-sess-1",
        sdp_offer="v=0\r\n",
        instructions="test instructions",
        model="cascade-v1",
        voice="default",
    )
    worker.sessions[session.session_id] = session

    async with TestClient(TestServer(worker.app)) as client:
        # 1. First connection
        ws1 = await client.ws_connect(
            f"/v1/sessions/{session.session_id}/attach",
            headers={"Authorization": "Bearer test-token"},
        )
        msg1 = await ws1.receive_json()
        assert msg1["type"] == "session.started"

        # 2. Disconnect first connection
        await ws1.close()
        # Session MUST remain alive in worker!
        assert session.session_id in worker.sessions
        assert not session.closed
        await session.simulate_user_input("断线期间保留的转写")

        # 3. Second connection (reconnect)
        ws2 = await client.ws_connect(
            f"/v1/sessions/{session.session_id}/attach",
            headers={"Authorization": "Bearer test-token"},
        )
        msg2 = await ws2.receive_json()
        assert msg2["type"] == "session.started"
        replay = await ws2.receive_json()
        assert replay["type"] == "session.input_transcript.delta"
        assert replay["delta"] == "断线期间保留的转写"

        # 4. Worker emits transcript event; ws2 receives it
        await session.simulate_user_input("重连后发送的语音识别结果")
        event = await ws2.receive_json()
        assert event["type"] == "session.input_transcript.delta"
        assert event["delta"] == "重连后发送的语音识别结果"
        assert isinstance(event["start_ms"], int)
        assert isinstance(event["end_ms"], int)
        assert event["end_ms"] >= event["start_ms"]

        await ws2.close()


@pytest.mark.asyncio
async def test_context_updates_do_not_become_spoken_output():
    worker = VoiceCascadeWorker(token="test-token")
    session = CascadeSession("private-context", "v=0", "test", "cascade-v1", "default")
    worker.sessions[session.session_id] = session
    async with TestClient(TestServer(worker.app)) as client:
        ws = await client.ws_connect(
            "/v1/sessions/private-context/attach",
            headers={"Authorization": "Bearer test-token"},
        )
        await ws.receive_json()
        for kind in ("thinking", "instructions", "commentary"):
            await ws.send_json({
                "type": f"session.{kind}.append", "event_id": f"private-{kind}",
                "content": "Private call context; do not speak it verbatim.",
            })
            ack = await ws.receive_json()
            assert ack["type"] == f"session.{kind}.appended"
        assert [item["kind"] for item in session.context_updates] == [
            "session.thinking.append", "session.instructions.append", "session.commentary.append",
        ]
        assert not session.pending_audio_chunks
        with pytest.raises(asyncio.TimeoutError):
            await ws.receive_json(timeout=0.1)
        await ws.close()


@pytest.mark.asyncio
async def test_foreground_speech_is_rejected_while_caller_is_speaking():
    class Speech:
        def ready(self):
            return True

        def synthesize_pcm48(self, _text):
            return b"\x01\x00" * 4800

    worker = VoiceCascadeWorker(token="test-token", speech_engine=Speech())
    session = CascadeSession("caller-first", "v=0", "test", "cascade-v1", "default",
                             speech_engine=worker.speech_engines["default"])
    worker.sessions[session.session_id] = session
    async with TestClient(TestServer(worker.app)) as client:
        ws = await client.ws_connect("/v1/sessions/caller-first/attach",
                                     headers={"Authorization": "Bearer test-token"})
        await ws.receive_json()
        await session.on_user_speech_started()
        assert (await ws.receive_json())["type"] == "session.output_gate.closed"
        await ws.send_json({"type": "session.speech.enqueue", "event_id": "old-answer",
                            "content": "A result that must yield to the caller"})
        rejected = await ws.receive_json()
        assert rejected["type"] == "error"
        assert rejected["client_event_id"] == "old-answer"
        assert rejected["error"]["code"] == "speech_suppressed_during_user_input"
        assert not session.pending_audio_chunks
        await session.on_user_speech_ended()
        await ws.send_json({"type": "session.speech.enqueue", "event_id": "stale-answer",
                            "generation": 0, "content": "A delayed old reply"})
        stale = await ws.receive_json()
        assert stale["type"] == "error"
        assert stale["client_event_id"] == "stale-answer"
        assert stale["error"]["code"] == "stale_speech_generation"
        assert not session.pending_audio_chunks
        await ws.close()


@pytest.mark.asyncio
async def test_barge_in_closes_audio_gate_and_purges_queue():
    """Verify user speech activity immediately closes output gate, increments generation, and clears old audio."""
    session = CascadeSession(
        session_id="barge-in-sess",
        sdp_offer="v=0\r\n",
        instructions="test instructions",
        model="cascade-v1",
        voice="default",
    )
    # 1. Enqueue assistant speech on generation 0
    await session.speak_text("正在朗读的第一段回答")
    assert session.turn_id == 1
    assert session.output_generation == 0
    assert len(session.pending_audio_chunks) > 0
    assert session.pending_audio_chunks[0][0] == 0  # Tagged with generation 0

    # 2. Trigger user speech started (barge-in)
    new_gen = await session.on_user_speech_started()
    assert new_gen == 1
    assert session.output_generation == 1
    assert session.output_gate_open is False

    # 3. All pending audio chunks must be purged immediately
    assert len(session.pending_audio_chunks) == 0
    await session.speak_text("等用户说完再回应")
    assert session.output_gate_open is False
    assert len(session.deferred_speech) == 1
    await session.on_user_speech_ended()
    assert session.output_gate_open is True
    assert len(session.deferred_speech) == 0
    await session.on_user_speech_started()
    assert session.output_generation == 2


def test_real_webrtc_bidirectional_media_in_worker_runtime():
    """Verify real WebRTC offer/answer negotiation with audio track in worker environment."""
    worker_python = Path("/home/lily/.local/share/hashi/cascade_runtime/venv/bin/python")
    if not worker_python.exists():
        pytest.skip("Cascade worker runtime environment is not provisioned on this machine")

    script = """
import asyncio, fractions
import aiortc
from aiortc import RTCPeerConnection, RTCSessionDescription, AudioStreamTrack
from av import AudioFrame

class TestTrack(AudioStreamTrack):
    kind = 'audio'
    async def recv(self):
        frame = AudioFrame(format='s16', layout='mono', samples=960)
        frame.sample_rate = 48000
        frame.pts = 0
        frame.time_base = fractions.Fraction(1, 48000)
        frame.planes[0].update(b'\\x00' * 1920)
        return frame

async def run():
    client_pc = RTCPeerConnection()
    server_pc = RTCPeerConnection()
    client_pc.addTrack(TestTrack())
    server_pc.addTrack(TestTrack())

    offer = await client_pc.createOffer()
    await client_pc.setLocalDescription(offer)
    await server_pc.setRemoteDescription(offer)

    answer = await server_pc.createAnswer()
    await server_pc.setLocalDescription(answer)
    await client_pc.setRemoteDescription(answer)

    assert 'opus' in answer.sdp
    assert server_pc.remoteDescription.type == 'offer'
    assert client_pc.remoteDescription.type == 'answer'

    await client_pc.close()
    await server_pc.close()
    print('REAL_WEBRTC_SUCCESS')

asyncio.run(run())
"""
    result = subprocess.run(
        [str(worker_python), "-c", script],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0
    assert "REAL_WEBRTC_SUCCESS" in result.stdout


def test_local_speech_models_generate_pcm_and_recognize_words():
    """The isolated models must produce audible PCM and recognize actual speech."""
    worker_python = Path("/home/lily/.local/share/hashi/cascade_runtime/venv/bin/python")
    model = Path(__file__).resolve().parents[1] / "voice_models/piper/zh_CN-huayan-medium.onnx"
    if not worker_python.exists() or not model.exists():
        pytest.skip("Cascade local speech runtime is not provisioned")
    script = """
import numpy as np
from tools.voice_cascade_speech import LocalCascadeSpeech
speech = LocalCascadeSpeech(stt_model='small', tts_model=%r, language='zh')
speech.warmup()
assert speech.ready()
pcm48 = speech.synthesize_pcm48('你好，我在听。')
assert len(pcm48) > 48000 * 2 // 2
pcm16 = np.frombuffer(pcm48, dtype='<i2')[::3].tobytes()
assert '你好' in speech.transcribe(pcm16)
print('LOCAL_SPEECH_OK')
""" % str(model)
    result = subprocess.run([str(worker_python), "-c", script],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=35)
    assert result.returncode == 0, result.stderr
    assert "LOCAL_SPEECH_OK" in result.stdout


def test_real_speech_turn_crosses_webrtc_and_sideband():
    """A generated spoken input must become a transcript and a spoken answer."""
    worker_python = Path("/home/lily/.local/share/hashi/cascade_runtime/venv/bin/python")
    model = Path(__file__).resolve().parents[1] / "voice_models/piper/zh_CN-huayan-medium.onnx"
    if not worker_python.exists() or not model.exists():
        pytest.skip("Cascade local speech runtime is not provisioned")
    script = r'''
import asyncio, fractions, json
from aiohttp.test_utils import TestClient, TestServer
from aiortc import RTCPeerConnection, RTCSessionDescription, AudioStreamTrack
from av import AudioFrame
from tools.voice_cascade_speech import LocalCascadeSpeech
from tools.voice_cascade_worker import VoiceCascadeWorker

class SpokenTrack(AudioStreamTrack):
    def __init__(self, pcm):
        super().__init__()
        self.pcm = pcm + b'\x00' * (48000 * 2)
        self.offset = 0
        self.pts = 0
        self.started = None

    async def recv(self):
        loop = asyncio.get_running_loop()
        if self.started is None:
            self.started = loop.time()
        await asyncio.sleep(max(0, self.started + self.pts / 48000 - loop.time()))
        chunk = self.pcm[self.offset:self.offset + 1920].ljust(1920, b'\x00')
        self.offset += 1920
        frame = AudioFrame(format='s16', layout='mono', samples=960)
        frame.sample_rate = 48000
        frame.pts = self.pts
        frame.time_base = fractions.Fraction(1, 48000)
        frame.planes[0].update(chunk)
        self.pts += 960
        return frame

async def run():
    speech = LocalCascadeSpeech(stt_model='small', tts_model=MODEL, language='zh')
    speech.warmup()
    input_pcm = speech.synthesize_pcm48('你好，我在听。')
    worker = VoiceCascadeWorker(token='turn-token', speech_engine=speech, speech_required=True)
    pc = RTCPeerConnection()
    pc.addTrack(SpokenTrack(input_pcm))
    channel = pc.createDataChannel('oai-events')
    remote_audio = asyncio.get_running_loop().create_future()
    @pc.on('track')
    def on_track(track):
        if track.kind == 'audio' and not remote_audio.done():
            remote_audio.set_result(track)
    async with TestClient(TestServer(worker.app)) as client:
        await pc.setLocalDescription(await pc.createOffer())
        response = await client.post('/v1/sessions',
            json={'sdp': pc.localDescription.sdp},
            headers={'Authorization': 'Bearer turn-token'})
        assert response.status == 200, await response.text()
        body = await response.json()
        ws = await client.ws_connect('/v1/sessions/' + body['provider_session_id'] + '/attach',
            headers={'Authorization': 'Bearer turn-token'})
        try:
            assert (await ws.receive_json())['type'] == 'session.started'
            await pc.setRemoteDescription(RTCSessionDescription(sdp=body['sdp_answer'], type='answer'))
            track = await asyncio.wait_for(remote_audio, 8)
            async def observe_audio():
                for _ in range(600):
                    frame = await asyncio.wait_for(track.recv(), 5)
                    if any(byte != 0 for byte in bytes(frame.planes[0])[:frame.samples * 2]):
                        return True
                return False
            heard_audio = asyncio.create_task(observe_audio())
            user_text = ''
            for _ in range(12):
                event = await ws.receive_json(timeout=20)
                if event['type'] == 'session.input_transcript.delta':
                    user_text = event['delta']
                    break
            assert '你好' in user_text, user_text
            await ws.send_json({'type': 'session.speech.enqueue', 'event_id': 'answer-1',
                'generation': worker.sessions[body['provider_session_id']].output_generation,
                'content': '收到，我在听。'})
            assert (await ws.receive_json(timeout=5))['type'] == 'session.speech.accepted'
            output_text = ''
            for _ in range(8):
                event = await ws.receive_json(timeout=10)
                if event['type'] == 'session.output_transcript.delta':
                    output_text = event['delta']
                    break
            assert output_text == '收到，我在听。'
            assert await asyncio.wait_for(heard_audio, 12)
            print('REAL_SPEECH_TURN_OK')
        finally:
            await ws.close()
            await pc.close()
            await worker.sessions[body['provider_session_id']].close()
asyncio.run(run())
'''.replace('MODEL', repr(str(model)))
    result = subprocess.run([str(worker_python), "-c", script],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=65)
    assert result.returncode == 0, result.stderr
    assert "REAL_SPEECH_TURN_OK" in result.stdout


def test_worker_webrtc_ready_silent_input_and_repeated_barge_in():
    """Exercise the actual worker's media and browser event handshake."""
    worker_python = Path("/home/lily/.local/share/hashi/cascade_runtime/venv/bin/python")
    if not worker_python.exists():
        pytest.skip("Cascade worker runtime environment is not provisioned on this machine")

    script = r'''
import asyncio, json, math, struct
from aiohttp.test_utils import TestClient, TestServer
from aiortc import RTCPeerConnection, RTCSessionDescription, AudioStreamTrack
from tools.voice_cascade_worker import VoiceCascadeWorker

class AdjustableTrack(AudioStreamTrack):
    def __init__(self):
        super().__init__()
        self.amplitude = 0

    async def recv(self):
        frame = await super().recv()
        if self.amplitude:
            samples = [int(self.amplitude * math.sin(2 * math.pi * 500 * (frame.pts + i) / 48000))
                       for i in range(frame.samples)]
            frame.planes[0].update(struct.pack('<' + 'h' * frame.samples, *samples))
        return frame

async def run():
    class StubSpeech:
        def ready(self): return True
        def transcribe(self, pcm):
            assert len(pcm) > 3200
            return 'recognized microphone audio'
        def synthesize_pcm48(self, text):
            assert text == 'spoken foreground reply'
            return b'\x10\x00' * (48000 * 3)

    worker = VoiceCascadeWorker(token='worker-test-token', speech_engine=StubSpeech(), speech_required=True)
    pc = RTCPeerConnection()
    source = AdjustableTrack()
    pc.addTrack(source)
    channel = pc.createDataChannel('oai-events')
    loop = asyncio.get_running_loop()
    ready = loop.create_future()
    remote_audio = loop.create_future()

    @channel.on('message')
    def on_message(message):
        event = json.loads(message)
        if event.get('type') == 'session.started' and not ready.done():
            ready.set_result(event)

    @pc.on('track')
    def on_track(track):
        if track.kind == 'audio' and not remote_audio.done():
            remote_audio.set_result(track)

    async with TestClient(TestServer(worker.app)) as client:
        await pc.setLocalDescription(await pc.createOffer())
        response = await client.post('/v1/sessions',
            json={'sdp': pc.localDescription.sdp},
            headers={'Authorization': 'Bearer worker-test-token'})
        assert response.status == 200, await response.text()
        body = await response.json()
        session = worker.sessions[body['provider_session_id']]
        ws = await client.ws_connect('/v1/sessions/' + session.session_id + '/attach',
            headers={'Authorization': 'Bearer worker-test-token'})
        assert (await ws.receive_json())['type'] == 'session.started'
        try:
            await pc.setRemoteDescription(RTCSessionDescription(
                sdp=body['sdp_answer'], type='answer'))
            started = await asyncio.wait_for(ready, 8)
            assert started['session']['id'] == session.session_id
            track = await asyncio.wait_for(remote_audio, 8)
            assert (await asyncio.wait_for(track.recv(), 8)).samples == 960

            await asyncio.sleep(.35)
            assert session.output_generation == 0
            assert not session.user_speaking

            source.amplitude = 5000
            await asyncio.sleep(.25)
            assert session.output_generation == 1, ('first speech', session.output_generation, session.user_speaking)
            assert session.user_speaking, ('first speech state', session.output_generation)

            source.amplitude = 0
            await asyncio.sleep(.55)
            assert not session.user_speaking, ('speech end', session.output_generation)
            found_transcript = False
            for _ in range(8):
                event = await ws.receive_json(timeout=2)
                if event['type'] == 'session.input_transcript.delta':
                    assert event['delta'] == 'recognized microphone audio'
                    assert event['final'] is True
                    found_transcript = True
                    break
            assert found_transcript

            await ws.send_json({'type': 'session.thinking.append', 'event_id': 'private-1',
                'content': 'background result: do not read aloud'})
            assert (await ws.receive_json())['type'] == 'session.thinking.appended'
            assert not session.pending_audio_chunks

            await ws.send_json({'type': 'session.speech.enqueue', 'event_id': 'reply-1',
                'generation': session.output_generation, 'content': 'spoken foreground reply'})
            assert (await ws.receive_json())['type'] == 'session.speech.accepted'
            for _ in range(30):
                if session.pending_audio_chunks:
                    break
                await asyncio.sleep(.02)
            assert session.pending_audio_chunks
            assert session.pending_audio_chunks[0][0] == 1

            source.amplitude = 5000
            await asyncio.sleep(.25)
            assert session.output_generation == 2, ('second speech', session.output_generation, session.user_speaking)
            assert not session.pending_audio_chunks
            assert not session.output_gate_open
            print('WORKER_MEDIA_AND_BARGE_IN_OK')
        finally:
            await ws.close()
            await pc.close()
            await session.close()

asyncio.run(run())
'''
    result = subprocess.run(
        [str(worker_python), "-c", script], capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "WORKER_MEDIA_AND_BARGE_IN_OK" in result.stdout


@pytest.mark.asyncio
async def test_live_voice_manager_start_to_transcript_integration():
    """Full integration test: LiveVoiceManager starts call with CascadeProvider, worker emits transcript, transcript is durably saved."""
    worker_python = Path("/home/lily/.local/share/hashi/cascade_runtime/venv/bin/python")
    if not worker_python.exists():
        pytest.skip("Cascade worker runtime is required for integration test")
    tts_model = Path(__file__).resolve().parents[1] / "voice_models/piper/zh_CN-huayan-medium.onnx"
    if not tts_model.exists():
        pytest.skip("Cascade local speech model is required for integration test")

    port = 8789
    proc = subprocess.Popen(
        [str(worker_python), "/home/lily/projects/hashi2/tools/voice_cascade_worker.py"],
        env={
            **os.environ,
            "CASCADE_WORKER_PORT": str(port),
            "CASCADE_WORKER_TOKEN": "integration-secret-token",
            "CASCADE_TTS_MODEL": str(tts_model),
        },
    )

    # Wait for worker to be ready
    health_url = f"http://127.0.0.1:{port}/health"
    ready = False
    for _ in range(150):
        try:
            with urlopen(health_url, timeout=1) as resp:
                if resp.status == 200:
                    ready = True
                    break
        except Exception:
            time.sleep(0.1)
    assert ready, "Worker process did not become healthy in time"

    old_url = os.environ.get("CASCADE_WORKER_URL")
    old_ws_url = os.environ.get("CASCADE_WORKER_WS_URL")
    os.environ["CASCADE_WORKER_URL"] = f"http://127.0.0.1:{port}"
    os.environ["CASCADE_WORKER_WS_URL"] = f"ws://127.0.0.1:{port}"

    temp_dir = tempfile.mkdtemp()
    manager = None
    try:
        db_path = Path(temp_dir) / "test_session.sqlite3"
        store = SessionStore(db_path, instance_id="HASHI")
        session_row = store.create_session(owner_id="owner-1", agent_id="agent-1", title="Cascade Phone Test")
        session_id = session_row["session_id"]

        cascade_provider = CascadeProvider()
        secrets = {
            "cascade_enabled": True,
            "cascade_worker_token": "integration-secret-token",
        }

        # Setup phone session definition
        phone_def = {
            "provider": "local-cascade",
            "model": "cascade-v1",
            "voice": "default",
            "instructions": "Speak concise Simplified Chinese.",
            "input": [
                {"role": "developer", "text": "REFERENCE CONTEXT\n\nInitial background info."},
                {"role": "user", "text": "你好，测试启动通话"},
            ],
            "public": {
                "provider": "local-cascade",
                "model": "cascade-v1",
                "voice": "default",
                "revision": "a" * 64,
                "language": "zh-CN",
            },
        }

        rendered = []
        async def render_speech(_binding, state):
            rendered.append(state)
            return {"text": "已收到。"}

        judged = []
        async def judge_action(_binding, state):
            judged.append(state)
            return {"route": "answer", "complete": True, "reply_needed": True,
                    "reply": "原始回答。", "actions": [], "result_ids": []}

        manager = LiveVoiceManager(
            store,
            SimpleNamespace(instance_id="HASHI", instance_generation="1", live_voice_v1=True),
            secrets,
            provider_registry={"local-cascade": cascade_provider},
            resolve_phone_session=lambda *_args, **_kwargs: phone_def,
            judge_action=judge_action,
            render_speech=render_speech,
            control_timeout_seconds=0.2,
            close_timeout_seconds=0.2,
            opening_grace_seconds=0.05,
        )

        context = await manager._op_context(
            "owner-1",
            {
                "session_id": session_id,
                "agent_id": "agent-1",
                "context_generation": 1,
            },
        )

        # 1. Start call via LiveVoiceManager (generates real SDP answer via aiortc)
        start_payload = {
            **context["binding"],
            "phone_revision": "a" * 64,
            "sdp": (
                "v=0\r\n"
                "o=- 3999901573 3999901573 IN IP4 0.0.0.0\r\n"
                "s=-\r\n"
                "t=0 0\r\n"
                "a=group:BUNDLE 0\r\n"
                "m=audio 9 UDP/TLS/RTP/SAVPF 96\r\n"
                "c=IN IP4 0.0.0.0\r\n"
                "a=sendrecv\r\n"
                "a=mid:0\r\n"
                "a=rtcp-mux\r\n"
                "a=rtpmap:96 opus/48000/2\r\n"
                "a=ice-ufrag:Eakh\r\n"
                "a=ice-pwd:ZyfuSxev4WEreKP4gvfRBv\r\n"
                "a=fingerprint:sha-256 C5:AD:2D:D6:1C:2C:52:F0:DC:E9:1F:7F:83:4A:68:5A:A6:33:9F:EA:73:D4:F0:BC:8B:E5:99:5E:5A:E9:CB:86\r\n"
                "a=setup:actpass\r\n"
            ),
            "idempotency_key": "start-cascade-test",
        }
        start_res = await manager._op_start("owner-1", start_payload)
        assert start_res["phone"]["provider"] == "local-cascade"
        provider_session_id = start_res["provider_session_id"]
        assert "sdp_answer" in start_res
        assert "opus" in start_res["sdp_answer"]

        call_id = start_res["binding"]["call_id"]
        bound = SimpleNamespace(owner_id="owner-1", agent_id="agent-1",
            session_id=session_id, context_generation=1, call_id=call_id,
            call_epoch=1, provider_session_id=provider_session_id)
        spoken = await manager._render_cascade_speech(bound, kind="result",
            goal="Summarize the requested result", source_context="Saved original: result details")
        assert spoken == "已收到。"
        assert rendered[-1]["instructions"] == phone_def["instructions"]
        assert rendered[-1]["source_context"] == "Saved original: result details"
        for _ in range(50):
            if manager._active_sockets.get(call_id) is not None:
                break
            await asyncio.sleep(0.05)

        # 2. Worker simulates user speech input via HTTP endpoint
        async with aiohttp.ClientSession() as http:
            async with http.post(
                f"http://127.0.0.1:{port}/v1/sessions/{provider_session_id}/simulate_speech",
                json={"text": "这是测试说话的完整语音转录"},
                headers={"Authorization": "Bearer integration-secret-token"},
            ) as resp:
                assert resp.status == 200

        # 3. Allow event processing and staging tasks to settle
        persisted = False
        for _ in range(50):
            await asyncio.sleep(0.1)
            with store._lock, store._connection() as conn:
                inbox_rows = conn.execute(
                    "SELECT text, speaker, start_ms, end_ms FROM live_provider_event_inbox WHERE call_id = ?",
                    (call_id,),
                ).fetchall()
                frag_rows = conn.execute(
                    """SELECT f.speaker, f.start_ms, f.end_ms, e.detail_json
                       FROM live_fragments AS f
                       JOIN run_events AS e ON e.event_id = f.event_id
                       WHERE f.call_id = ?""",
                    (call_id,),
                ).fetchall()
                if inbox_rows or frag_rows:
                    persisted = True
                    break
        assert persisted, "Transcript fragment was not durably persisted"

        # 4. Verify durable persistence in SQLite
        with store._lock, store._connection() as conn:
            inbox_rows = conn.execute(
                "SELECT text, speaker, start_ms, end_ms FROM live_provider_event_inbox WHERE call_id = ?",
                (call_id,),
            ).fetchall()
            frag_rows = conn.execute(
                """SELECT f.speaker, f.start_ms, f.end_ms, e.detail_json
                   FROM live_fragments AS f
                   JOIN run_events AS e ON e.event_id = f.event_id
                   WHERE f.call_id = ?""",
                (call_id,),
            ).fetchall()

            if frag_rows:
                speaker, start_ms, end_ms, detail_json = frag_rows[0]
                detail = json.loads(detail_json)
                text = detail["text"]
            else:
                text, speaker, start_ms, end_ms = inbox_rows[0]

            assert text == "这是测试说话的完整语音转录"
            assert speaker == "user"
            assert isinstance(start_ms, int) and start_ms >= 0
            assert isinstance(end_ms, int) and end_ms >= start_ms

        # A recognized utterance must reach PAO judgment and then become a
        # PCM-rendered spoken reply, with both sides durably visible.
        answered = False
        for _ in range(100):
            await asyncio.sleep(0.1)
            with store._lock, store._connection() as conn:
                answers = conn.execute(
                    """SELECT e.detail_json FROM live_fragments f
                       JOIN run_events e ON e.event_id=f.event_id
                       WHERE f.call_id=? AND f.speaker='assistant'""",
                    (call_id,),
                ).fetchall()
            if answers:
                assert json.loads(answers[-1]["detail_json"])["text"] == "已收到。"
                answered = True
                break
        assert judged and judged[-1]["utterance"] == "这是测试说话的完整语音转录"
        assert any(state["kind"] == "result" and state["goal"] == "原始回答。" for state in rendered)
        assert answered, "The judged foreground reply was not synthesized and persisted"

        # The next reply must use the new Worker generation after barge-in.
        async with aiohttp.ClientSession() as http:
            async with http.post(
                f"http://127.0.0.1:{port}/v1/sessions/{provider_session_id}/speech_started",
                headers={"Authorization": "Bearer integration-secret-token"},
            ) as resp:
                assert (await resp.json())["output_generation"] == 1
        mute = await manager._op_control("owner-1", {
            **start_res["binding"], "action": "mute", "idempotency_key": "mute-after-barge-in",
        })
        assert mute["applied"] is True
        async with aiohttp.ClientSession() as http:
            async with http.post(
                f"http://127.0.0.1:{port}/v1/sessions/{provider_session_id}/simulate_speech",
                json={"text": "第二个问题"},
                headers={"Authorization": "Bearer integration-secret-token"},
            ) as resp:
                assert resp.status == 200
        second_answered = False
        for _ in range(100):
            await asyncio.sleep(0.1)
            with store._lock, store._connection() as conn:
                count = conn.execute(
                    "SELECT COUNT(*) FROM live_fragments WHERE call_id=? AND speaker='assistant'",
                    (call_id,),
                ).fetchone()[0]
            if count >= 2:
                second_answered = True
                break
        assert len(judged) >= 2 and judged[-1]["utterance"] == "第二个问题"
        assert second_answered, "Speech after barge-in used a stale output generation"

    finally:
        if manager is not None:
            await manager.shutdown()
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
        if old_url:
            os.environ["CASCADE_WORKER_URL"] = old_url
        else:
            os.environ.pop("CASCADE_WORKER_URL", None)
        if old_ws_url:
            os.environ["CASCADE_WORKER_WS_URL"] = old_ws_url
        else:
            os.environ.pop("CASCADE_WORKER_WS_URL", None)
        shutil.rmtree(temp_dir, ignore_errors=True)
