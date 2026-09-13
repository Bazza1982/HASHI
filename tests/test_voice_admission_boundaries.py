import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer
import pytest

from orchestrator import runtime_session
from orchestrator import runtime_media, voice_transcriber
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from orchestrator.function_worker_protocol import FunctionWorkerRemoteError
from orchestrator.runtime_media import VoiceIngressError
from orchestrator.session_store import SessionConflict, SessionStore
from orchestrator.workbench_api import WorkbenchApiServer


def test_session_admission_atomically_fences_a_reset_after_the_voice_precheck(tmp_path):
    store = SessionStore(tmp_path / "sessions.sqlite3", instance_id="TEST")
    runtime = SimpleNamespace(name="voice-agent", session_store=store,
        global_config=SimpleNamespace(authorized_id=7, instance_id="TEST"))
    session = store.ensure_default_session(owner_id="user:7", agent_id=runtime.name)
    metadata = {"session_id": session["session_id"], "owner_id": "user:7",
        "session_surface": "workbench", "session_channel_key": "default",
        "session_context_generation": session["context_generation"]}
    # This is the race after the Function's optimistic precheck but before the
    # persistent admission transaction. The real Session APIs perform both steps.
    assert store.get_session(session["session_id"])["context_generation"] == metadata["session_context_generation"]
    current = store.start_fresh_generation(session["session_id"])
    request = dict(runtime=runtime, request_id="voice-stale", chat_id=7,
        prompt="transcribed voice", source="api", request_metadata=metadata,
        request_content=None, idempotency_key="recording:0")
    with pytest.raises(SessionConflict, match="session_context_generation_changed"):
        runtime_session.accept_request(**request)
    assert store.messages(session["session_id"]) == []
    assert store.recent_session_runs(session["session_id"], owner_id="user:7") == []
    metadata["session_context_generation"] = current["context_generation"]
    accepted = runtime_session.accept_request(**{**request, "request_id": "voice-current"})[1]
    replay = runtime_session.accept_request(**{**request, "request_id": "voice-repeat"})[1]
    assert replay.replayed is True
    assert accepted.request_id == replay.request_id == "voice-current"
    assert len(store.messages(session["session_id"])) == 1


@pytest.mark.asyncio
async def test_actual_multipart_route_preserves_scope_and_only_typed_pre_admission_errors_are_known_rejections(tmp_path):
    config_path = tmp_path / "agents.json"
    config_path.write_text(json.dumps({"global": {}, "agents": [{"name": "voice-agent"}]}))
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    runtime = SimpleNamespace(name="voice-agent", media_dir=media_dir, enqueue_api_media=AsyncMock())
    server = WorkbenchApiServer(config_path=config_path, runtimes=[runtime],
        global_config=SimpleNamespace(bridge_home=tmp_path, project_root=tmp_path,
            instance_id="TEST", authorized_id=7, workbench_port=0, api_gateway_port=0,
            deployment_profile="personal"), reconcile_session_runs=False)
    async with TestClient(TestServer(server.app)) as client:
        for error in [VoiceIngressError("voice_safe_confirmation_required"),
            FunctionWorkerRemoteError("runtime.enqueue_api_media", {"type": "VoiceIngressError", "message": "voice_safe_confirmation_required"})]:
            runtime.enqueue_api_media.side_effect = error
            form = FormData()
            for key, value in {"agent": runtime.name, "session_id": "canonical-session", "session_context_generation": "2",
                "media_type": "voice", "idempotency_key": "recording", "surface": "workbench", "client_id": "default"}.items():
                form.add_field(key, value)
            form.add_field("files", b"complete-recording", filename="voice.webm", content_type="audio/webm")
            response = await client.post("/api/chat", data=form)
            assert response.status == 409
            assert await response.json() == {"ok": False, "accepted": False, "error_code": "voice_safe_confirmation_required"}
            kwargs = runtime.enqueue_api_media.await_args.kwargs
            assert kwargs["media_kind"] == "voice"
            assert kwargs["filename"] == "voice.webm"
            assert kwargs["idempotency_key"] == "recording:0"
            assert kwargs["request_metadata"]["session_id"] == "canonical-session"
            assert kwargs["request_metadata"]["session_context_generation"] == 2
            assert list(media_dir.iterdir()) == []

        # A same-looking message from an unrelated exception cannot be upgraded
        # into proof of rejection after an upstream operation.
        runtime.enqueue_api_media.side_effect = RuntimeError("voice_safe_confirmation_required")
        form = FormData()
        form.add_field("agent", runtime.name)
        form.add_field("media_type", "voice")
        form.add_field("files", b"audio", filename="voice.webm", content_type="audio/webm")
        response = await client.post("/api/chat", data=form)
        assert response.status == 500
        assert response.content_type != "application/json"


