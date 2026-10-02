"""Phone boundary and durable opening checks using a non-OpenAI wire protocol.

The alternate protocol is an offline fixture, not a qualified product provider.
"""

import asyncio
import base64
from contextlib import asynccontextmanager
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import aiohttp
import pytest
from orchestrator.frontend_live_voice.manager import LiveVoiceManager, _wait_for_monotonic_deadline
from orchestrator.frontend_live_voice.opening import OpeningAudioObservation
from orchestrator.frontend_live_voice.openai_live import OpenAILiveProvider
from orchestrator.frontend_live_voice.protocol import CallBinding, LiveVoiceError
from orchestrator.frontend_live_voice.provider import (
    ProviderCapabilities,
    default_registry,
)
from orchestrator.session_store import SessionStore


class AlternateSocket:
    def __init__(self, adapter):
        self.adapter = adapter
        self.closed = False
        self.incoming = asyncio.Queue()
        self.incoming.put_nowait({"signal": "connected"})

    def __aiter__(self):
        return self

    async def __anext__(self):
        event = await self.incoming.get()
        if event is None:
            raise StopAsyncIteration
        return SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(event))

    async def send_json(self, packet):
        self.adapter.sent.append(packet)
        if packet["command"] in {"greet", "context"} and self.adapter.acknowledge:
            self.incoming.put_nowait(
                {"signal": "accepted", "request": packet["request"]}
            )
        elif packet["command"] == "close":
            self.incoming.put_nowait({"signal": "closed"})

    async def close(self):
        self.closed = True
        self.incoming.put_nowait(None)


class AlternateProvider:
    provider_id = "fixture-local"
    capabilities = ProviderCapabilities("fixture-pcm-v2", 64, 8000, 4000, 600)

    def __init__(self):
        self.sent = []
        self.created = []
        self.sockets = []
        self.acknowledge = True

    def credential(self, secrets):
        return secrets.get("fixture_credential", "")

    def validate_selection(self, model, voice):
        if model != "fixture-speech" or voice != "fixture-voice":
            raise LiveVoiceError("fixture_selection_invalid")

    def media_descriptor(self):
        return {
            "transport": "webrtc",
            "protocol": self.capabilities.version,
            "data_channel": "fixture-signals",
            "version": 1,
        }

    def encode_history(self, messages):
        return [{"who": item["role"], "said": item["text"]} for item in messages]

    def validate_session(self, instructions, model, voice, messages):
        self.validate_selection(model, voice)
        assert instructions
        self.encode_history(messages)

    @asynccontextmanager
    async def http_session(self):
        yield self

    async def fit_input(self, http, **kwargs):
        return (
            self.encode_history(kwargs["input_messages"]),
            {"input_tokens_exact": 12, "history_omitted_units": 0},
        )

    async def create(self, http, **kwargs):
        self.created.append(kwargs)
        return {
            "provider_session_id": f"fixture-{len(self.created)}",
            "sdp_answer": "v=0\r\n",
        }

    async def attach(self, http, **kwargs):
        socket = AlternateSocket(self)
        self.sockets.append(socket)
        return socket

    def normalize_event(self, raw):
        item = json.loads(raw)
        return {
            "connected": {"type": "provider.ready"},
            "accepted": {
                "type": "update.accepted",
                "client_event_id": item.get("request"),
            },
            "audio": {"type": "output.generated", "completion": "unknown",
                      "activity": item.get("activity", "unknown"),
                      "start_ms": item.get("start_ms"), "end_ms": item.get("end_ms")},
            "assistant": {
                "type": "conversation.assistant.delta",
                "event_id": item.get("event_id", "assistant-opening-1"),
                "delta": item.get("text", "Hello, let's continue our conversation."),
                "start_ms": 200,
                "end_ms": 600,
            },
            "closed": {"type": "provider.closed", "reason": "requested"},
        }.get(item.get("signal"))

    def update(self, kind, content, delegation_id, event_id):
        if delegation_id is not None:
            raise LiveVoiceError("fixture_unknown_native_delegation", 502)
        return {
            "command": "context",
            "request": event_id,
            "mode": kind,
            "facts": content,
        }

    def control(self, action, event_id):
        return {"command": action, "request": event_id}

    def opening(self, goal, event_id):
        return {"command": "greet", "request": event_id, "goal": goal}


