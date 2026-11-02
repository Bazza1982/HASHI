from __future__ import annotations

import asyncio
import json
import multiprocessing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

from orchestrator.function_worker_host import FunctionWorkerHost
from orchestrator.function_worker_protocol import JsonConnectionPeer
from orchestrator.function_worker_supervisor import AgentRuntimeHandle, FunctionWorkerError
from orchestrator.runtime_media import VoiceIngressError
from orchestrator.voice_transcription_result import transcription_failure
from orchestrator.workbench_api import WorkbenchApiServer
from tests.test_session_attachment_authorization import Consumer, CodexConsumer, HERConsumer, fixture


@pytest.mark.asyncio
@pytest.mark.parametrize("backend_class", [Consumer, CodexConsumer, HERConsumer])
async def test_transcription_failure_reaches_every_engine_boundary_without_a_model_action(tmp_path, backend_class):
    mgr, backend, store, session, run, _, content = fixture(tmp_path, codex=True, backend_class=backend_class)
    mgr.config.active_backend = "codex-cli"
    failure = transcription_failure(ImportError("leak_token_Example", name="faster_whisper"), phase="model")
    attachment_id = content["parts"][0]["attachment_id"]
    store.record_voice_transcript(request_id=run["request_id"], attachment_id=attachment_id,
        text="", provenance="local_stt", safe_voice_state="unavailable", failure=failure.as_dict())
    state = {"request_id":run["request_id"], "attachment_id":attachment_id, "attachment_ids":[attachment_id],
        "safe_voice":True, "status":"unavailable", "text":"", "failure":failure.as_dict(),
        "ready_event":asyncio.Event(), "release_event":asyncio.Event(), "gate_lock":asyncio.Lock()}
    state["ready_event"].set()
    state["release_event"].set()
    mgr.runtime._native_voice_transcripts = {run["request_id"]:state, attachment_id:state}
    response = await mgr.generate_response("synthetic", run["request_id"], request_content=content)
    assert not response.is_success and not hasattr(backend, "seen_prompt")
    assert failure.code in response.error and failure.reference in response.error
    assert "faster_whisper" in response.error and "leak_token_Example" not in response.error
    diagnostic = response.stream_metadata["media_diagnostic"]
    assert diagnostic["reason"] == failure.code and diagnostic["diagnostic_ref"] == failure.reference
    events = store.events(session["session_id"], owner_id=session["owner_id"])
    saved = json.dumps(events)
    assert failure.code in saved and failure.reference in saved and "leak_token_Example" not in saved
    warning = next(event for event in events if event["kind"] == "voice.warning")
    assert "Transcript-dependent actions cannot start" in warning["summary"]
    assert "audio response will continue" not in warning["summary"]


@pytest.mark.asyncio
async def test_typed_basic_voice_rejection_survives_the_real_unchanged_json_rpc():
    failure = transcription_failure(ImportError(name="av"), phase="model")
    runtime = SimpleNamespace(enqueue_api_media=AsyncMock(side_effect=VoiceIngressError(
        "voice_transcription_unavailable", failure=failure.as_dict())))
    host = object.__new__(FunctionWorkerHost)
    host.phase, host.accepting = "ACTIVE", True
    host._require_runtime = lambda: runtime
    host.emit_metadata = AsyncMock()
    left_connection, right_connection = multiprocessing.Pipe(duplex=True)
    left = JsonConnectionPeer(left_connection, label="test-caller")
    right = JsonConnectionPeer(right_connection, label="test-worker", request_handler=host.handle_request)
    left.start()
    right.start()
    handle = object.__new__(AgentRuntimeHandle)
    handle._route = left.request
    handle._outstanding_request_ids = set()
    try:
        with pytest.raises(VoiceIngressError) as caught:
            await asyncio.wait_for(handle.enqueue_api_media(Path("input.wav"), "voice", "input.wav"), 5)
        assert str(caught.value) == "voice_transcription_unavailable"
        assert caught.value.failure == failure.as_dict()
        assert handle._outstanding_request_ids == set()
        assert runtime.enqueue_api_media.await_count == 1
        host.emit_metadata.assert_not_awaited()
    finally:
        await asyncio.gather(left.close(), right.close())


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [True, 2])
async def test_malformed_typed_rejection_does_not_become_a_request_or_get_replayed(version):
    failure = transcription_failure(TimeoutError(), phase="external")
    handle = object.__new__(AgentRuntimeHandle)
    handle._outstanding_request_ids = set()
    handle._route = AsyncMock(return_value={"type":"hashi.voice-admission-rejection", "version":version,
        "code":"voice_transcription_unavailable", "failure":failure.as_dict()})
    with pytest.raises(FunctionWorkerError):
        await handle.enqueue_api_media(Path("input.wav"), "voice", "input.wav")
    assert handle._outstanding_request_ids == set() and handle._route.await_count == 1


