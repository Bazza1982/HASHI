"""External AV must fence presentation without changing PAO input ownership."""

import asyncio
import hashlib
from dataclasses import replace
import pytest
from orchestrator.frontend_call.contract import CallError
from orchestrator.frontend_call.video_tts import VideoTTSSelection
from test_call_service import setup
from test_call_contract import wav

SELECTION = {
    "enabled": True,
    "reference": {"node_id": "studio", "service_id": "avatar", "model_id": "talker"},
    "protocol": "video.speech.async.v1",
    "voice": "warm",
}


class Output:
    audio_owner = "agentpresence"

    def __init__(self):
        self.epoch = self.generation = 0
        self.closed = False
        self.internal_call_id = "ap-internal-call-1"
        self.reference = b"AP-owned PNG"
        self.state = {"replies": []}
        self.starts = self.ends = self.interrupts = self.commits = 0
        self.prepared = []

    async def start(self):
        self.starts += 1

    def view(self):
        return {"mode": "external_av", "audio_owner": self.audio_owner, "stage": "idle"}

    def require_active(self, epoch):
        if self.closed or epoch != self.epoch:
            raise CallError("call_video_interrupted", 409)

    async def prepare(self, utterance, media, *, epoch):
        self.require_active(epoch)
        assert media == b"AV media"
        self.prepared.append(utterance)
        receipt = {
            "call_id": self.internal_call_id,
            "context_id": "ap-context-1",
            "utterance_id": utterance,
            "state": "prepared",
            "generation": self.generation,
        }
        self.state["replies"].append(receipt)
        return receipt

    async def commit(self, utterance, *, epoch):
        self.require_active(epoch)
        self.commits += 1
        receipt = next(
            x for x in self.state["replies"] if x["utterance_id"] == utterance
        )
        receipt["state"] = "playing"
        return receipt

    async def refresh(self):
        return {"call_active": not self.closed, **self.state}

    def fence(self, key):
        self.epoch += 1
        return True

    async def interrupt(self):
        self.interrupts += 1

    async def end(self):
        self.closed = True
        self.ends += 1


class Generator:
    def __init__(self):
        self.references = []
        self.release = None
        self.started = asyncio.Event()
        self.error = None

    async def resolve(self, *args):
        return None

    async def synthesize(self, selection, reference, text, **kwargs):
        self.references.append(reference)
        self.started.set()
        if self.release:
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                # A remote server may finish after client cancellation.
                await self.release.wait()
        if self.error is not None:
            raise self.error
        return {"media": b"AV media", "provider_receipt": {"job_id": "job-one"}}


async def arranged(tmp_path, *, connected=True, protocol="call-output/1"):
    service, ports, adapters, base, clock = setup(tmp_path)
    generator, output = Generator(), Output()
    service.video_synthesizer = generator
    service.config.video_tts_selection = lambda revision: VideoTTSSelection.parse(
        SELECTION
    )

    async def resolve(*args, **kwargs):
        return output if connected else None

    ports.call_output = resolve
    ports.frontend_subscribers_value = ("agentpresence",)
    info = await service.invoke("owner", {**base, "operation": "context"})
    body = {
        **base,
        "operation": "start",
        "call_id": "call-1",
        "generation": info["generation"],
        "revision": info["revision"],
    }
    if protocol:
        body["output_protocol"] = protocol
    return service, ports, adapters, base, generator, output, body


async def test_connected_output_requires_client_audio_handoff(tmp_path):
    service, ports, adapters, base, gen, output, body = await arranged(
        tmp_path, protocol=None
    )
    with pytest.raises(CallError, match="call_output_client_upgrade_required"):
        await service.invoke("owner", body)
    assert output.starts == 0 and adapters.tts == 0
    await service.close()


