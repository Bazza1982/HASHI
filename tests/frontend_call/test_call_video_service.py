"""External AV must fence presentation without changing PAO input ownership."""

import asyncio
import pytest
from orchestrator.frontend_call.contract import CallError
from orchestrator.frontend_call.video_tts import VideoTTSSelection
from test_call_service import setup

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
        "speech_segments": ["Old reply."],
    }
    gen.release = asyncio.Event()
    service._start_speech(call, 0, "Old reply.")
    old_task = call.speech_task
    await asyncio.wait_for(gen.started.wait(), 2)
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
    await old_task
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
