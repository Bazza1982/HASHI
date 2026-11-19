import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from orchestrator import runtime_media
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from tests.test_runtime_media import _runtime, _update


def _callback(data, *, actor=1, chat=123, thread=None):
    message = SimpleNamespace(message_thread_id=thread)
    query = SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=actor),
        message=message,
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    return SimpleNamespace(
        callback_query=query,
        effective_user=query.from_user,
        effective_chat=SimpleNamespace(id=chat, type="supergroup"),
        effective_message=message,
    )


def _button(reply):
    return reply["reply_markup"].inline_keyboard[0][0].callback_data


@pytest.mark.asyncio
async def test_confirmations_remain_distinct_and_bind_actor_chat_and_topic(tmp_path):
    runtime = _runtime(tmp_path)
    runtime._is_authorized_user = lambda user_id: user_id in {1, 2}
    origin = _update(message_id=1, message_thread_id=11)
    for index in (1, 2):
        await runtime_media._present_safe_voice_transcript(
            runtime, origin, transcript=f"voice {index}", prompt=f"prompt {index}",
            summary=f"summary {index}",
        )
    first, second = [_button(reply) for reply in runtime.replies]
    assert first != second
    assert len(runtime._pending_voice) == 2

    for changed in (
        _callback(first.replace("safevoice:yes:", "safevoice:invalid:"), actor=1, thread=11),
        SimpleNamespace(callback_query=_callback(first, actor=1, thread=11).callback_query),
        _callback(first, actor=2, thread=11),
        _callback(first, actor=1, chat=456, thread=11),
        _callback(first, actor=1, thread=12),
    ):
        await FlexibleAgentRuntime.callback_safevoice(runtime, changed, SimpleNamespace())
        assert len(runtime._pending_voice) == 2
        assert runtime.enqueued == []

    await FlexibleAgentRuntime.callback_safevoice(runtime, _callback(first, thread=11), SimpleNamespace())
    assert [item["prompt"] for item in runtime.enqueued] == ["prompt 1"]
    assert len(runtime._pending_voice) == 1
    await FlexibleAgentRuntime.callback_safevoice(runtime, _callback(first, thread=11), SimpleNamespace())
    assert [item["prompt"] for item in runtime.enqueued] == ["prompt 1"]
    await FlexibleAgentRuntime.callback_safevoice(runtime, _callback(second, thread=11), SimpleNamespace())
    assert [item["prompt"] for item in runtime.enqueued] == ["prompt 1", "prompt 2"]
    assert runtime._pending_voice == {}


@pytest.mark.asyncio
async def test_regular_voice_confirmations_do_not_replace_each_other(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)
    seen = iter(("first transcript", "second transcript"))

    async def transcribe(_transcriber, _path):
        return SimpleNamespace(ok=True, text=next(seen))

    monkeypatch.setattr(runtime_media, "transcribe_audio", transcribe)
    monkeypatch.setattr("orchestrator.voice_transcriber.get_transcriber", lambda: object())
    for index in (1, 2):
        update = _update(update_id=100 + index, message_id=index, message_thread_id=9)
        await runtime_media.handle_voice_or_audio(
            runtime, update, "Voice", f"voice-{index}.ogg", f"file-{index}",
        )
    first, second = [_button(reply) for reply in runtime.replies]
    assert first != second
    assert len(runtime._pending_voice) == 2

    await FlexibleAgentRuntime.callback_safevoice(runtime, _callback(first, thread=9), SimpleNamespace())
    await FlexibleAgentRuntime.callback_safevoice(runtime, _callback(second, thread=9), SimpleNamespace())
    assert [item["prompt"] for item in runtime.enqueued] == [
        "[Voice message transcription] first transcript",
        "[Voice message transcription] second transcript",
    ]
    assert runtime._pending_voice == {}