async def test_unconnected_call_keeps_legacy_audio(tmp_path):
    service, ports, adapters, base, gen, output, body = await arranged(
        tmp_path, connected=False
    )
    result = await service.invoke("owner", body)
    assert result.get("output", {}).get("mode", "window_audio") == "window_audio"
    assert gen.references == [] and output.starts == 0
    assert ports.frontend_events[0]["kind"] == "call.started"
    await service.close()


async def test_external_video_opening_sends_first_sentence_then_remainder(tmp_path):
    service, ports, adapters, base, gen, output, body = await arranged(tmp_path)

    async def opening(*_args):
        return "测试官，晚上好，露米在这里。今天想聊点什么，或者有什么要我来安排的？"

    ports.opening = opening
    await service.invoke("owner", body)
    call = service.calls["call-1"]
    await call.task

    assert call.turn["speech_segments"] == [
        "测试官，晚上好，露米在这里。",
        "今天想聊点什么，或者有什么要我来安排的？",
    ]
    await service.close()


async def test_external_video_sends_first_sentence_then_batches_the_remainder(tmp_path):
    service, ports, adapters, base, gen, output, body = await arranged(tmp_path)
    first = "看见了，测试官，这个我看得挺清楚。"
    remainder = (
        "你手里这包是 ChaCha 的藤椒瓜子，绿色包装，上面印着一根大瓜子和几粒青花椒，"
        "还写着藤椒瓜子和 Szechuan Pepper Flavor，净含量 5.64 盎司，"
        "换算下来大概 160 克。"
        "这和咱们前两天聊的藤椒口味正好对上，"
        "看来你是把当时说的那包直接拿到镜头前给我看了。"
    )
    observed = []
    original_synthesize = gen.synthesize
    calls = 0
    released = asyncio.Event()
    first_started = asyncio.Event()
    remainder_started = asyncio.Event()
    release_first = asyncio.Event()
    expected_utterances = [
        "utt-" + hashlib.sha256(f"call-1-turn-1-{index}".encode()).hexdigest()[:48]
        for index in (0, 1)
    ]

    async def synthesize(selection, reference, text, **kwargs):
        observed.append(text)
        if text == first:
            first_started.set()
            await release_first.wait()
        elif text == remainder:
            remainder_started.set()
        return await original_synthesize(selection, reference, text, **kwargs)

    def result(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "terminal": False,
                "state": "running",
                "text": "",
                "activity_sequence": 2,
                "events": [
                    {
                        "sequence": 2,
                        "delivery_class": "answer_preview",
                        "presentation_channel": "answer",
                        "summary": first + "你手里这包是 ChaCha 的藤椒瓜子",
                    }
                ],
            }
        if not released.is_set():
            return {
                "terminal": False,
                "state": "running",
                "text": "",
                "activity_sequence": 2,
                "events": [],
            }
        return {
            "terminal": True,
            "state": "completed",
            "text": first + remainder,
            "activity_sequence": 3,
            "events": [],
        }

    gen.synthesize = synthesize
    ports.result = result
    try:
        info = await service.invoke("owner", {**base, "operation": "context"})
        body["generation"] = info["generation"]
        body["revision"] = info["revision"]
        await service.invoke("owner", body)
        binding = {
            **base,
            "call_id": "call-1",
            "generation": info["generation"],
        }
        await service.invoke(
            "owner",
            {
                **binding,
                "operation": "turn",
                "turn_id": "turn-1",
                "sequence": 1,
                "audio_b64": wav(),
            },
        )
        for _ in range(100):
            call = service.calls["call-1"]
            if call.turn.get("speech_segments"):
                break
            await asyncio.sleep(0.001)
        call = service.calls["call-1"]
        assert call.turn["speech_segments"] == [first]
        await asyncio.wait_for(first_started.wait(), 1)
        released.set()
        await call.task
        await asyncio.wait_for(remainder_started.wait(), 1)
        assert not set(expected_utterances).intersection(output.prepared)
        release_first.set()
        active = list(getattr(call, "speech_tasks", {}).values())
        if active:
            await asyncio.gather(*active)
        elif call.speech_task:
            await call.speech_task

        assert call.turn["speech_segments"] == [first, remainder]
        assert [text for text in observed if text in {first, remainder}] == [
            first,
            remainder,
        ]
        assert output.prepared[-2:] == expected_utterances
    finally:
        released.set()
        release_first.set()
        await service.close()