@pytest.mark.asyncio
async def test_actual_multipart_voice_failure_exposes_safe_cause_and_no_admission(tmp_path):
    config_path = tmp_path / "agents.json"
    config_path.write_text(json.dumps({"global":{}, "agents":[{"name":"voice-agent"}]}))
    media = tmp_path / "media"
    media.mkdir()
    config = SimpleNamespace(bridge_home=tmp_path, project_root=tmp_path, instance_id="TEST",
        authorized_id=7, workbench_port=0, api_gateway_port=0, deployment_profile="personal", ui_language="zh-CN")
    failure = transcription_failure(ImportError("leak_token_Example", name="av"), phase="model")
    runtime = SimpleNamespace(name="voice-agent", media_dir=media, global_config=config,
        enqueue_api_media=AsyncMock(side_effect=VoiceIngressError(
            "voice_transcription_unavailable", failure=failure.as_dict())))
    server = WorkbenchApiServer(config_path=config_path, runtimes=[runtime], global_config=config,
                                reconcile_session_runs=False)
    async with TestClient(TestServer(server.app)) as client:
        form = FormData()
        form.add_field("agent", runtime.name)
        form.add_field("media_type", "voice")
        form.add_field("files", b"recording", filename="voice.webm", content_type="audio/webm")
        response = await client.post("/api/chat", data=form)
        assert response.status == 503
        body = await response.json()
        assert body["accepted"] is False and body["error_code"] == "voice_transcription_unavailable"
        assert body["transcription_failure"] == failure.as_dict()
        assert body["cause_code"] == failure.code and body["diagnostic_ref"] == failure.reference
        assert "尚未提交" in body["message"] and "处理组件不可用" in body["message"]
        assert "leak_token_Example" not in json.dumps(body)
        assert runtime.enqueue_api_media.await_count == 1
        assert not any(path.is_file() for path in media.rglob("*"))


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["telegram", "whatsapp", "whatsapp-reclassified"])
@pytest.mark.parametrize("empty", [False, True])
async def test_every_messaging_voice_path_keeps_the_same_failure_reference(tmp_path, monkeypatch, surface, empty):
    from orchestrator import runtime_media, voice_transcriber
    from orchestrator.voice_transcription_result import TranscriptionResult
    from tests.test_runtime_media import _runtime, _update
    from transports.whatsapp import WhatsAppTransport

    outcome = (TranscriptionResult() if empty else TranscriptionResult(
        failure=transcription_failure(ImportError("leak_token_Example", name="av"), phase="model")))
    transcriber = SimpleNamespace(transcribe_result=AsyncMock(return_value=outcome))
    monkeypatch.setattr(voice_transcriber, "get_transcriber", lambda: transcriber)
    if surface == "telegram":
        runtime = _runtime(tmp_path)
        runtime.global_config = SimpleNamespace(ui_language="zh-CN")
        await runtime_media.handle_voice_or_audio(runtime, _update(), "voice", "voice.ogg", "file-id")
        assert runtime.enqueued == [] and runtime._pending_voice == {}
        text = runtime.replies[-1]["text"]
    else:
        transport = WhatsAppTransport.__new__(WhatsAppTransport)
        transport.global_cfg = SimpleNamespace(ui_language="zh-CN")
        transport._media_dir = tmp_path
        transport._send_text = AsyncMock()
        if surface == "whatsapp":
            transport._client = SimpleNamespace(download_any=AsyncMock(return_value=b"OggSrecording"))
            result = await transport._handle_voice(SimpleNamespace(Message=object()), "chat")
            assert result is None and transport._client.download_any.await_count == 1
        else:
            result = await transport._reclassify_as_voice(b"OggSrecording", "chat")
            assert result == (None, None)
        assert transport._send_text.await_count == 1
        text = transport._send_text.await_args.args[1]
    assert outcome.failure.reference in text and "尚未提交" in text
    assert ("没有产生转写文字" if empty else "处理组件不可用") in text
    assert "leak_token_Example" not in text
    assert transcriber.transcribe_result.await_count == 1