@pytest.mark.asyncio
async def test_multi_file_voice_rejection_reports_prior_admission_and_keeps_normal_media_identity(tmp_path, monkeypatch):
    config_path = tmp_path / "agents.json"
    config_path.write_text(json.dumps({"global": {}, "agents": [{"name": "voice-agent"}]}))
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    config = SimpleNamespace(bridge_home=tmp_path, project_root=tmp_path,
        instance_id="TEST", authorized_id=7, workbench_port=0, api_gateway_port=0,
        deployment_profile="personal")
    runtime = SimpleNamespace(name="voice-agent", media_dir=media_dir, global_config=config,
        _should_redirect_after_transfer=lambda: False, _primary_chat_id=lambda: 7,
        _safevoice_enabled=True, _build_media_prompt=runtime_media.build_media_prompt)
    server = WorkbenchApiServer(config_path=config_path, runtimes=[runtime],
        global_config=config, reconcile_session_runs=False)
    runtime.session_store = server.session_store
    session = server.session_store.ensure_default_session(owner_id="user:7", agent_id=runtime.name)
    admitted = []

    async def enqueue_request(chat_id, prompt, source, _summary, **kwargs):
        # Keep model execution outside the test; use the real persistence owner
        # at its admission boundary so the first file is demonstrably accepted.
        accepted = runtime_session.accept_request(runtime, request_id=f"request-media-{len(admitted) + 1}",
            chat_id=chat_id, prompt=prompt, source=source, request_metadata=kwargs.get("request_metadata"),
            request_content=None, idempotency_key=kwargs.get("idempotency_key"))[1]
        admitted.append(accepted)
        return accepted.request_id

    runtime.enqueue_request = enqueue_request

    async def enqueue_media(**kwargs):
        return await FlexibleAgentRuntime.enqueue_api_media(runtime, **kwargs)

    runtime.enqueue_api_media = enqueue_media
    monkeypatch.setattr(
        voice_transcriber,
        "get_transcriber",
        lambda: SimpleNamespace(
            transcribe=AsyncMock(return_value="pending safe voice transcript")
        ),
    )
    async with TestClient(TestServer(server.app)) as client:
        multipart = FormData()
        multipart.add_field("agent", runtime.name)
        multipart.add_field("session_id", session["session_id"])
        multipart.add_field("idempotency_key", "mixed-upload")
        multipart.add_field("files", b"first document", filename="notes.txt", content_type="text/plain")
        multipart.add_field("files", b"unconfirmed voice", filename="voice.ogg", content_type="audio/ogg")
        response = await client.post("/api/chat", data=multipart)
        payload = await response.json()
        assert response.status == 409
        assert payload["ok"] is False
        assert payload["accepted"] is None
        assert payload["request_ids"] == [admitted[0].request_id]
        assert payload["error_code"] == "voice_safe_confirmation_required"
        assert len(server.session_store.messages(session["session_id"])) == 1
        assert len(list(media_dir.iterdir())) == 1

        ordinary = FormData()
        ordinary.add_field("agent", runtime.name)
        ordinary.add_field("session_id", session["session_id"])
        ordinary.add_field("idempotency_key", "ordinary-upload")
        ordinary.add_field("files", b"second document", filename="more.txt", content_type="text/plain")
        response = await client.post("/api/chat", data=ordinary)
        payload = await response.json()
        accepted = admitted[1]
        assert response.status == 200
        assert payload == {"ok": True, "request_id": accepted.request_id,
            "request_ids": [accepted.request_id], "run_id": accepted.run_id,
            "session_id": session["session_id"], "context_generation": session["context_generation"]}
        assert len(server.session_store.messages(session["session_id"])) == 2