async def test_external_video_submits_every_group_concurrently_but_prepares_in_order(
    tmp_path,
):
    service, ports, adapters, base, gen, output, body = await arranged(tmp_path)
    await service.invoke("owner", body)
    call = service.calls["call-1"]
    parts = [f"第{index}组。" for index in range(5)]
    call.turn = {
        "turn_id": "ordered-groups",
        "phase": "complete",
        "speech_segments": [],
    }
    gates = {part: asyncio.Event() for part in parts}
    started = []

    async def synthesize(selection, reference, text, **kwargs):
        started.append(text)
        await gates[text].wait()
        return {
            "media": b"AV media",
            "provider_receipt": {"job_id": f"job-{parts.index(text)}"},
        }

    gen.synthesize = synthesize
    try:
        for part in parts:
            service._queue_speech(call, call.turn, part, "answer")
        for _ in range(100):
            if len(started) == len(parts):
                break
            await asyncio.sleep(0.001)
        assert started == parts

        for part in reversed(parts):
            gates[part].set()
        await asyncio.gather(*list(call.speech_tasks.values()))

        expected = [
            "utt-"
            + hashlib.sha256(f"call-1-ordered-groups-{index}".encode()).hexdigest()[:48]
            for index in range(len(parts))
        ]
        assert output.prepared[-len(parts) :] == expected
    finally:
        for gate in gates.values():
            gate.set()
        await service.close()


async def test_av_prepare_commit_interrupt_and_end_share_one_call(tmp_path):
    service, ports, adapters, base, gen, output, body = await arranged(tmp_path)
    result = await service.invoke("owner", body)
    assert result["output"]["mode"] == "external_av"
    assert output.starts == 1
    call = service.calls["call-1"]
    call.turn = {
        "turn_id": "answer",
        "phase": "complete",
        "speech_segments": ["Hello."],
    }
    service._start_speech(call, 0, "Hello.")
    await call.speech_task
    scoped = {
        **base,
        "call_id": "call-1",
        "generation": service.generation,
        "turn_id": "answer",
        "segment": 0,
    }
    speech = await service.invoke("owner", {**scoped, "operation": "speech"})
    assert (
        speech["ready"] is True and speech["output"]["audio_owner"] == "agentpresence"
    )
    assert "content_b64" not in speech
    assert speech["presentation"]["call_id"] == "call-1"
    assert output.state["replies"][0]["call_id"] == output.internal_call_id
    assert gen.references == [output.reference]
    assert adapters.tts == 0 and output.commits == 0
    assert all(e["kind"] != "call.speech" for e in ports.frontend_events)
    committed = await service.invoke(
        "owner",
        {**scoped, "operation": "speech_commit", "output_protocol": "call-output/1"},
    )
    assert committed["presentation"]["call_id"] == "call-1"
    assert output.state["replies"][0]["call_id"] == output.internal_call_id
    assert output.commits == 1
    control = {k: v for k, v in scoped.items() if k != "segment"}
    await service.invoke("owner", {**control, "operation": "interrupt"})
    assert output.interrupts == 1 and ports.cancelled == []
    await service.invoke(
        "owner",
        {
            **base,
            "call_id": "call-1",
            "generation": service.generation,
            "operation": "end",
        },
    )
    await service.close()
    assert output.ends >= 1


