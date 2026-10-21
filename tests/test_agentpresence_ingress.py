from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer
import pytest

from orchestrator import agent_presence_notify, agent_presence_settings
from orchestrator.frontend_compatibility import normalize_compatibility_command
from orchestrator.message_context import (
    AGENTPRESENCE_AUTHENTICATED_METADATA_KEY,
    MESSAGE_SOURCE_RESERVED_METADATA_KEY,
    build_message_context_snapshot,
)
from orchestrator.runtime_media import VoiceIngressError
from orchestrator.workbench_api import WorkbenchApiServer


def _server(tmp_path, *, default_agent="agent1"):
    config_path = tmp_path / "agents.json"
    config_path.write_text(
        json.dumps({"global": {"instance_id": "TEST"}, "agents": [{"name": "agent1"}]}),
        encoding="utf-8",
    )
    if default_agent:
        agent_presence_settings.set_default_agent(tmp_path, default_agent)
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    runtime = SimpleNamespace(
        name="agent1",
        media_dir=media_dir,
        enqueue_api_text=AsyncMock(return_value="req-ap-text"),
        enqueue_api_media=AsyncMock(return_value="req-ap-voice"),
    )
    global_config = SimpleNamespace(
        bridge_home=tmp_path,
        project_root=tmp_path,
        instance_id="TEST",
        authorized_id=7,
        workbench_port=0,
        api_gateway_port=0,
        deployment_profile="personal",
    )
    server = WorkbenchApiServer(
        config_path=config_path,
        runtimes=[runtime],
        global_config=global_config,
        reconcile_session_runs=False,
    )
    return server, runtime


def test_authenticated_agentpresence_source_is_typed_in_message_context():
    runtime = SimpleNamespace(
        name="agent1",
        global_config=SimpleNamespace(instance_id="HASHI3", bridge_home=None),
    )
    snapshot = build_message_context_snapshot(
        runtime,
        source="agentpresence",
        chat_id="default",
        prompt="ignored for authority",
        metadata={
            MESSAGE_SOURCE_RESERVED_METADATA_KEY: "agentpresence",
            AGENTPRESENCE_AUTHENTICATED_METADATA_KEY: True,
        },
    )
    assert snapshot["message_source"]["id"] == "agentpresence"
    assert snapshot["network_authentication"] == "local_shared_token"


def test_agentpresence_slash_text_uses_registered_command_adapter():
    normalized = normalize_compatibility_command(
        "/agentpresence",
        source_channel="api_chat",
        session_metadata={"session_surface": "agentpresence"},
    )
    assert normalized["connector_id"] == "agentpresence"
    assert normalized["adapter"]["id"] == "agentpresence.command"
    assert normalized["operation"]["kind"] == "command"


@pytest.mark.asyncio
async def test_agentpresence_reserved_source_requires_token_and_exact_instance(
    tmp_path, monkeypatch
):
    server, runtime = _server(tmp_path)
    monkeypatch.setattr(
        agent_presence_notify, "token_matches", lambda value: value == "ap-secret"
    )
    base = {
        "text": "hello",
        "source": "agentpresence",
        "surface": "agentpresence",
        "instance_id": "TEST",
    }
    async with TestClient(TestServer(server.app)) as client:
        response = await client.post("/api/chat", json=base)
        assert response.status == 401
        assert (await response.json())["error_code"] == "agentpresence_auth_required"

        response = await client.post(
            "/api/chat",
            json={**base, "instance_id": "OTHER"},
            headers={"X-AP-Token": "ap-secret"},
        )
        assert response.status == 409
        assert (await response.json())["error_code"] == "agentpresence_instance_mismatch"

        response = await client.post(
            "/api/chat",
            json={**base, "surface": "workbench"},
            headers={"X-AP-Token": "ap-secret"},
        )
        assert response.status == 400
        assert (await response.json())["error_code"] == "agentpresence_source_mismatch"

    runtime.enqueue_api_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_agentpresence_text_and_voice_use_one_isolated_default_session_route(
    tmp_path, monkeypatch
):
    server, runtime = _server(tmp_path)
    monkeypatch.setattr(
        agent_presence_notify, "token_matches", lambda value: value == "ap-secret"
    )
    headers = {"X-AP-Token": "ap-secret"}
    async with TestClient(TestServer(server.app)) as client:
        response = await client.post(
            "/api/chat",
            json={
                "agent": "caller-cannot-select-agent",
                "text": "hello from AP",
                "source": "agentpresence",
                "surface": "agentpresence",
                "client_id": "attempted-other-channel",
                "session_id": "attempted-other-session",
                "instance_id": "TEST",
                "idempotency_key": "agentpresence:text-1",
            },
            headers=headers,
        )
        assert response.status == 200
        assert (await response.json())["request_id"] == "req-ap-text"
        text_kwargs = runtime.enqueue_api_text.await_args.kwargs
        assert text_kwargs["source"] == "agentpresence"
        assert text_kwargs["idempotency_key"] == "agentpresence:text-1"
        assert text_kwargs["request_metadata"]["session_id"] is None
        assert text_kwargs["request_metadata"]["session_surface"] == "agentpresence"
        assert text_kwargs["request_metadata"]["session_channel_key"] == "default"
        assert (
            text_kwargs["request_metadata"][MESSAGE_SOURCE_RESERVED_METADATA_KEY]
            == "agentpresence"
        )
        assert (
            text_kwargs["request_metadata"][AGENTPRESENCE_AUTHENTICATED_METADATA_KEY]
            is True
        )

        form = FormData()
        for key, value in {
            "agent": "caller-cannot-select-agent",
            "source": "agentpresence",
            "surface": "agentpresence",
            "client_id": "attempted-other-channel",
            "session_id": "attempted-other-session",
            "instance_id": "TEST",
            "media_type": "voice",
            "idempotency_key": "agentpresence:voice-1",
        }.items():
            form.add_field(key, value)
        form.add_field(
            "files", b"RIFF-valid-enough-for-route-test", filename="voice.wav", content_type="audio/wav"
        )
        response = await client.post("/api/chat", data=form, headers=headers)
        assert response.status == 200
        assert (await response.json())["request_id"] == "req-ap-voice"
        voice_kwargs = runtime.enqueue_api_media.await_args.kwargs
        assert voice_kwargs["media_kind"] == "voice"
        assert voice_kwargs["source"] == "agentpresence"
        assert voice_kwargs["idempotency_key"] == "agentpresence:voice-1:0"
        assert voice_kwargs["request_metadata"]["session_id"] is None
        assert voice_kwargs["request_metadata"]["session_surface"] == "agentpresence"
        assert voice_kwargs["request_metadata"]["session_channel_key"] == "default"
        assert (
            voice_kwargs["request_metadata"][MESSAGE_SOURCE_RESERVED_METADATA_KEY]
            == "agentpresence"
        )
        assert (
            voice_kwargs["request_metadata"][AGENTPRESENCE_AUTHENTICATED_METADATA_KEY]
            is True
        )


