"""Workbench Safe Voice holds transcripts until an explicit Worker decision."""
from __future__ import annotations

import base64
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from orchestrator.admin_local_testing import (
    execute_local_command,
    try_execute_slash_command_text,
)
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from orchestrator import runtime_media, runtime_session, voice_transcriber
from orchestrator.session_store import SessionStore
from orchestrator.voice_confirmation_transport import TRANSPORT_PREFIX


def _wire(**payload):
    raw = json.dumps({"version": 1, **payload}, separators=(",", ":")).encode()
    return TRANSPORT_PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _runtime(tmp_path, *, now=1000.0):
    clock = [now]
    target = NS(
        name="nana",
        workspace_dir=tmp_path,
        global_config=NS(
            authorized_id=7,
            instance_id="fixture",
            deployment_profile="personal",
        ),
        session_store=SessionStore(tmp_path / "sessions.sqlite", instance_id="fixture"),
        _safevoice_enabled=True,
        _voice_confirmation_clock=lambda: clock[0],
        _should_redirect_after_transfer=lambda: False,
        _primary_chat_id=lambda: 7,
        _is_authorized_user=lambda actor: actor == 7,
        enqueue_request=AsyncMock(return_value="request-confirmed"),
    )
    session = runtime_session.current_session(
        target, surface="workbench", channel_key="default"
    )
    metadata = {
        "session_id": session["session_id"],
        "owner_id": "user:7",
        "session_surface": "workbench",
        "session_channel_key": "default",
        "session_context_generation": session["context_generation"],
    }
    return target, session, metadata, clock


async def _upload(tmp_path, monkeypatch, target, metadata, *, key, transcript="check this text"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    audio = tmp_path / f"{key.replace(':', '-')}.webm"
    audio.write_bytes(b"complete audio bytes")
    transcriber = NS(transcribe=AsyncMock(return_value=transcript))
    monkeypatch.setattr(voice_transcriber, "get_transcriber", lambda: transcriber)
    with pytest.raises(runtime_media.VoiceIngressError, match="voice_safe_confirmation_required"):
        await FlexibleAgentRuntime.enqueue_api_media(
            target,
            audio,
            "voice",
            "voice.webm",
            request_metadata=metadata,
            idempotency_key=key,
        )
    return transcriber


@pytest.mark.asyncio
async def test_safe_voice_transcribes_but_admits_only_after_confirm(tmp_path, monkeypatch):
    target, session, metadata, _clock = _runtime(tmp_path)
    transcriber = await _upload(
        tmp_path, monkeypatch, target, metadata, key="recording-one:0"
    )
    transcriber.transcribe.assert_awaited_once()
    target.enqueue_request.assert_not_awaited()

    read = await try_execute_slash_command_text(
        target,
        _wire(op="read", idempotency_key="recording-one:0"),
        source_channel="workbench_api",
        session_metadata={"session_id": "forged"},
    )
    assert read["ok"] is True
    confirmation = read["confirmation"]
    assert confirmation == {
        "version": 1,
        "pending_id": confirmation["pending_id"],
        "state": "pending_confirmation",
        "transcript": "check this text",
        "session_id": session["session_id"],
        "context_generation": session["context_generation"],
        "expires_at": "1970-01-01T00:26:40Z",
    }
    decision = _wire(
        op="decide", pending_id=confirmation["pending_id"], decision="confirm"
    )
    confirmed = await execute_local_command(
        target, decision, source_channel="workbench_api"
    )
    replayed = await execute_local_command(
        target, decision, source_channel="workbench_api"
    )
    assert confirmed["request_id"] == replayed["request_id"] == "request-confirmed"
    target.enqueue_request.assert_awaited_once()
    call = target.enqueue_request.await_args
    assert call.args[1] == "[Voice message transcription] check this text"
    assert call.kwargs["request_metadata"]["session_id"] == session["session_id"]
    assert call.kwargs["idempotency_key"] == "recording-one:0"


@pytest.mark.asyncio
async def test_discard_expiry_scope_change_and_safevoice_off_never_admit(tmp_path, monkeypatch):
    for case in ("discard", "expired", "scope", "safevoice_off"):
        target, session, metadata, clock = _runtime(tmp_path / case)
        await _upload(
            tmp_path / case,
            monkeypatch,
            target,
            metadata,
            key=f"recording-{case}:0",
        )
        read = await try_execute_slash_command_text(
            target,
            _wire(op="read", idempotency_key=f"recording-{case}:0"),
            source_channel="workbench_api",
        )
        pending_id = read["confirmation"]["pending_id"]
        if case == "discard":
            result = await execute_local_command(
                target,
                _wire(op="decide", pending_id=pending_id, decision="discard"),
                source_channel="workbench_api",
            )
            assert result["state"] == "discarded"
        elif case == "expired":
            clock[0] += 601
            result = await execute_local_command(
                target,
                _wire(op="decide", pending_id=pending_id, decision="confirm"),
                source_channel="workbench_api",
            )
            assert result["error_code"] == "voice_confirmation_expired"
        elif case == "scope":
            target.session_store.start_fresh_generation(session["session_id"])
            result = await execute_local_command(
                target,
                _wire(op="decide", pending_id=pending_id, decision="confirm"),
                source_channel="workbench_api",
            )
            assert result["error_code"] == "voice_session_changed"
        else:
            runtime_media.disable_safe_voice(target)
            result = await execute_local_command(
                target,
                _wire(op="decide", pending_id=pending_id, decision="confirm"),
                source_channel="workbench_api",
            )
            assert result["error_code"] == "voice_confirmation_discarded"
        target.enqueue_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_transport_is_terminal_for_invalid_or_unauthorized_envelopes(tmp_path):
    target, _session, _metadata, _clock = _runtime(tmp_path)
    invalid = await try_execute_slash_command_text(
        target, TRANSPORT_PREFIX + "bad!", source_channel="workbench_api"
    )
    assert invalid == {
        "ok": False,
        "voice_confirmation_version": 1,
        "error_code": "voice_confirmation_request_invalid",
        "http_status": 400,
    }
    forbidden = await try_execute_slash_command_text(
        target,
        _wire(op="read", idempotency_key="recording-forbidden:0"),
        source_channel="telegram",
    )
    assert forbidden["error_code"] == "voice_confirmation_forbidden"
    assert await try_execute_slash_command_text(
        target, "ordinary text", source_channel="workbench_api"
    ) is None
    target.enqueue_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirmation_admission_exception_and_missing_receipt_are_uncertain(tmp_path, monkeypatch):
    target, _session, metadata, _clock = _runtime(tmp_path)
    target.enqueue_request.side_effect = RuntimeError("after admission is unknowable")
    await _upload(
        tmp_path, monkeypatch, target, metadata, key="recording-uncertain:0"
    )
    read = await execute_local_command(
        target,
        _wire(op="read", idempotency_key="recording-uncertain:0"),
        source_channel="workbench_api",
    )
    result = await execute_local_command(
        target,
        _wire(
            op="decide",
            pending_id=read["confirmation"]["pending_id"],
            decision="confirm",
        ),
        source_channel="workbench_api",
    )
    assert result["error_code"] == "voice_confirmation_outcome_unknown"
    assert result["accepted"] is None
    assert "after admission is unknowable" not in json.dumps(result)

    missing = await execute_local_command(
        target,
        _wire(op="decide", pending_id="voice-missing", decision="confirm"),
        source_channel="workbench_api",
    )
    assert missing["error_code"] == "voice_confirmation_not_found"
    assert missing["accepted"] is None