async def test_late_generation_after_interrupt_never_reaches_ap(tmp_path):
    service, ports, adapters, base, gen, output, body = await arranged(tmp_path)
    await service.invoke("owner", body)
    call = service.calls["call-1"]
    call.turn = {
        "turn_id": "old",
        "phase": "complete",
        "speech_segments": [],
    }
    gen.release = asyncio.Event()
    started_texts = []
    original_synthesize = gen.synthesize

    async def synthesize(selection, reference, text, **kwargs):
        started_texts.append(text)
        return await original_synthesize(selection, reference, text, **kwargs)

    gen.synthesize = synthesize
    service._queue_speech(call, call.turn, "Old opener.", "answer")
    service._queue_speech(call, call.turn, "Old remainder.", "answer")
    old_tasks = list(call.speech_tasks.values())
    for _ in range(100):
        if {"Old opener.", "Old remainder."}.issubset(started_texts):
            break
        await asyncio.sleep(0.001)
    assert {"Old opener.", "Old remainder."}.issubset(started_texts)
    await service.invoke(
        "owner",
        {
            **base,
            "call_id": "call-1",
            "generation": service.generation,
            "turn_id": "old",
            "operation": "interrupt",
        },
    )
    gen.release.set()
    await asyncio.gather(*old_tasks)
    assert output.prepared == [] and call.speech_cache == {}
    assert adapters.tts == 0 and ports.cancelled == []
    await service.close()


async def test_video_output_does_not_depend_on_unused_audio_tts_credentials(tmp_path):
    service, ports, adapters, base, gen, output, body = await arranged(tmp_path)
    adapters.target_available = lambda target: target["kind"] != "tts"
    context = await service.invoke("owner", {**base, "operation": "context"})
    assert context["call_ready"] is True
    result = await service.invoke("owner", body)
    assert result["output"]["mode"] == "external_av"
    assert output.starts == 1 and adapters.tts == 0
    await service.close()


async def test_transient_resolution_failure_starts_recovering_and_can_upgrade(tmp_path):
    service, ports, adapters, base, gen, output, body = await arranged(tmp_path)
    original_resolve = gen.resolve

    async def unavailable(*_args):
        raise CallError("call_video_service_unavailable", 503)

    gen.resolve = unavailable
    result = await service.invoke("owner", body)

    assert result["phase"] == "active"
    assert result["output"]["mode"] == "external_av"
    assert result["output"]["degraded"] is True
    assert result["output"]["degradation_reason"] == "call_video_service_unavailable"
    assert result["tts"]["mode"] == "video_tts"
    assert result["tts"]["status"] == "recovering"
    assert output.starts == 1

    gen.resolve = original_resolve
    call = service.calls["call-1"]
    call.turn = {
        "turn_id": "answer",
        "phase": "complete",
        "speech_segments": ["Service recovered."],
    }
    service._start_speech(call, 0, call.turn["speech_segments"][0])
    await call.speech_task
    speech = await service.invoke(
        "owner",
        {
            **base,
            "call_id": "call-1",
            "generation": service.generation,
            "turn_id": "answer",
            "segment": 0,
            "operation": "speech",
        },
    )
    assert speech["output"]["mode"] == "external_av"
    assert call.output.transient_reason_code == ""
    await service.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "video_error",
    ["call_video_generation_failed", "call_video_queue_timeout"],
)
async def test_video_generation_failure_uses_normal_tts_without_ending_call(
    tmp_path, video_error
):
    service, ports, adapters, base, gen, output, body = await arranged(tmp_path)
    gen.error = CallError(video_error, 502)
    original_audio = adapters.synthesize

    async def routed_audio(*args, **kwargs):
        result = await original_audio(*args, **kwargs)
        return {
            **result,
            "_tts_route": {
                "service_id": "normal-tts",
                "service_label": "Normal TTS",
            },
        }

    adapters.synthesize = routed_audio
    await service.invoke("owner", body)
    call = service.calls["call-1"]
    call.turn = {
        "turn_id": "answer",
        "phase": "complete",
        "speech_segments": ["Keep the call alive."],
    }

    service._start_speech(call, 0, call.turn["speech_segments"][0])
    await call.speech_task
    speech = await service.invoke(
        "owner",
        {
            **base,
            "call_id": "call-1",
            "generation": service.generation,
            "turn_id": "answer",
            "segment": 0,
            "operation": "speech",
        },
    )

    assert call.phase == "active"
    assert adapters.tts == 1
    assert speech["ready"] is True
    assert speech["media_type"] == "audio/wav"
    assert speech["content_b64"] == wav()
    assert speech["output"]["mode"] == "window_audio"
    assert speech["tts"]["mode"] == "audio_fallback"
    assert speech["tts"]["reason_code"] == video_error
    assert speech["tts"]["selected_service_label"] == "avatar"
    assert speech["tts"]["effective_service_id"] == "normal-tts"
    assert speech["tts"]["effective_service_label"] == "Normal TTS"
    assert output.ends == 0
    assert call.output.video_enabled is True
    assert call.output.transient_reason_code == video_error

    gen.error = None
    call.turn["speech_segments"].append("Video can recover on the next segment.")
    service._start_speech(call, 1, call.turn["speech_segments"][1])
    await call.speech_task
    recovered = await service.invoke(
        "owner",
        {
            **base,
            "call_id": "call-1",
            "generation": service.generation,
            "turn_id": "answer",
            "segment": 1,
            "operation": "speech",
        },
    )
    assert recovered["output"]["mode"] == "external_av"
    assert recovered["presentation"]["state"] == "prepared"
    assert call.output.transient_reason_code == ""
    assert adapters.tts == 1
    await service.close()