@pytest.mark.asyncio
async def test_agentpresence_safe_voice_returns_transcript_and_accepts_explicit_decision(
    tmp_path, monkeypatch
):
    server, runtime = _server(tmp_path)
    runtime.is_function_worker_proxy = True
    runtime.enqueue_api_media.side_effect = VoiceIngressError(
        "voice_safe_confirmation_required"
    )
    runtime.execute_slash_command = AsyncMock(
        side_effect=[
            {
                "ok": True,
                "voice_confirmation_version": 1,
                "confirmation": {
                    "version": 1,
                    "pending_id": "voice-ap-pending",
                    "state": "pending_confirmation",
                    "transcript": "review this transcript",
                    "session_id": "ses-ap",
                    "context_generation": 1,
                    "expires_at": "2026-10-21T06:00:00Z",
                },
            },
            {
                "ok": True,
                "voice_confirmation_version": 1,
                "state": "confirmed",
                "request_id": "req-ap-confirmed",
                "session_id": "ses-ap",
                "context_generation": 1,
            },
        ]
    )
    monkeypatch.setattr(
        agent_presence_notify, "token_matches", lambda value: value == "ap-secret"
    )
    headers = {"X-AP-Token": "ap-secret"}
    form = FormData()
    for key, value in {
        "source": "agentpresence",
        "surface": "agentpresence",
        "instance_id": "TEST",
        "media_type": "voice",
        "idempotency_key": "agentpresence:voice-safe",
    }.items():
        form.add_field(key, value)
    form.add_field(
        "files",
        b"RIFF-safe-voice-route-test",
        filename="voice.wav",
        content_type="audio/wav",
    )

    async with TestClient(TestServer(server.app)) as client:
        response = await client.post("/api/chat", data=form, headers=headers)
        assert response.status == 200
        pending = await response.json()
        assert pending["ok"] is True
        assert pending["accepted"] is False
        assert pending["confirmation_required"] is True
        assert pending["voice_confirmation"]["transcript"] == "review this transcript"

        response = await client.post(
            "/api/agentpresence/voice-decision",
            json={
                "source": "agentpresence",
                "surface": "agentpresence",
                "instance_id": "TEST",
                "pending_id": "voice-ap-pending",
                "decision": "confirm",
            },
            headers={"X-AP-Token": "wrong"},
        )
        assert response.status == 401
        assert (await response.json())["error_code"] == "agentpresence_auth_required"

        response = await client.post(
            "/api/agentpresence/voice-decision",
            json={
                "source": "agentpresence",
                "surface": "agentpresence",
                "instance_id": "TEST",
                "pending_id": "voice-ap-pending",
                "decision": "confirm",
                "agent": "caller-cannot-select-agent",
            },
            headers=headers,
        )
        assert response.status == 200
        confirmed = await response.json()
        assert confirmed["ok"] is True
        assert confirmed["accepted"] is True
        assert confirmed["request_id"] == "req-ap-confirmed"

    assert runtime.execute_slash_command.await_count == 2
    for call in runtime.execute_slash_command.await_args_list:
        assert call.kwargs["source_channel"] == "agentpresence_api"
        assert call.args[0].startswith("__hashi_voice_confirmation_v1__:")


@pytest.mark.asyncio
async def test_agentpresence_ingress_fails_closed_without_hashi_default_agent(tmp_path, monkeypatch):
    server, runtime = _server(tmp_path, default_agent="")
    monkeypatch.setattr(
        agent_presence_notify, "token_matches", lambda value: value == "ap-secret"
    )
    async with TestClient(TestServer(server.app)) as client:
        response = await client.post(
            "/api/chat",
            json={
                "text": "must not guess",
                "source": "agentpresence",
                "surface": "agentpresence",
                "instance_id": "TEST",
            },
            headers={"X-AP-Token": "ap-secret"},
        )
        assert response.status == 409
        assert (await response.json())["error_code"] == "agentpresence_agent_unconfigured"
    runtime.enqueue_api_text.assert_not_awaited()