@pytest.mark.asyncio
async def test_group_callback_is_checked_after_real_telegram_actor_admission(tmp_path):
    from orchestrator.config_json import read_config_json, write_config_json
    from orchestrator.telegram_actor_scope import process_telegram_update
    from tests.test_telegram_actor_scope import instance, update

    runtime, _, config_path = instance(tmp_path)
    config = read_config_json(config_path)
    config["agents"][0]["extra"]["authorized_telegram_ids"] = [75]
    write_config_json(config_path, config)
    runtime._pending_voice = {}
    runtime._long_pending_voice_keys = set()
    runtime._is_authorized_user = lambda actor: actor in {42, 75}
    runtime._reply_text = AsyncMock()
    runtime.enqueue_request = AsyncMock()

    origin = update(42, chat_id=-123, chat_type="supergroup")
    origin.message = SimpleNamespace(message_id=7, message_thread_id=11)
    origin.effective_message = origin.message
    await runtime_media._present_safe_voice_transcript(
        runtime, origin, transcript="original", prompt="original prompt", summary="voice",
    )
    button = runtime._reply_text.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
    assert len(button.encode("utf-8")) <= 64

    async def dispatch(candidate):
        await FlexibleAgentRuntime.callback_safevoice(runtime, candidate, SimpleNamespace())

    runtime.app.process_update = AsyncMock(side_effect=dispatch)
    wrong_actor = _callback(button, actor=75, chat=-123, thread=11)
    wrong_topic = _callback(button, actor=42, chat=-123, thread=12)
    for candidate in (wrong_actor, wrong_topic):
        assert await process_telegram_update(runtime, candidate)
        assert len(runtime._pending_voice) == 1
        runtime.enqueue_request.assert_not_awaited()

    assert await process_telegram_update(runtime, _callback(button, actor=42, chat=-123, thread=11))
    assert runtime._pending_voice == {}
    assert runtime.enqueue_request.await_args.args[1] == "original prompt"
    assert runtime.enqueue_request.await_count == 1


@pytest.mark.asyncio
async def test_native_voice_buttons_release_only_the_corresponding_run(tmp_path):
    runtime = _runtime(tmp_path)
    runtime.session_store = SimpleNamespace(decide_voice_transcript=Mock())
    runtime._native_voice_transcripts = {
        f"run-{i}": {"status": "pending_confirmation", "release_event": asyncio.Event()}
        for i in (1, 2)
    }
    origin = _update(message_id=7, message_thread_id=11)
    for i in (1, 2):
        await runtime_media._present_safe_voice_transcript(
            runtime, origin, transcript=f"voice {i}", prompt=f"prompt {i}",
            summary="voice", request_id=f"run-{i}", native_audio=True,
        )
    first, second = [_button(reply) for reply in runtime.replies]
    await FlexibleAgentRuntime.callback_safevoice(runtime, _callback(first, thread=11), SimpleNamespace())
    runtime.session_store.decide_voice_transcript.assert_called_once_with(
        request_id="run-1", confirmed=True,
    )
    assert runtime._native_voice_transcripts["run-1"]["release_event"].is_set()
    assert not runtime._native_voice_transcripts["run-2"]["release_event"].is_set()
    await FlexibleAgentRuntime.callback_safevoice(runtime, _callback(first, thread=11), SimpleNamespace())
    await FlexibleAgentRuntime.callback_safevoice(runtime, _callback(second, thread=11), SimpleNamespace())
    assert runtime.session_store.decide_voice_transcript.call_count == 2
    assert runtime.session_store.decide_voice_transcript.call_args.kwargs == {
        "request_id": "run-2", "confirmed": True,
    }
    assert runtime._native_voice_transcripts["run-2"]["release_event"].is_set()


@pytest.mark.asyncio
async def test_session_boundary_discards_all_unsubmitted_confirmations(tmp_path):
    runtime = _runtime(tmp_path)
    origin = _update(message_id=7, message_thread_id=11)
    for i in (1, 2):
        await runtime_media._present_safe_voice_transcript(
            runtime, origin, transcript=f"voice {i}", prompt=f"prompt {i}", summary="voice",
        )
    first = _button(runtime.replies[0])
    assert runtime_media.discard_pending_safe_voice_inputs(runtime) == 2
    await FlexibleAgentRuntime.callback_safevoice(runtime, _callback(first, thread=11), SimpleNamespace())
    assert runtime._pending_voice == {}
    assert runtime.enqueued == []


@pytest.mark.asyncio
async def test_unbound_long_voice_discards_reservation_without_leaving_confirmation(tmp_path, monkeypatch):
    from orchestrator import runtime_long

    runtime = _runtime(tmp_path)
    runtime_long.begin_batch(runtime, 123, "original text")

    async def transcribe(_transcriber, _path):
        return SimpleNamespace(ok=True, text="voice")

    monkeypatch.setattr(runtime_media, "transcribe_audio", transcribe)
    monkeypatch.setattr("orchestrator.voice_transcriber.get_transcriber", lambda: object())
    update = _update(update_id=101, message_id=1)
    update.effective_user = None
    await runtime_media.handle_voice_or_audio(runtime, update, "Voice", "voice.ogg", "file-1")

    assert runtime._pending_voice == {}
    assert runtime._long_pending_voice_keys == set()
    assert runtime._long_pending_media_ids == set()
    assert runtime._long_buffer == ["original text"]
    assert runtime.enqueued == []
# HASHI-Fingerprint: hfp_e451bf18b65e4ce1a3a6139c3b9d9aa2