async def test_slow_video_falls_back_for_one_segment_then_late_success_recovers(
    tmp_path,
):
    service, ports, adapters, base, gen, output, body = await arranged(tmp_path)
    selection = replace(
        VideoTTSSelection.parse(SELECTION),
        wait_base_seconds=0.01,
        wait_per_char_seconds=0.0,
        wait_min_seconds=0.01,
        wait_max_seconds=0.01,
        job_timeout_seconds=1.0,
    )
    service.config.video_tts_selection = lambda revision: selection
    gen.release = asyncio.Event()
    await service.invoke("owner", body)
    call = service.calls["call-1"]
    call.turn = {
        "turn_id": "answer",
        "phase": "complete",
        "speech_segments": ["Slow segment."],
    }

    service._start_speech(call, 0, call.turn["speech_segments"][0])
    await asyncio.wait_for(gen.started.wait(), 1)
    pending = await service.invoke(
        "owner",
        {
            **base,
            "call_id": "call-1",
            "generation": service.generation,
            "turn_id": "answer",
            "segment": 0,
            "operation": "speech",
        },
    )
    assert pending["ready"] is False
    assert pending["retry_after_ms"] == 350
    assert pending["wait_remaining_ms"] >= 30000
    await call.speech_task
    fallback = await service.invoke(
        "owner",
        {
            **base,
            "call_id": "call-1",
            "generation": service.generation,
            "turn_id": "answer",
            "segment": 0,
            "operation": "speech",
        },
    )
    assert fallback["output"]["mode"] == "window_audio"
    assert fallback["tts"]["reason_code"] == "call_video_wait_timeout"
    assert call.output.video_enabled is True
    assert output.prepared == []

    gen.release.set()
    for _ in range(100):
        if not call.output.transient_reason_code:
            break
        await asyncio.sleep(0.001)
    assert call.output.transient_reason_code == ""
    assert output.prepared == [], (
        "late media is a health signal, not duplicate playback"
    )

    call.turn["speech_segments"].append("Recovered segment.")
    service._start_speech(call, 1, call.turn["speech_segments"][1])
    await call.speech_task
    recovered = await service.invoke(
        "owner",
        {
            **base,
            "call_id": "call-1",
            "generation": service.generation,
            "turn_id": "answer",
            "segment": 1,
            "operation": "speech",
        },
    )
    assert recovered["output"]["mode"] == "external_av"
    assert recovered["presentation"]["state"] == "prepared"
    assert len(output.prepared) == 1
    await service.close()


