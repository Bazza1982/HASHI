"""Voice command observes the shared producer and actual FC delivery outcome."""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from telegram.error import BadRequest

from orchestrator import runtime_delivery, runtime_session
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from orchestrator.session_store import SessionStore
from orchestrator.voice_synthesizer import VoiceAsset, VoiceSynthesisError


def voice_runtime(tmp_path, monkeypatch, *, synthesis_error=None, missing_asset=False, send_error=None):
    from orchestrator import frontend_agent_presence
    monkeypatch.setattr(frontend_agent_presence, "schedule_agentpresence_audio_event", lambda *args, **kwargs: None)
    audio = tmp_path / "fixture.ogg"
    if not missing_asset:
        audio.write_bytes(b"OggS" + b"\0" * 32)
    counts = {"synthesis": 0, "send": 0, "publish": 0}
    notices = []

    class Manager:
        async def synthesize_reply(self, *args, **kwargs):
            counts["synthesis"] += 1
            if synthesis_error:
                raise synthesis_error
            return VoiceAsset(
                provider="fixture", text="fixture reply", spoken_text="fixture reply",
                wav_path=None, ogg_path=audio,
            )

    class Bot:
        async def send_voice(self, **kwargs):
            counts["send"] += 1
            if send_error:
                raise send_error
            return SimpleNamespace(message_id=101)

    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime.telegram_connected = True
    runtime.voice_manager = Manager()
    runtime.name = "qa"
    runtime.app = SimpleNamespace(bot=Bot())
    runtime.config = SimpleNamespace(active_backend="codex-cli", extra={})
    runtime.global_config = SimpleNamespace(
        authorized_id=123, instance_id="HASHI1", project_root=tmp_path, ui_language="en",
    )
    runtime.workspace_dir = tmp_path
    runtime.session_dir = tmp_path
    runtime.session_store = SessionStore(tmp_path / "sessions.sqlite3", instance_id="HASHI1")
    runtime.logger = runtime.telegram_logger = runtime.error_logger = logging.getLogger("voice-outcome-test")
    runtime._notify_enabled = False
    runtime._mark_error = lambda *args: None
    runtime._is_authorized_user = lambda _id: True
    runtime._load_recent_visible_assistant_texts = lambda update, limit: ["fixture reply"]

    async def reply_text(update, message):
        notices.append(message)

    async def not_blocked(*args, **kwargs):
        return False

    runtime._reply_text = reply_text
    monkeypatch.setattr(runtime_delivery.telegram_delivery_failover, "handle_blocked_send", not_blocked)
    return runtime, counts, notices


@pytest.mark.asyncio
@pytest.mark.parametrize("phase,code", [
    ("synthesis", "tts_timeout"),
    ("prepare", "voice_asset_missing"),
    ("publication", "voice_publication_unconfirmed"),
    ("delivery", "telegram_media_failed"),
])
async def test_say_preserves_the_actual_failure_step_and_reason(tmp_path, monkeypatch, phase, code):
    runtime, counts, notices = voice_runtime(
        tmp_path, monkeypatch,
        synthesis_error=VoiceSynthesisError("tts_timeout", "The selected voice service timed out.")
        if phase == "synthesis" else None,
        missing_asset=phase == "prepare",
        send_error=BadRequest("Voice messages are forbidden") if phase == "delivery" else None,
    )
    publisher = runtime_session.publish_frontend_media_notification

    def publish(*args, **kwargs):
        counts["publish"] += 1
        publication = publisher(*args, **kwargs)
        if phase == "publication":
            raise OSError("private-path token=private-secret: acknowledgment lost after commit")
        return publication

    monkeypatch.setattr(runtime_session, "publish_frontend_media_notification", publish)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123), effective_chat=SimpleNamespace(id=123),
    )
    await FlexibleAgentRuntime.cmd_say(runtime, update, SimpleNamespace(args=[]))
    assert len(notices) == 1
    assert code in notices[0], notices[0]
    assert "private-" not in notices[0]
    assert counts["synthesis"] == 1
    assert counts["send"] == (1 if phase == "delivery" else 0)
    assert counts["publish"] == (1 if phase in {"publication", "delivery"} else 0)
    if phase == "publication":
        assert "unconfirmed" in notices[0].lower()
        session = runtime.session_store.resolve_primary_session(owner_id="user:123", agent_id="qa")
        events = runtime.session_store.events(session["session_id"], owner_id="user:123")
        assert sum(event["kind"] == "frontend.message.recorded" for event in events) == 1


