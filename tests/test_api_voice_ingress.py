from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from orchestrator import runtime_media, voice_transcriber
from orchestrator.session_store import SessionStore, IdempotencyConflict


def runtime(*, safe_voice=False):
    return SimpleNamespace(
        _safevoice_enabled=safe_voice,
        _should_redirect_after_transfer=lambda: False,
        _primary_chat_id=lambda: 7,
        _build_media_prompt=runtime_media.build_media_prompt,
        enqueue_request=AsyncMock(return_value="request-voice"),
    )


@pytest.mark.asyncio
async def test_basic_voice_uses_local_transcript_and_stable_digest_across_upload_paths(tmp_path, monkeypatch):
    target = runtime()
    transcriber = SimpleNamespace(transcribe=AsyncMock(return_value="This is a voice test."))
    monkeypatch.setattr(voice_transcriber, "get_transcriber", lambda: transcriber)
    files = [tmp_path / "upload-a.webm", tmp_path / "upload-b.webm"]
    for file in files:
        file.write_bytes(b"same-recorded-audio")
        result = await FlexibleAgentRuntime.enqueue_api_media(
            target, file, "voice", "voice.webm", idempotency_key="same-recording:0",
            request_metadata={"session_id": "session-voice"},
        )
        assert result == "request-voice"
    assert transcriber.transcribe.await_count == 2
    first, second = target.enqueue_request.await_args_list
    assert first.args[1] == "[Voice message transcription] This is a voice test."
    assert second.args[1] == first.args[1]
    assert first.kwargs["request_metadata"]["session_id"] == "session-voice"
    assert first.kwargs["request_metadata"]["session_message_content"] == second.kwargs["request_metadata"]["session_message_content"]
    assert first.kwargs["idempotency_key"] == second.kwargs["idempotency_key"] == "same-recording:0"
    assert str(tmp_path) not in first.args[1]

    # Exercise the actual persisted admission contract with the emitted content,
    # rather than assuming that merely forwarding a key makes media idempotent.
    store = SessionStore(tmp_path / "sessions.sqlite3", instance_id="TEST")
    session = store.create_session(owner_id="owner", agent_id="voice-agent", title="Voice")
    common = dict(session_id=session["session_id"], owner_id="owner", agent_id="voice-agent", source="api", idempotency_key="same-recording:0", text=first.args[1])
    accepted = store.accept_run(**common, request_id="req-first", content=first.kwargs["request_metadata"]["session_message_content"])
    replayed = store.accept_run(**common, request_id="req-second", content=second.kwargs["request_metadata"]["session_message_content"])
    assert replayed.replayed is True
    assert replayed.request_id == accepted.request_id
    files[1].write_bytes(b"different-recorded-audio")
    await FlexibleAgentRuntime.enqueue_api_media(target, files[1], "voice", "voice.webm", idempotency_key="same-recording:0")
    different = target.enqueue_request.await_args.kwargs["request_metadata"]["session_message_content"]
    with pytest.raises(IdempotencyConflict):
        store.accept_run(**common, request_id="req-third", content=different)


@pytest.mark.asyncio
async def test_basic_voice_respects_safe_confirmation_without_dispatching_or_transcribing(tmp_path, monkeypatch):
    target = runtime(safe_voice=True)
    transcriber = SimpleNamespace(transcribe=AsyncMock(return_value="not authorized"))
    monkeypatch.setattr(voice_transcriber, "get_transcriber", lambda: transcriber)
    audio = tmp_path / "voice.m4a"
    audio.write_bytes(b"audio")
    with pytest.raises(ValueError, match="voice_safe_confirmation_required"):
        await FlexibleAgentRuntime.enqueue_api_media(target, audio, "voice", "voice.m4a")
    transcriber.transcribe.assert_not_awaited()
    target.enqueue_request.assert_not_awaited()
    assert audio.exists()


@pytest.mark.asyncio
async def test_transcription_failure_empty_and_permission_change_do_not_become_model_prompts(tmp_path, monkeypatch):
    audio = tmp_path / "voice.ogg"
    audio.write_bytes(b"audio")
    for result, code in [("[Transcription error] decoder failure", "voice_transcription_unavailable"), ("  ", "voice_transcription_empty"), ("late safe voice", "voice_safe_confirmation_required")]:
        target = runtime()

        async def transcribe(_path):
            if result == "late safe voice":
                target._safevoice_enabled = True
            return result

        monkeypatch.setattr(voice_transcriber, "get_transcriber", lambda: SimpleNamespace(transcribe=transcribe))
        with pytest.raises(ValueError, match=code):
            await FlexibleAgentRuntime.enqueue_api_media(target, audio, "voice", "voice.ogg")
        target.enqueue_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_session_reset_during_transcription_cannot_enqueue_into_new_generation(tmp_path, monkeypatch):
    target = runtime()
    target.name = "voice-agent"
    target.global_config = SimpleNamespace(authorized_id=7, instance_id="TEST")
    target.session_store = SessionStore(tmp_path / "sessions.sqlite3", instance_id="TEST")
    session = target.session_store.create_session(owner_id="owner", agent_id=target.name, title="Voice")
    audio = tmp_path / "voice.webm"
    audio.write_bytes(b"audio")

    async def transcribe(_path):
        target.session_store.start_fresh_generation(session["session_id"])
        return "Old conversation voice."

    monkeypatch.setattr(voice_transcriber, "get_transcriber", lambda: SimpleNamespace(transcribe=transcribe))
    with pytest.raises(ValueError, match="voice_session_changed"):
        await FlexibleAgentRuntime.enqueue_api_media(
            target, audio, "voice", "voice.webm",
            request_metadata={"session_id": session["session_id"], "owner_id": "owner", "session_context_generation": session["context_generation"]},
        )
    target.enqueue_request.assert_not_awaited()