async def test_video_finishing_during_audio_fallback_is_promoted_before_publish(
    tmp_path,
):
    service, ports, adapters, base, gen, output, body = await arranged(tmp_path)
    selection = replace(
        VideoTTSSelection.parse(SELECTION),
        wait_base_seconds=0.01,
        wait_per_char_seconds=0.0,
        wait_min_seconds=0.01,
        wait_max_seconds=0.01,
        job_timeout_seconds=1.0,
    )
    service.config.video_tts_selection = lambda revision: selection
    gen.release = asyncio.Event()
    audio_started = asyncio.Event()
    audio_release = asyncio.Event()
    original_audio = adapters.synthesize

    async def slow_audio(*args):
        audio_started.set()
        await audio_release.wait()
        return await original_audio(*args)

    adapters.synthesize = slow_audio
    try:
        await service.invoke("owner", body)
        call = service.calls["call-1"]
        call.turn = {
            "turn_id": "answer",
            "phase": "complete",
            "speech_segments": ["Promote the late video."],
        }
        service._start_speech(call, 0, call.turn["speech_segments"][0])
        await asyncio.wait_for(audio_started.wait(), 1)
        gen.release.set()
        await call.speech_task
        asset = await service.invoke(
            "owner",
            {
                **base,
                "call_id": "call-1",
                "generation": service.generation,
                "turn_id": "answer",
                "segment": 0,
                "operation": "speech",
            },
        )
        assert asset["output"]["mode"] == "external_av"
        assert asset["presentation"]["state"] == "prepared"
        assert call.output.transient_reason_code == ""
        assert len(output.prepared) == 1
    finally:
        gen.release.set()
        audio_release.set()
        await service.close()


async def test_video_and_audio_failure_keeps_call_alive_with_visible_error(tmp_path):
    service, ports, adapters, base, gen, output, body = await arranged(tmp_path)
    gen.error = CallError("call_video_generation_failed", 502)
    adapters.fail_tts = True
    await service.invoke("owner", body)
    call = service.calls["call-1"]
    call.turn = {
        "turn_id": "answer",
        "phase": "complete",
        "speech_segments": ["Keep the call alive."],
    }

    service._start_speech(call, 0, call.turn["speech_segments"][0])
    await call.speech_task
    with pytest.raises(CallError, match="call_provider_rejected"):
        await service.invoke(
            "owner",
            {
                **base,
                "call_id": "call-1",
                "generation": service.generation,
                "turn_id": "answer",
                "segment": 0,
                "operation": "speech",
            },
        )
    snapshot = await service.invoke(
        "owner",
        {
            **base,
            "call_id": "call-1",
            "generation": service.generation,
            "operation": "snapshot",
        },
    )

    assert call.phase == "active"
    assert call.speech_errors[0] == "call_provider_rejected"
    assert snapshot["output"]["mode"] == "external_av"
    assert snapshot["output"]["degraded"] is True
    assert snapshot["tts"]["mode"] == "video_tts"
    assert snapshot["tts"]["status"] == "recovering"
    assert snapshot["tts"]["reason_code"] == "call_video_generation_failed"
    assert snapshot["tts"]["error_code"] == "call_provider_rejected"
    await service.close()


async def test_duplicate_start_waits_for_same_negotiated_output(tmp_path):
    service, ports, adapters, base, gen, output, body = await arranged(tmp_path)
    started, released = asyncio.Event(), asyncio.Event()

    async def delayed_start():
        output.starts += 1
        started.set()
        await released.wait()

    output.start = delayed_start
    first = asyncio.create_task(service.invoke("owner", body))
    await asyncio.wait_for(started.wait(), 2)
    duplicate = asyncio.create_task(service.invoke("owner", body))
    await asyncio.sleep(0)
    assert not duplicate.done()
    released.set()
    results = await asyncio.gather(first, duplicate)
    assert all(result["output"]["mode"] == "external_av" for result in results)
    assert output.starts == 1 and adapters.tts == 0
    await service.close()