@pytest.mark.asyncio
async def test_acknowledged_voice_is_not_reported_failed_after_bookkeeping_exception(tmp_path, monkeypatch):
    from orchestrator.frontend_connector_registry import endpoint_id_for
    from orchestrator.session_store import SessionStoreError

    runtime, counts, _ = voice_runtime(tmp_path, monkeypatch)
    dispatch = runtime_delivery.dispatch_claimed_telegram_event

    async def acknowledged_then_exception(*args, **kwargs):
        await dispatch(*args, **kwargs)
        raise RuntimeError("private-bookkeeping-detail")

    monkeypatch.setattr(runtime_delivery, "dispatch_claimed_telegram_event", acknowledged_then_exception)
    runtime._mark_error = lambda *args: pytest.fail("An acknowledged voice is not a failed delivery")
    result = await runtime._send_voice_reply_result(123, "fixture reply", "ack-then-error", force=True)
    assert result.delivered is True and counts["send"] == 1

    store = runtime.session_store
    session = store.resolve_primary_session(owner_id="user:123", agent_id="qa")
    event = next(row for row in store.events(session["session_id"], owner_id="user:123")
                 if row["kind"] == "frontend.message.recorded")
    scope = dict(session_id=session["session_id"], owner_id="user:123", event_id=event["event_id"],
                 connector_id="telegram", endpoint_id=endpoint_id_for("telegram", ingress_transport="telegram", channel_key="123"))
    evidence = store.frontend_delivery_task_result(**scope)
    assert evidence["state"] == "completed" and evidence["error_code"] == ""
    assert store.frontend_delivery_task_result(**{**scope, "endpoint_id": "another-endpoint"}) is None
    with pytest.raises(SessionStoreError):
        store.frontend_delivery_task_result(**{**scope, "owner_id": "user:someone-else"})


@pytest.mark.asyncio
async def test_other_endpoint_receipt_cannot_confirm_this_voice_delivery(tmp_path, monkeypatch):
    from telegram.error import TimedOut

    runtime, counts, _ = voice_runtime(tmp_path, monkeypatch, send_error=TimedOut("ack timeout"))
    original = runtime.session_store.frontend_delivery_receipts

    def receipts(**kwargs):
        return original(**kwargs) + [{"endpoint_id": "unrelated-endpoint", "status": "delivered"}]

    monkeypatch.setattr(runtime.session_store, "frontend_delivery_receipts", receipts)
    runtime._mark_error = lambda *args: pytest.fail("Unconfirmed delivery must not become a hard failure")
    result = await runtime._send_voice_reply_result(123, "fixture reply", "target-timeout", force=True)
    assert result.delivered is None
    assert result.stage == "delivery" and result.code == "telegram_media_unknown"
    assert counts["send"] == 1


@pytest.mark.asyncio
async def test_voice_reply_cancellation_remains_cancellation(tmp_path, monkeypatch):
    import asyncio

    runtime, counts, _ = voice_runtime(tmp_path, monkeypatch, synthesis_error=asyncio.CancelledError())
    runtime._mark_error = lambda *args: pytest.fail("Cancellation is not a failed voice reply")
    with pytest.raises(asyncio.CancelledError):
        await runtime._send_voice_reply_result(123, "fixture reply", "cancelled-voice", force=True)
    assert counts["send"] == 0


@pytest.mark.asyncio
async def test_concurrent_synthesis_failures_keep_their_own_reason_and_reference(tmp_path, monkeypatch):
    import asyncio

    runtime, _, _ = voice_runtime(tmp_path, monkeypatch)
    ready = asyncio.Event()
    entered = 0

    class ConcurrentManager:
        async def synthesize_reply(self, agent, request_id, text, **kwargs):
            nonlocal entered
            entered += 1
            if entered == 2:
                ready.set()
            await ready.wait()
            raise VoiceSynthesisError("tts_timeout" if text == "first" else "tts_text_invalid", text + " reason")

    runtime.voice_manager = ConcurrentManager()
    first, second = await asyncio.gather(
        runtime._send_voice_reply_result(123, "first", "first-failure", force=True),
        runtime._send_voice_reply_result(123, "second", "second-failure", force=True),
    )
    assert (first.code, first.reason) == ("tts_timeout", "first reason")
    assert (second.code, second.reason) == ("tts_text_invalid", "second reason")
    assert first.reference != second.reference