class TestPhoneProviderOpening:
    @pytest.fixture(autouse=True)
    async def prepared_phone(self):
        await self.asyncSetUp()
        try:
            yield
        finally:
            await self.asyncTearDown()

    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = SessionStore(
            Path(self.directory.name) / "session.sqlite3", instance_id="HASHI"
        )
        self.session = self.store.create_session(
            owner_id="owner", agent_id="agent", title="Phone"
        )
        self.adapter = AlternateProvider()
        self.phone = {
            "provider": "fixture-local",
            "model": "fixture-speech",
            "voice": "fixture-voice",
            "instructions": "Current effective persona prefers respectful Japanese; this is already projected.",
            "input": [
                {
                    "role": "user",
                    "text": "Please continue the subject we were discussing.",
                }
            ],
            "public": {
                "provider": "fixture-local",
                "model": "fixture-speech",
                "voice": "fixture-voice",
                "revision": "a" * 64,
                "language": "ja",
            },
        }
        self.manager = LiveVoiceManager(
            self.store,
            SimpleNamespace(
                instance_id="HASHI", instance_generation="1", live_voice_v1=True
            ),
            {"fixture_credential": "test-only"},
            provider_registry={self.adapter.provider_id: self.adapter},
            resolve_phone_session=lambda *_args, **_kwargs: self.phone,
            control_timeout_seconds=0.05,
            close_timeout_seconds=0.05,
            opening_grace_seconds=0.02,
        )
        self.context = await self.manager._op_context(
            "owner",
            {
                "session_id": self.session["session_id"],
                "agent_id": "agent",
                "context_generation": 1,
            },
        )

    async def asyncTearDown(self):
        await self.manager.shutdown()
        self.directory.cleanup()

    async def start_call(self):
        self.result = await self.manager._op_start(
            "owner",
            {
                **self.context["binding"],
                "phone_revision": "a" * 64,
                "sdp": "v=0\r\n",
                "idempotency_key": "start-test",
            },
        )
        self.scope = self.result["binding"]
        self.binding = CallBinding(
            "owner",
            **self.scope,
            provider_session_id=self.result["provider_session_id"],
        )
        await asyncio.sleep(0)
        return self.result

    async def observe(self, kind, **detail):
        return await self.manager._op_observe(
            "owner",
            {
                **self.scope,
                "event": kind,
                "event_id": f"obs-{len(self.adapter.sent)}-{kind}-{len(detail)}",
                "observed_at": "2026-09-30T00:00:00Z",
                "client_sequence": 1,
                "detail": detail,
            },
        )

    async def settle(self):
        for _ in range(10):
            await asyncio.sleep(0.01)
            if not self.manager._opening_tasks:
                break

    async def until(self, predicate):
        async with asyncio.timeout(5):
            while not predicate():
                await asyncio.sleep(0.005)

    def audio_observation(self):
        return self.manager._opening_audio.get((self.binding.call_id, self.binding.call_epoch,
                                               self.binding.provider_session_id))

    async def feed_silence(self, *, defect=None, callback=None, prior_baseline=True, lose_second_ack=False):
        # Model continuous PCM independently of the caller task. Gate the first
        # ACK explicitly so slow SQLite/Windows scheduling cannot accidentally
        # make the test exercise a different protocol phase.
        self.manager._opening_output_timeout_seconds = 0.6
        self.manager._control_timeout_seconds = 1.0
        self.adapter.acknowledge = False
        await self.observe("client.media_ready", input_active=True, playback_unlocked=True)
        await self.until(lambda: any(packet["command"] == "greet" for packet in self.adapter.sent))
        await self.until(lambda: self.audio_observation() is not None)
        stream_ready = asyncio.Event()
        inject_defect = asyncio.Event()
        async def stream():
            position = 0
            began = asyncio.get_running_loop().time()
            while True:
                end = max(position + 1, round((asyncio.get_running_loop().time() - began) * 1000))
                selected = defect if inject_defect.is_set() else None
                inject_defect.clear()
                self.adapter.sockets[0].incoming.put_nowait({
                    "signal": "audio", "activity": selected if selected and selected != "gap" else "silent",
                    "start_ms": position + (1 if selected == "gap" else 0), "end_ms": end,
                })
                position = end
                stream_ready.set()
                await asyncio.sleep(0.01)
        task = None
        try:
            if prior_baseline:
                task = asyncio.create_task(stream())
                await stream_ready.wait()
                await self.until(lambda: self.audio_observation().last_end_ms is not None)
            first = next(packet for packet in self.adapter.sent if packet["command"] == "greet")
            self.adapter.sockets[0].incoming.put_nowait({"signal": "accepted", "request": first["request"]})
            await self.until(lambda: self.audio_observation().acknowledged_at is not None)
            self.adapter.acknowledge = not lose_second_ack
            if task is None:
                task = asyncio.create_task(stream())
            if defect:
                inject_defect.set()
            if callback is not None:
                await callback()
            await self.until(lambda: not self.manager._opening_tasks)
        finally:
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_confirmed_continuous_silence_gets_only_one_opening_continuation(self):
        await self.start_call()
        await self.feed_silence()
        await self.settle()
        greetings = [packet for packet in self.adapter.sent if packet["command"] == "greet"]
        assert len(greetings) == 2
        assert greetings[0]["request"] != greetings[1]["request"]
        state = self.manager.opening.read(self.binding)
        assert state["continuation_reserved"] is True
        assert state["continuation_sent"] is True
        assert not state["output_observed"]
        await self.observe("client.media_ready", input_active=True, playback_unlocked=True)
        await self.settle()
        assert len([packet for packet in self.adapter.sent if packet["command"] == "greet"]) == 2

    async def test_early_timer_wakeup_still_waits_for_actual_monotonic_deadline(self):
        current = 0.0
        waits = []
        async def early_sleep(duration):
            nonlocal current
            waits.append(duration)
            current += duration - 0.01 if len(waits) == 1 else duration
        await _wait_for_monotonic_deadline(8.0, clock=lambda: current, sleep=early_sleep)
        assert current >= 8.0
        assert waits == [8.0, 0.02]

    async def test_uncertain_or_non_silent_audio_cannot_be_washed_away_by_silence(self):
        await self.start_call()
        await self.feed_silence(defect="non_silent")
        await self.settle()
        assert len([packet for packet in self.adapter.sent if packet["command"] == "greet"]) == 1
        assert not self.manager.opening.read(self.binding).get("continuation_reserved")

    async def test_missing_audio_frame_cannot_authorize_opening_continuation(self):
        await self.start_call()
        await self.feed_silence(defect="gap")
        await self.settle()
        assert len([packet for packet in self.adapter.sent if packet["command"] == "greet"]) == 1

    async def test_unknown_audio_cannot_be_washed_away_by_later_silence(self):
        await self.start_call()
        await self.feed_silence(defect="unknown")
        await self.settle()
        assert len([packet for packet in self.adapter.sent if packet["command"] == "greet"]) == 1

    async def test_user_speech_during_silence_observation_cancels_continuation(self):
        await self.start_call()
        await self.feed_silence(callback=lambda: self.observe("client.user_speech_started"))
        await self.settle()
        assert len([packet for packet in self.adapter.sent if packet["command"] == "greet"]) == 1
        assert self.manager.opening.read(self.binding)["user_started"]

    async def test_missing_continuation_ack_never_sends_a_third_request(self):
        await self.start_call()
        await self.feed_silence(lose_second_ack=True)
        await self.settle()
        state = self.manager.opening.read(self.binding)
        assert state["continuation_sent"] and not state["continuation_accepted"]
        assert state["state"] == "uncertain"
        assert len([packet for packet in self.adapter.sent if packet["command"] == "greet"]) == 2
        await self.observe("client.media_ready", input_active=True, playback_unlocked=True)
        await self.settle()
        assert len([packet for packet in self.adapter.sent if packet["command"] == "greet"]) == 2

    async def test_only_silence_after_ack_without_prior_pcm_baseline_is_uncertain(self):
        await self.start_call()
        await self.feed_silence(prior_baseline=False)
        await self.settle()
        assert len([packet for packet in self.adapter.sent if packet["command"] == "greet"]) == 1

    async def test_epoch_change_during_observation_never_continues_old_opening(self):
        await self.start_call()
        async def change_epoch():
            with self.store._lock, self.store._connection() as connection:
                connection.execute("UPDATE live_calls SET call_epoch=2,provider_session_id='next-provider' WHERE call_id=?",
                                   (self.binding.call_id,))
        await self.feed_silence(callback=change_epoch)
        await self.settle()
        assert len([packet for packet in self.adapter.sent if packet["command"] == "greet"]) == 1
        assert not self.manager._opening_audio

    async def test_audio_observations_do_not_read_or_write_session_state_per_packet(self):
        self.manager._control_timeout_seconds = 1.0
        await self.start_call()
        await self.observe("client.media_ready", input_active=True, playback_unlocked=True)
        await self.until(lambda: self.audio_observation() is not None
                         and self.audio_observation().acknowledged_at is not None)
        with patch.object(self.store, "_connection", wraps=self.store._connection) as connections:
            for index in range(100):
                self.adapter.sockets[0].incoming.put_nowait({"signal": "audio", "activity": "silent",
                    "start_ms": index * 10, "end_ms": (index + 1) * 10})
            await self.until(lambda: self.audio_observation().last_end_ms == 1000)
            assert connections.call_count == 0
        observation = self.manager._opening_audio[(self.binding.call_id, 1, self.binding.provider_session_id)]
        assert observation.last_end_ms == 1000

    async def test_native_reflected_pcm_silence_is_validated_without_exposing_audio(self):
        adapter = OpenAILiveProvider()
        audio = base64.b64encode(b"\0\0" * 240).decode()
        raw = {"type": "session.output_audio.delta", "delta": audio, "start_ms": 0, "end_ms": 10}
        event = adapter.normalize_event(json.dumps(raw))
        assert event["activity"] == "silent"
        assert "delta" not in event and "audio" not in event
        for changed in ({"delta": ""}, {"delta": "bad"}, {"delta": base64.b64encode(b"\0").decode()},
                        {"start_ms": True}, {"end_ms": 11}, {"end_ms": float("nan")},
                        {"start_ms": 10**1000, "end_ms": 10**1000 + 10}):
            assert adapter.normalize_event(json.dumps({**raw, **changed}))["activity"] == "unknown"
        speaking = base64.b64encode(b"\x20\x00" * 240).decode()
        assert adapter.normalize_event(json.dumps({**raw, "delta": speaking}))["activity"] == "non_silent"

    async def test_alternate_protocol_waits_for_media_and_separates_ack_audio_playback(
        self,
    ):
        assert self.context["capability"]["available"]
        assert self.context["capability"]["media"]["protocol"] == "fixture-pcm-v2"
        await self.start_call()
        assert self.adapter.created[0]["input_messages"][0]["who"] == "user"
        snapshot = await self.manager._op_snapshot("owner", self.scope)
        assert snapshot["snapshot"]["media"] == self.result["media"]
        assert "selection" not in snapshot["snapshot"]
        assert self.adapter.sent == []
        await self.observe(
            "client.media_ready", input_active=True, playback_unlocked=True
        )
        await self.settle()
        state = self.manager.opening.read(self.binding)
        assert state["state"] == "accepted"
        assert state["request_accepted"]
        assert not state["output_observed"]
        assert not state["playback_observed"]
        assert len([x for x in self.adapter.sent if x["command"] == "greet"]) == 1
        assert "ja" in self.adapter.sent[0]["goal"]
        assert "爸爸" not in self.adapter.sent[0]["goal"]
        self.adapter.sockets[0].incoming.put_nowait({"signal": "audio"})
        await asyncio.sleep(0.02)
        assert not self.manager.opening.read(self.binding)["output_observed"]
        self.adapter.sockets[0].incoming.put_nowait({"signal": "assistant"})
        for _ in range(50):
            if self.manager.opening.read(self.binding).get("output_observed"):
                break
            await asyncio.sleep(0.01)
        assert self.manager.opening.read(self.binding)["output_evidence"] == "assistant_transcript"
        await self.observe(
            "client.playback_progress",
            current_time_ms=200,
            opening_id=state["opening_id"],
        )
        assert self.manager.opening.read(self.binding)["playback_observed"]
        opening_audit = [
            json.loads(line)["detail"]
            for line in self.manager.audit.path_for(self.binding).read_text(encoding="utf-8").splitlines()
            if json.loads(line)["event"] == "opening.state"
        ]
        assert opening_audit[-1]["request_sent"] is True
        assert opening_audit[-1]["request_accepted"] is True
        assert opening_audit[-1]["output_evidence"] == "assistant_transcript"
        assert opening_audit[-1]["playback_observed"] is True
        self.adapter.sockets[0].incoming.put_nowait({"signal": "connected"})
        await self.observe(
            "client.media_ready", input_active=True, playback_unlocked=True
        )
        await self.settle()
        assert len([x for x in self.adapter.sent if x["command"] == "greet"]) == 1

    async def test_user_first_skips_opening_and_stale_media_cannot_reopen_it(self):
        await self.start_call()
        await self.observe("client.user_speech_started")
        await self.observe(
            "client.media_ready", input_active=True, playback_unlocked=True
        )
        await self.settle()
        assert self.manager.opening.read(self.binding)["state"] == "skipped"
        assert self.adapter.sent == []

    async def test_worker_barge_in_before_transcription_skips_pending_opening(self):
        await self.start_call()
        self.manager._observe_opening_provider(self.binding, {
            "type": "output.interrupted", "reason": "user_speech_interrupted",
        })
        await self.observe("client.media_ready", input_active=True, playback_unlocked=True)
        await self.settle()
        assert self.manager.opening.read(self.binding)["state"] == "skipped"
        assert self.adapter.sent == []

    async def test_continuous_audio_during_grace_does_not_consume_opening_request(self):
        self.manager._opening_grace_seconds = 0.08
        await self.start_call()
        await self.observe(
            "client.media_ready", input_active=True, playback_unlocked=True
        )
        self.adapter.sockets[0].incoming.put_nowait({"signal": "audio"})
        self.adapter.sockets[0].incoming.put_nowait({"signal": "assistant"})
        await asyncio.sleep(0.01)
        state = self.manager.opening.read(self.binding)
        assert state["state"] == "requested"
        assert state["request_sent"] is False
        assert state["output_observed"] is False
        await self.settle()
        state = self.manager.opening.read(self.binding)
        assert state["request_sent"] is True
        assert state["request_accepted"] is True
        assert state["state"] == "accepted"
        assert len([item for item in self.adapter.sent if item["command"] == "greet"]) == 1

    async def test_silent_audio_and_playback_clock_after_ack_are_not_speech(self):
        await self.start_call()
        await self.observe(
            "client.media_ready", input_active=True, playback_unlocked=True
        )
        await self.settle()
        self.adapter.sockets[0].incoming.put_nowait({"signal": "audio"})
        await asyncio.sleep(0.01)
        state = self.manager.opening.read(self.binding)
        await self.observe("client.playback_progress", current_time_ms=30000,
                           opening_id=state["opening_id"])
        state = self.manager.opening.read(self.binding)
        assert state["state"] == "accepted"
        assert state["output_observed"] is False
        assert state["playback_observed"] is False

    async def test_lost_ack_is_uncertain_no_retry_or_hangup(self):
        self.adapter.acknowledge = False
        await self.start_call()
        await self.observe(
            "client.media_ready", input_active=True, playback_unlocked=True
        )
        await self.settle()
        assert self.manager.opening.read(self.binding)["state"] == "uncertain"
        await self.observe(
            "client.media_ready", input_active=True, playback_unlocked=True
        )
        await self.settle()
        assert len(self.adapter.sent) == 1
        snapshot = await self.manager._op_snapshot("owner", self.scope)
        assert snapshot["snapshot"]["phase"] == "active"

    async def test_user_wins_during_opening_reservation_window(self):
        await self.start_call()
        await self.observe(
            "client.media_ready", input_active=True, playback_unlocked=True
        )
        await asyncio.sleep(0)
        await self.observe("client.user_speech_started")
        await self.settle()
        assert self.manager.opening.read(self.binding)["state"] == "skipped"
        assert self.adapter.sent == []

    async def test_hangup_wins_during_opening_reservation_window(self):
        await self.start_call()
        await self.observe(
            "client.media_ready", input_active=True, playback_unlocked=True
        )
        result = await self.manager._finish_call(
            self.binding, reason="user_hangup", initiator="user"
        )
        await self.settle()
        assert result["phase"] == "ended"
        assert not any((packet["command"] == "greet" for packet in self.adapter.sent))
        assert self.manager.opening.read(self.binding)["state"] == "skipped"

    async def test_cleanup_without_call_binding_uses_its_actual_provider(self):
        await self.start_call()
        close_state, usage = await self.manager._close_provider_session(
            "test-only", self.result["provider_session_id"]
        )
        assert close_state == "confirmed"
        assert usage is None
        assert self.adapter.sent[-1]["command"] == "close"

    async def test_application_action_ids_do_not_leak_to_native_provider_tasks(self):
        await self.start_call()
        delivered = await self.manager._send_provider_update(
            self.binding, kind="commentary", content="The saved record has been verified.",
            delegation_id="action-request-application-owned",
        )
        assert delivered is True
        assert self.adapter.sent[-1]["command"] == "context"

    async def test_resume_uses_frozen_provider_and_durable_opening(self):
        await self.start_call()
        await self.observe(
            "client.media_ready", input_active=True, playback_unlocked=True
        )
        await self.settle()
        original = self.manager.opening.read(self.binding)
        self.phone = {
            **self.phone,
            "provider": "future-provider",
            "voice": "future-voice",
            "public": {
                **self.phone["public"],
                "provider": "future-provider",
                "voice": "future-voice",
                "revision": "b" * 64,
            },
        }
        self.manager._mark_recovering(
            self.binding, reason="network_offline", summary="Network interrupted"
        )
        restoring_context = await self.manager._op_context(
            "owner", {"session_id": self.session["session_id"], "agent_id": "agent", "context_generation": 1}
        )
        assert restoring_context["foreground_call"]["media"] == self.result["media"]
        resumed = await self.manager._op_resume(
            "owner",
            {
                **self.scope,
                "phone_revision": "a" * 64,
                "sdp": "v=0\r\n",
                "idempotency_key": "resume-test",
            },
        )
        self.scope = resumed["binding"]
        self.binding = CallBinding(
            "owner", **self.scope, provider_session_id=resumed["provider_session_id"]
        )
        assert resumed["phone"]["provider"] == "fixture-local"
        assert self.adapter.created[-1]["voice"] == "fixture-voice"
        await self.observe(
            "client.media_ready", input_active=True, playback_unlocked=True
        )
        await self.settle()
        assert (
            self.manager.opening.read(self.binding)["opening_id"]
            == original["opening_id"]
        )
        assert len([x for x in self.adapter.sent if x["command"] == "greet"]) == 1


class QualifiedAdapterTests(unittest.TestCase):
    def test_stale_pre_ack_baseline_and_delayed_first_frame_are_unknown(self):
        stale = OpeningAudioObservation()
        stale.observe({"activity": "silent", "start_ms": 0, "end_ms": 10}, now=0)
        stale.acknowledge(now=2)
        assert stale.uncertain is True
        delayed = OpeningAudioObservation()
        delayed.observe({"activity": "silent", "start_ms": 0, "end_ms": 10}, now=0)
        delayed.acknowledge(now=0)
        delayed.observe({"activity": "silent", "start_ms": 10, "end_ms": 20}, now=2)
        assert delayed.uncertain is True
        invalid = OpeningAudioObservation()
        invalid.observe({"activity": "silent", "start_ms": 10**1000, "end_ms": 10**1000 + 10}, now=0)
        assert invalid.uncertain is True

    def test_sparse_arrival_then_old_frame_burst_cannot_prove_continuous_silence(self):
        observation = OpeningAudioObservation()
        observation.observe({"activity": "silent", "start_ms": 0, "end_ms": 10}, now=0)
        observation.acknowledge(now=0)
        observation.observe({"activity": "silent", "start_ms": 10, "end_ms": 20}, now=0.1)
        for index in range(80):
            observation.observe({"activity": "silent", "start_ms": 20 + index * 100,
                                 "end_ms": 120 + index * 100}, now=7.9)
        assert observation.silence_proof(elapsed_seconds=8, now=8) is None
        assert observation.uncertain is True

    def test_burst_old_frames_and_absent_pre_ack_audio_cannot_prove_live_silence(self):
        observation = OpeningAudioObservation()
        observation.observe({"activity": "silent", "start_ms": 0, "end_ms": 10}, now=0)
        observation.acknowledge(now=0)
        observation.observe({"activity": "silent", "start_ms": 10, "end_ms": 7010}, now=8)
        assert observation.silence_proof(elapsed_seconds=8, now=8.1) is None
        missing = OpeningAudioObservation()
        missing.acknowledge(now=0)
        for index in range(9):
            missing.observe({"activity": "silent", "start_ms": index * 1000, "end_ms": (index + 1) * 1000}, now=index)
        assert missing.silence_proof(elapsed_seconds=8, now=8.1) is None

    def test_default_registry_and_reflected_audio_are_honest(self):
        assert set(default_registry()) == {"openai"}
        adapter = OpenAILiveProvider()
        assert adapter.normalize_event(
            json.dumps(
                {
                    "type": "session.output_audio.delta",
                    "delta": "private-audio-bytes",
                    "start_ms": 1,
                    "end_ms": 20,
                }
            )
        ) == {
            "type": "output.generated",
            "activity": "unknown",
            "start_ms": 1,
            "end_ms": 20,
            "completion": "unknown",
        }
        assert adapter.encode_history([{"role": "assistant", "text": "Prior reply"}])[
            0
        ]["content"] == [{"type": "output_text", "text": "Prior reply"}]
