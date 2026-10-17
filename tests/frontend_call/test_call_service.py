import asyncio
import base64
import json
from datetime import datetime, timedelta, timezone
import pytest
from orchestrator.frontend_call.config import CallConfig, validate_target
from orchestrator.frontend_call.contract import CallError
from orchestrator.frontend_call.service import CallService
from test_call_contract import wav


def document(location="local"):
    common = {
        "adapter": "openai_compatible",
        "location": location,
        "base_url": "http://127.0.0.1:19000/v1"
        if location == "local"
        else "https://provider.example/v1",
        "model": "fixture-model",
    }
    return {
        "version": 1,
        "enabled": True,
        "targets": [
            {
                **common,
                "id": "ears",
                "kind": "stt",
                "credential_env": "TEST_CALL_TOKEN",
                "options": {"language": {"type": "string", "max_length": 8}},
            },
            {
                **common,
                "id": "mouth",
                "kind": "tts",
                "voices": ["warm", "clear"],
                "audio_format": "wav",
                "options": {"speed": {"type": "number", "min": 0.5, "max": 2}},
            },
            {**common, "id": "eyes", "kind": "vision"},
        ],
        "default_profile": {
            "stt": {"target_id": "ears", "options": {}},
            "tts": {"target_id": "mouth", "voice_id": "warm", "options": {}},
            "vision": None,
        },
        "profiles": {},
    }


class Ports:
    def __init__(self):
        self.accepted = []
        self.accepted_bindings = []
        self.accepted_frames = []
        self.persisted_openings = []
        self.cancelled = []
        self.phone = False
        self.level = 1
        self.valid = True
        self.finished = True

    def validate(self, owner, binding):
        if not self.valid or owner != "owner":
            raise CallError("call_scope_changed", 409)

    def privacy_level(self, agent):
        return self.level

    def phone_busy(self, owner):
        return self.phone

    async def admit(
        self, owner, binding, turn, text, observation, captured_at, frames=()
    ):
        self.accepted.append((turn, text, observation, captured_at))
        self.accepted_bindings.append(dict(binding))
        self.accepted_frames.append(tuple(dict(frame) for frame in frames))
        return {"run_id": "run-1", "request_id": "req-1", "message_id": "msg-1"}

    def persist_opening(self, owner, binding, text):
        self.persisted_openings.append((owner, dict(binding), text))
        return {"message_id": "opening-msg-1"}

    async def cancel(self, owner, binding, *, request_id, run_id, reason):
        self.cancelled.append(
            {
                "owner": owner,
                "agent_id": binding["agent_id"],
                "session_id": binding["session_id"],
                "request_id": request_id,
                "run_id": run_id,
                "reason": reason,
            }
        )
        return {
            "ok": True,
            "request_id": request_id,
            "run_id": run_id,
            "session_id": binding["session_id"],
            "status": "cancellation_requested",
            "terminal": False,
        }

    def result(self, *args):
        return {
            "terminal": self.finished,
            "state": "completed" if self.finished else "running",
            "text": "Hello. A bounded answer.",
        }


class Adapters:
    def __init__(self):
        self.stt = 0
        self.tts = 0
        self.fail_tts = False
        self.wait = None
        self.stt_receipt = None
        self.tts_receipt = None
        self.text = "Please check the task."
        self.stt_segments = None
        self.has_speech = None

    async def transcribe(self, *args):
        self.stt += 1
        if self.wait:
            await self.wait.wait()
        return {
            "text": self.text,
            "segments": self.stt_segments,
            "has_speech": self.has_speech,
            "provider_receipt": self.stt_receipt,
        }

    async def observe(self, *args):
        return "A book is visible."

    async def synthesize(self, *args):
        self.tts += 1
        if self.fail_tts:
            raise CallError("call_provider_rejected", 502)
        return {
            "media_type": "audio/wav",
            "content_b64": wav(),
            "provider_receipt": self.tts_receipt,
        }


def setup(tmp_path, location="local", *, startup_video_timeout=None):
    path = tmp_path / "call_profiles.json"
    path.write_text(json.dumps(document(location)))
    config = CallConfig(path)
    ports = Ports()
    adapters = Adapters()
    clock = [100.0]
    options = {}
    if startup_video_timeout is not None:
        options["startup_video_timeout"] = startup_video_timeout
    service = CallService(
        config,
        ports,
        adapters,
        clock=lambda: clock[0],
        poll_seconds=0.001,
        **options,
    )
    base = {
        "client_id": "client-1",
        "agent_id": "agent-a",
        "session_id": "sess-a",
        "context_generation": 1,
    }
    return service, ports, adapters, base, clock


async def start(service, base, cloud=False, *, video_requested=False):
    info = await service.invoke("owner", {**base, "operation": "context"})
    body = {
        **base,
        "operation": "start",
        "call_id": "call-1",
        "generation": info["generation"],
        "revision": info["revision"],
        "allow_cloud": cloud,
        **({"video_requested": True} if video_requested else {}),
    }
    result = await service.invoke("owner", body)
    return {**base, "call_id": "call-1", "generation": info["generation"]}, body, result


async def test_context_hides_call_when_selected_media_credentials_are_unavailable(
    tmp_path,
):
    service, _ports, adapters, base, _clock = setup(tmp_path)
    adapters.target_available = lambda target: target["kind"] != "stt"

    context = await service.invoke("owner", {**base, "operation": "context"})
    route = await service.invoke("owner", {**base, "operation": "route"})

    assert context["call_ready"] is False
    assert context["camera_available"] is False
    assert route["call_ready"] is False
    assert route["route"] == "phone"


async def finish_task(service):
    await service.calls["call-1"].task


async def test_start_generates_exactly_one_agent_opening_before_first_user_turn(
    tmp_path,
):
    service, ports, adapters, base, _ = setup(tmp_path)
    released = asyncio.Event()
    openings = []

    async def opening(owner, binding, profile, targets, visual_context=None):
        openings.append(
            (owner, dict(binding), dict(profile), dict(targets), visual_context)
        )
        await released.wait()
        return "（小声）您好，我接到您的电话了。请讲。"

    ports.opening = opening
    binding, start_body, first = await start(service, base)
    assert first["turn"] == {
        "turn_id": "opening-call-1",
        "sequence": 0,
        "kind": "opening",
        "phase": "thinking",
    }
    duplicate = await service.invoke("owner", start_body)
    assert duplicate["turn"] == first["turn"]
    await asyncio.sleep(0)
    assert len(openings) == 1

    released.set()
    await service.calls["call-1"].task
    final = await service.invoke("owner", {**binding, "operation": "snapshot"})
    assert final["turn"]["kind"] == "opening"
    assert final["turn"]["phase"] == "complete"
    assert final["turn"]["answer"] == "您好，我接到您的电话了。请讲。"
    assert final["rows"] == [
        {
            "speaker": "assistant",
            "text": "您好，我接到您的电话了。请讲。",
            "turn_id": "opening-call-1",
        }
    ]
    assert ports.persisted_openings == [
        ("owner", openings[0][1], "您好，我接到您的电话了。请讲。")
    ]
    assert adapters.stt == 0
    await service.close()


async def test_video_start_waits_for_first_observation_before_agent_opening(tmp_path):
    service, ports, _adapters, base, _ = setup(tmp_path)
    context = service.config.context("owner", base["agent_id"])
    context["profile"]["vision"] = {"target_id": "eyes", "options": {}}
    service.config.save(
        "owner", base["agent_id"], context["revision"], context["profile"]
    )
    openings = []

    async def opening(owner, binding, profile, targets, visual_context=None):
        openings.append(visual_context)
        return "您好，请讲。"

    ports.opening = opening
    binding, _, first = await start(service, base, video_requested=True)
    assert first["camera"]["state"] == "pending"
    assert first["camera"]["enabled"] is True
    await asyncio.sleep(0)
    assert openings == [], (
        "the greeting waits while ringback covers first-frame preparation"
    )

    captured_at = datetime.now(timezone.utc).isoformat()
    image = base64.b64encode(bytes.fromhex("ffd8ffc00008080010001000ffd9")).decode()
    await service.invoke(
        "owner",
        {
            **binding,
            "operation": "observe",
            "frame_sequence": 1,
            "image_b64": image,
            "captured_at": captured_at,
        },
    )
    await service.calls["call-1"].task

    assert len(openings) == 1
    assert openings[0]["type"] == "hashi.call-opening-visual"
    assert openings[0]["observation"] == "A book is visible."
    assert openings[0]["captured_at"] == captured_at
    final = await service.invoke("owner", {**binding, "operation": "snapshot"})
    assert final["turn"]["phase"] == "complete"
    assert final["camera"]["state"] == "fresh"
    await service.close()


async def test_video_start_timeout_falls_back_to_voice_without_ending_call(tmp_path):
    service, ports, _adapters, base, _ = setup(tmp_path, startup_video_timeout=0.01)
    context = service.config.context("owner", base["agent_id"])
    context["profile"]["vision"] = {"target_id": "eyes", "options": {}}
    service.config.save(
        "owner", base["agent_id"], context["revision"], context["profile"]
    )
    openings = []

    async def opening(owner, binding, profile, targets, visual_context=None):
        openings.append(visual_context)
        return "您好，请讲。"

    ports.opening = opening
    binding, _, _ = await start(service, base, video_requested=True)
    await service.calls["call-1"].task
    final = await service.invoke("owner", {**binding, "operation": "snapshot"})

    assert openings == [None]
    assert final["phase"] == "active"
    assert final["turn"]["phase"] == "complete"
    assert final["camera"]["state"] == "off"
    assert final["camera"]["enabled"] is False
    assert final["camera"]["reason"] == "startup_unavailable"
    await service.close()


async def test_video_start_keeps_camera_while_an_accepted_first_frame_is_processing(
    tmp_path,
):
    service, ports, adapters, base, _ = setup(tmp_path, startup_video_timeout=0.01)
    context = service.config.context("owner", base["agent_id"])
    context["profile"]["vision"] = {"target_id": "eyes", "options": {}}
    service.config.save(
        "owner", base["agent_id"], context["revision"], context["profile"]
    )
    began, release = asyncio.Event(), asyncio.Event()
    openings = []

    async def observe(*_args):
        began.set()
        await release.wait()
        return "A book is visible."

    async def opening(owner, binding, profile, targets, visual_context=None):
        openings.append(visual_context)
        return "您好，请讲。"

    adapters.observe = observe
    ports.opening = opening
    try:
        binding, _, _ = await start(service, base, video_requested=True)
        captured_at = datetime.now(timezone.utc).isoformat()
        image = base64.b64encode(bytes.fromhex("ffd8ffc00008080010001000ffd9")).decode()
        await service.invoke(
            "owner",
            {
                **binding,
                "operation": "observe",
                "frame_sequence": 1,
                "image_b64": image,
                "captured_at": captured_at,
            },
        )
        await asyncio.wait_for(began.wait(), 1)
        await asyncio.sleep(0.03)

        pending = await service.invoke("owner", {**binding, "operation": "snapshot"})
        assert pending["camera"]["enabled"] is True
        assert pending["camera"]["state"] == "pending"
        assert openings == []

        release.set()
        await service.calls["call-1"].task
        final = await service.invoke("owner", {**binding, "operation": "snapshot"})
        assert final["camera"]["state"] == "fresh"
        assert openings[0]["observation"] == "A book is visible."
    finally:
        release.set()
        await service.close()


async def test_recent_accepted_frame_keeps_stale_observation_delayed(tmp_path):
    service, _ports, adapters, base, clock = setup(tmp_path)
    context = service.config.context("owner", base["agent_id"])
    context["profile"]["vision"] = {"target_id": "eyes", "options": {}}
    service.config.save(
        "owner", base["agent_id"], context["revision"], context["profile"]
    )
    binding, _, _ = await start(service, base, video_requested=True)
    call = service.calls["call-1"]
    call.camera_had_observation = True
    call.observation = {
        "text": "An earlier image.",
        "epoch": call.camera_epoch,
        "frame_sequence": 1,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "captured_unix": datetime.now(timezone.utc).timestamp() - 21,
        "image_digest": "old",
    }
    clock[0] += 30
    began, release = asyncio.Event(), asyncio.Event()

    async def observe(*_args):
        began.set()
        await release.wait()
        return "A current image."

    adapters.observe = observe
    try:
        response = await service.invoke(
            "owner",
            {
                **binding,
                "operation": "observe",
                "frame_sequence": 2,
                "image_b64": base64.b64encode(
                    bytes.fromhex("ffd8ffc00008080010001000ffd9")
                ).decode(),
                "captured_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        assert response["camera"]["enabled"] is True
        assert response["camera"]["state"] == "delayed"
        await asyncio.wait_for(began.wait(), 1)
        release.set()
        await call.vision_task
        final = await service.invoke("owner", {**binding, "operation": "snapshot"})
        assert final["camera"]["state"] == "fresh"
    finally:
        release.set()
        await service.close()


async def test_camera_falls_back_when_frame_ingress_really_stops(tmp_path):
    service, _ports, adapters, base, clock = setup(tmp_path)
    context = service.config.context("owner", base["agent_id"])
    context["profile"]["vision"] = {"target_id": "eyes", "options": {}}
    service.config.save(
        "owner", base["agent_id"], context["revision"], context["profile"]
    )
    binding, _, _ = await start(service, base, video_requested=True)
    began, release = asyncio.Event(), asyncio.Event()

    async def observe(*_args):
        began.set()
        await release.wait()
        return "A current image."

    adapters.observe = observe
    try:
        accepted = await service.invoke(
            "owner",
            {
                **binding,
                "operation": "observe",
                "frame_sequence": 1,
                "image_b64": base64.b64encode(
                    bytes.fromhex("ffd8ffc00008080010001000ffd9")
                ).decode(),
                "captured_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        assert accepted["camera"]["state"] == "pending"
        await asyncio.wait_for(began.wait(), 1)
        clock[0] += service.calls["call-1"].video_policy["freshness_seconds"] + 1

        result = await service.invoke("owner", {**binding, "operation": "snapshot"})

        assert result["phase"] == "active"
        assert result["camera"]["enabled"] is False
        assert result["camera"]["reason"] == "frame_unavailable"
    finally:
        release.set()
        await service.close()


async def test_video_turn_freezes_ten_evenly_sampled_frames_from_last_ten_seconds(
    tmp_path,
):
    service, ports, adapters, base, clock = setup(tmp_path)
    context = service.config.context("owner", base["agent_id"])
    context["profile"]["vision"] = {"target_id": "eyes", "options": {}}
    service.config.save(
        "owner", base["agent_id"], context["revision"], context["profile"]
    )
    binding, _, _ = await start(service, base, video_requested=True)
    newest = datetime.now(timezone.utc)
    image = base64.b64encode(bytes.fromhex("ffd8ffc00008080010001000ffd9")).decode()

    for sequence in range(1, 22):
        captured_at = newest - timedelta(seconds=(21 - sequence) * 0.45)
        await service.invoke(
            "owner",
            {
                **binding,
                "operation": "observe",
                "frame_sequence": sequence,
                "image_b64": image,
                "captured_at": captured_at.isoformat(),
            },
        )
        clock[0] += 0.5

    adapters.wait = asyncio.Event()
    await service.invoke(
        "owner",
        {
            **binding,
            "operation": "turn",
            "sequence": 1,
            "turn_id": "turn-live-frames",
            "audio_b64": wav(),
        },
    )
    await asyncio.sleep(0)
    await service.invoke("owner", {**binding, "operation": "camera", "enabled": False})
    adapters.wait.set()
    await service.calls["call-1"].task

    frames = ports.accepted_frames[0]
    sequences = [frame["frame_sequence"] for frame in frames]
    assert len(frames) == 10
    assert sequences[0] == 1 and sequences[-1] == 21
    assert sequences == sorted(set(sequences))
    assert all(frame["image"].startswith(b"\xff\xd8") for frame in frames)
    assert ports.accepted_bindings[0]["call_context"]["mode"] == "video"
    assert service.calls["call-1"].frame_buffer == []
    await service.close()


def test_settings_redact_endpoints_credentials_and_check_revision(tmp_path):
    service, *_ = setup(tmp_path)
    public = service.config.context("owner", "agent-a")
    assert "base_url" not in json.dumps(public) and "TEST_CALL_TOKEN" not in json.dumps(
        public
    )
    profile = public["profile"]
    profile["tts"]["voice_id"] = "clear"
    service.config.save("owner", "agent-a", public["revision"], profile)
    with pytest.raises(CallError, match="configuration_changed"):
        service.config.save("owner", "agent-a", public["revision"], profile)
    assert (
        service.config.context("owner", "agent-a")["profile"]["tts"]["voice_id"]
        == "clear"
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"base_url": "http://evil.example/v1"},
        {"base_url": "http://127.0.0.1/v1?key=secret"},
        {"base_url": "http://user:pass@localhost/v1"},
        {"adapter": "not-implemented"},
    ],
)
def test_config_rejects_wrong_locality_and_unsupported_protocol(changes):
    target = document()["targets"][0]
    target.update(changes)
    with pytest.raises(CallError):
        validate_target(target)


def test_config_allows_literal_private_lan_targets_only():
    target = document()["targets"][2]
    target.update(
        {
            "location": "lan",
            "base_url": "http://10.23.45.67:48254/v1",
            "model": "minicpm-v-4.6",
        }
    )

    assert validate_target(target)["base_url"] == "http://10.23.45.67:48254/v1"

    for base_url in (
        "http://127.0.0.1:48254/v1",
        "http://169.254.169.254/v1",
        "http://8.8.8.8/v1",
        "http://vision.local:48254/v1",
        "ftp://10.23.45.67:48254/v1",
    ):
        with pytest.raises(CallError, match="call_lan_target_not_private"):
            validate_target({**target, "base_url": base_url})


async def test_turn_enters_agent_once_and_duplicate_upload_is_idempotent(tmp_path):
    service, ports, adapters, base, _ = setup(tmp_path)
    binding, start_body, _ = await start(service, base)
    duplicate = await service.invoke("owner", start_body)
    assert duplicate["call_id"] == "call-1"
    body = {
        **binding,
        "operation": "turn",
        "turn_id": "turn-1",
        "sequence": 1,
        "audio_b64": wav(),
    }
    await service.invoke("owner", body)
    await service.invoke("owner", body)
    await finish_task(service)
    final = await service.invoke("owner", {**binding, "operation": "snapshot"})
    assert final["turn"]["phase"] == "complete" and final["turn"]["answer"].startswith(
        "Hello"
    )
    assert adapters.stt == 1 and len(ports.accepted) == 1
    with pytest.raises(CallError, match="idempotency_conflict"):
        await service.invoke("owner", {**body, "turn_id": "different"})
    assert "audio_b64" not in json.dumps(final)
    await service.close()


async def test_new_voice_turn_is_admitted_without_cancelling_the_running_turn(tmp_path):
    service, ports, adapters, base, _ = setup(tmp_path)
    binding, _, _ = await start(service, base)
    release_first = asyncio.Event()
    first_started = asyncio.Event()
    original_transcribe = adapters.transcribe

    async def transcribe(*args):
        result = await original_transcribe(*args)
        if adapters.stt == 1:
            first_started.set()
            await release_first.wait()
        return result

    adapters.transcribe = transcribe
    first = {
        **binding,
        "operation": "turn",
        "turn_id": "turn-1",
        "sequence": 1,
        "audio_b64": wav(),
    }
    second = {
        **binding,
        "operation": "turn",
        "turn_id": "turn-2",
        "sequence": 2,
        "audio_b64": wav(),
    }
    try:
        await service.invoke("owner", first)
        await first_started.wait()
        accepted = await service.invoke("owner", second)
        assert accepted["turn"]["turn_id"] == "turn-2"
        assert accepted["turn"]["phase"] == "transcribing"
        assert service.calls["call-1"].sequence == 2
        assert ports.cancelled == []
        with pytest.raises(CallError, match="call_turn_id_conflict"):
            await service.invoke("owner", {**second, "sequence": 3})
        assert service.calls["call-1"].sequence == 2
        release_first.set()
        await asyncio.gather(*service.calls["call-1"].turn_tasks.values())
        assert [item[0] for item in ports.accepted] == ["turn-2", "turn-1"]
        assert ports.cancelled == []
        assert service.calls["call-1"].turn["turn_id"] == "turn-2"
    finally:
        release_first.set()
        await service.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "annotation",
    [
        "*Loud sound*",
        " [noise] ",
        "*Music*",
        "**Loud sound**",
        "*Coughing* *Loud sound*",
        "*Loud sound*.",
        "[noise], [music].",
        "*Loud sound.*",
    ],
)
async def test_non_speech_annotation_skips_agent_and_speech_then_accepts_next_turn(
    tmp_path, annotation
):
    service, ports, adapters, base, _ = setup(tmp_path)
    binding, _, _ = await start(service, base)
    body = {
        **binding,
        "operation": "turn",
        "turn_id": "noise-1",
        "sequence": 1,
        "audio_b64": wav(),
    }
    adapters.text = annotation
    try:
        await service.invoke("owner", body)
        await service.invoke("owner", body)
        await finish_task(service)
        final = await service.invoke("owner", {**binding, "operation": "snapshot"})
        assert final["phase"] == "active"
        assert final["turn"]["phase"] == "ignored"
        assert final["turn"]["ignore_reason"] == "non_speech_annotation"
        assert final["rows"] == []
        assert "text" not in final["turn"] and "run_id" not in final["turn"]
        assert not ports.accepted and adapters.tts == 0
        duplicate = await service.invoke("owner", body)
        assert duplicate["turn"]["phase"] == "ignored" and adapters.stt == 1
        with pytest.raises(CallError, match="idempotency_conflict"):
            await service.invoke("owner", {**body, "turn_id": "different"})
        with pytest.raises(CallError, match="speech_not_ready"):
            await service.invoke(
                "owner",
                {**binding, "operation": "speech", "turn_id": "noise-1", "segment": 0},
            )

        adapters.text = "停"
        await service.invoke("owner", {**body, "turn_id": "speech-2", "sequence": 2})
        await finish_task(service)
        await service.calls["call-1"].speech_task
        final = await service.invoke("owner", {**binding, "operation": "snapshot"})
        assert final["turn"]["phase"] == "complete"
        assert [row["speaker"] for row in final["rows"]] == ["user", "assistant"]
        assert ports.accepted == [("speech-2", "停", "", None)]
        assert adapters.stt == 2 and adapters.tts == 1
    finally:
        await service.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "spoken_text, expected",
    [
        ("Loud sound", "Loud sound"),
        ("嗯", "嗯"),
        ("停", "停"),
        ("*Loud sound* 我说24", "我说24"),
        ("[noise] please stop", "please stop"),
        ("*please stop*", "*please stop*"),
    ],
)
async def test_spoken_text_and_mixed_annotations_are_preserved(
    tmp_path, spoken_text, expected
):
    service, ports, adapters, base, _ = setup(tmp_path)
    binding, _, _ = await start(service, base)
    adapters.text = spoken_text
    try:
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
        await finish_task(service)
        await service.calls["call-1"].speech_task
        final = await service.invoke("owner", {**binding, "operation": "snapshot"})
        assert final["turn"]["phase"] == "complete"
        assert final["rows"][0]["text"] == expected
        assert ports.accepted == [("turn-1", expected, "", None)]
        assert adapters.tts == 1
    finally:
        await service.close()


async def test_noise_completion_after_scope_change_remains_a_failure(tmp_path):
    service, ports, adapters, base, _ = setup(tmp_path)
    binding, _, _ = await start(service, base)
    adapters.text = "*Loud sound*"
    adapters.wait = asyncio.Event()
    try:
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
        await asyncio.sleep(0)
        ports.valid = False
        adapters.wait.set()
        await finish_task(service)
        turn = service.calls["call-1"].turn
        assert turn["phase"] == "failed" and turn["error"] == "call_scope_changed"
        assert not ports.accepted and adapters.tts == 0
    finally:
        await service.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "text, segments, has_speech",
    [
        ("", None, None),
        ("   ", None, None),
        (
            "Thank you for watching.",
            [{"no_speech_prob": 0.96, "avg_logprob": -2.0}],
            None,
        ),
        ("A noise description", None, False),
    ],
)
async def test_cloud_no_speech_skips_agent_and_tts_without_failing_the_call(
    tmp_path, text, segments, has_speech
):
    service, ports, adapters, base, _ = setup(tmp_path)
    binding, _, _ = await start(service, base)
    adapters.text, adapters.stt_segments = text, segments
    adapters.has_speech = has_speech
    try:
        await service.invoke(
            "owner",
            {
                **binding,
                "operation": "turn",
                "turn_id": "noise-1",
                "sequence": 1,
                "audio_b64": wav(),
            },
        )
        await finish_task(service)
        final = await service.invoke("owner", {**binding, "operation": "snapshot"})
        assert final["phase"] == "active" and final["turn"]["phase"] == "ignored"
        assert final["turn"]["ignore_reason"] == "no_speech"
        assert final["rows"] == [] and "text" not in final["turn"]
        assert not ports.accepted and adapters.tts == 0
        adapters.text, adapters.stt_segments = "嗯", None
        adapters.has_speech = True
        await service.invoke(
            "owner",
            {
                **binding,
                "operation": "turn",
                "turn_id": "speech-2",
                "sequence": 2,
                "audio_b64": wav(),
            },
        )
        await finish_task(service)
        await service.calls["call-1"].speech_task
        assert ports.accepted == [("speech-2", "嗯", "", None)]
    finally:
        await service.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "segments",
    [
        None,
        [],
        [{"no_speech_prob": 0.99}],
        [{"no_speech_prob": 0.99, "avg_logprob": -0.1}],
        [
            {"no_speech_prob": 0.99, "avg_logprob": -2},
            {"no_speech_prob": 0.1, "avg_logprob": -0.1},
        ],
        [{"no_speech_prob": float("nan"), "avg_logprob": -2}],
    ],
)
async def test_uncertain_or_mixed_cloud_evidence_retains_a_short_spoken_word(
    tmp_path, segments
):
    service, ports, adapters, base, _ = setup(tmp_path)
    binding, _, _ = await start(service, base)
    adapters.text, adapters.stt_segments = "停", segments
    try:
        await service.invoke(
            "owner",
            {
                **binding,
                "operation": "turn",
                "turn_id": "speech-1",
                "sequence": 1,
                "audio_b64": wav(),
            },
        )
        await finish_task(service)
        await service.calls["call-1"].speech_task
        assert ports.accepted == [("speech-1", "停", "", None)]
        assert adapters.tts == 1
    finally:
        await service.close()


async def test_verified_media_providers_follow_the_turn_and_speech_result(tmp_path):
    service, _ports, adapters, base, _clock = setup(tmp_path)
    adapters.stt_receipt = {
        "gateway": "OpenRouter",
        "actual_provider": "Groq",
        "verification": "verified",
    }
    adapters.tts_receipt = {
        "gateway": "OpenRouter",
        "actual_provider": "Google AI Studio",
        "verification": "verified",
    }
    binding, _, _ = await start(service, base)
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
    await finish_task(service)
    speech = {**binding, "operation": "speech", "turn_id": "turn-1", "segment": 0}
    await service.invoke("owner", speech)
    await service.calls["call-1"].speech_task
    snapshot = await service.invoke("owner", {**binding, "operation": "snapshot"})
    receipts = snapshot["turn"]["provider_receipts"]
    assert receipts["stt"]["actual_provider"] == "Groq"
    assert receipts["tts"]["0"]["actual_provider"] == "Google AI Studio"
    assert (await service.invoke("owner", speech))["provider_receipt"] == (
        adapters.tts_receipt
    )
    await service.close()


async def test_scope_and_owner_fences_prevent_delivery(tmp_path):
    service, ports, adapters, base, _ = setup(tmp_path)
    binding, _, _ = await start(service, base)
    for patch in [
        {"client_id": "another"},
        {"session_id": "other"},
        {"generation": "old"},
    ]:
        with pytest.raises(CallError):
            await service.invoke("owner", {**binding, "operation": "snapshot", **patch})
    with pytest.raises(CallError):
        await service.invoke("other", {**binding, "operation": "snapshot"})
    adapters.wait = asyncio.Event()
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
    await asyncio.sleep(0)
    ports.valid = False
    adapters.wait.set()
    await finish_task(service)
    assert not ports.accepted
    await service.close()


async def test_cloud_requires_qualified_privacy_at_each_stage(tmp_path):
    service, ports, adapters, base, _ = setup(tmp_path, "cloud")
    ports.level = None
    with pytest.raises(CallError, match="privacy"):
        await start(service, base, True)
    ports.level = 1
    binding, _, _ = await start(service, base, True)
    ports.level = 2
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
    await finish_task(service)
    assert adapters.stt == 0 and not ports.accepted
    await service.close()


async def test_speech_retry_never_repeats_agent_run(tmp_path):
    service, ports, adapters, base, _ = setup(tmp_path)
    adapters.fail_tts = True
    binding, _, _ = await start(service, base)
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
    await finish_task(service)
    speech = {**binding, "operation": "speech", "turn_id": "turn-1", "segment": 0}
    assert service.calls["call-1"].speech_task is not None
    await service.calls["call-1"].speech_task
    with pytest.raises(CallError):
        await service.invoke("owner", speech)
    adapters.fail_tts = False
    await service.invoke("owner", {**speech, "retry": True})
    await service.calls["call-1"].speech_task
    assert (await service.invoke("owner", speech))["ready"]
    assert (await service.invoke("owner", speech))["ready"]
    assert adapters.tts == 2 and len(ports.accepted) == 1
    await service.close()


async def test_answer_starts_speech_before_client_poll_and_reuses_it(tmp_path):
    service, ports, adapters, base, _ = setup(tmp_path)
    started, release = asyncio.Event(), asyncio.Event()

    async def synthesize(*args):
        adapters.tts += 1
        started.set()
        await release.wait()
        return {"media_type": "audio/wav", "content_b64": wav()}

    adapters.synthesize = synthesize
    binding, _, _ = await start(service, base)
    try:
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
        await finish_task(service)
        call = service.calls["call-1"]
        assert call.speech_task is not None
        await asyncio.wait_for(started.wait(), 1)
        assert call.turn["phase"] == "complete"
        speech = {**binding, "operation": "speech", "turn_id": "turn-1", "segment": 0}
        assert not (await service.invoke("owner", speech))["ready"]
        assert not (await service.invoke("owner", speech))["ready"]
        release.set()
        await call.speech_task
        assert (await service.invoke("owner", speech))["ready"]
        assert (await service.invoke("owner", speech))["ready"]
        assert adapters.tts == 1 and len(ports.accepted) == 1
    finally:
        release.set()
        await service.close()


async def test_requested_segment_warms_only_one_queued_segment_without_next_poll(
    tmp_path,
):
    service, _ports, adapters, base, _ = setup(tmp_path)
    first_started, release_first = asyncio.Event(), asyncio.Event()
    synthesized = []

    async def synthesize(_target, _profile, text):
        synthesized.append(text)
        if len(synthesized) == 1:
            first_started.set()
            await release_first.wait()
        return {"media_type": "audio/wav", "content_b64": wav()}

    adapters.synthesize = synthesize
    binding, _, _ = await start(service, base)
    call = service.calls["call-1"]
    call.turn = {
        "turn_id": "turn-1",
        "sequence": 1,
        "phase": "thinking",
        "speech_segments": [],
    }
    try:
        service._queue_speech(call, call.turn, "First sentence.", "commentary")
        first_task = call.speech_task
        await asyncio.wait_for(first_started.wait(), 1)

        speech0 = {
            **binding,
            "operation": "speech",
            "turn_id": "turn-1",
            "segment": 0,
        }
        assert not (await service.invoke("owner", speech0))["ready"]
        assert call.speech_requested_index == 0

        service._queue_speech(call, call.turn, "Second sentence.", "answer")
        service._queue_speech(call, call.turn, "Third sentence.", "answer")
        await asyncio.sleep(0)
        assert synthesized == ["First sentence."]

        release_first.set()
        await first_task
        second_task = call.speech_task
        assert second_task is not first_task
        await second_task
        assert synthesized == ["First sentence.", "Second sentence."]
        assert set(call.speech_cache) == {0, 1}

        speech1 = {**speech0, "segment": 1}
        assert (await service.invoke("owner", speech1))["ready"]
        await call.speech_task
        assert synthesized == [
            "First sentence.",
            "Second sentence.",
            "Third sentence.",
        ]
        assert set(call.speech_cache) == {1, 2}
    finally:
        release_first.set()
        await service.close()


async def test_automatic_speech_never_publishes_after_call_changes(tmp_path):
    for change in ("end", "turn", "scope"):
        case_path = tmp_path / change
        case_path.mkdir()
        await _automatic_speech_after_change(case_path, change)


async def _automatic_speech_after_change(tmp_path, change):
    service, ports, adapters, base, _ = setup(tmp_path)
    started, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def synthesize(*args):
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            # A provider may already have completed when cancellation arrives.
            cancelled.set()
            await release.wait()
        return {"media_type": "audio/wav", "content_b64": wav()}

    adapters.synthesize = synthesize
    binding, _, _ = await start(service, base)
    try:
        turn = {
            **binding,
            "operation": "turn",
            "turn_id": "turn-1",
            "sequence": 1,
            "audio_b64": wav(),
        }
        await service.invoke("owner", turn)
        await finish_task(service)
        call = service.calls["call-1"]
        assert call.speech_task is not None
        await asyncio.wait_for(started.wait(), 1)
        old_speech = call.speech_task
        if change == "end":
            await service.invoke("owner", {**binding, "operation": "end"})
        elif change == "turn":
            adapters.wait = asyncio.Event()
            await service.invoke("owner", {**turn, "turn_id": "turn-2", "sequence": 2})
        else:
            ports.valid = False
        if change != "scope":
            await asyncio.wait_for(cancelled.wait(), 1)
        release.set()
        await old_speech
        assert not call.speech_cache
        if change == "turn":
            assert call.turn["turn_id"] == "turn-2" and not call.speech_errors
    finally:
        release.set()
        await service.close()


async def test_automatic_speech_checks_privacy_without_failing_text_answer(tmp_path):
    service, ports, adapters, base, _ = setup(tmp_path, "cloud")
    binding, _, _ = await start(service, base, True)
    original_result = ports.result

    def result(*args):
        answer = original_result(*args)
        ports.level = 2
        return answer

    ports.result = result
    try:
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
        await finish_task(service)
        call = service.calls["call-1"]
        assert call.speech_task is not None
        await call.speech_task
        assert call.turn["phase"] == "complete" and call.turn["answer"]
        assert adapters.tts == 0 and not call.speech_cache
        assert "privacy" in call.speech_errors[0]
    finally:
        await service.close()


async def test_expired_call_does_not_start_automatic_speech_after_agent_finishes(
    tmp_path,
):
    service, ports, adapters, base, clock = setup(tmp_path)
    binding, _, _ = await start(service, base)
    original_result = ports.result

    def result(*args):
        answer = original_result(*args)
        clock[0] += 46  # The frontend stopped renewing this call's lease.
        return answer

    ports.result = result
    try:
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
        call = service.calls["call-1"]
        await asyncio.gather(call.task, return_exceptions=True)
        if call.speech_task:
            await asyncio.gather(call.speech_task, return_exceptions=True)
        assert call.phase == "ended"
        assert adapters.tts == 0 and not call.speech_cache
        assert len(ports.accepted) == 1
    finally:
        await service.close()


async def test_privacy_revocation_discards_inflight_and_cached_speech(tmp_path):
    for moment in ("inflight", "cached"):
        case_path = tmp_path / moment
        case_path.mkdir()
        service, ports, adapters, base, _ = setup(case_path, "cloud")
        started, release = asyncio.Event(), asyncio.Event()

        async def synthesize(*args):
            adapters.tts += 1
            started.set()
            await release.wait()
            return {"media_type": "audio/wav", "content_b64": wav()}

        adapters.synthesize = synthesize
        binding, _, _ = await start(service, base, True)
        try:
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
            await finish_task(service)
            call = service.calls["call-1"]
            await asyncio.wait_for(started.wait(), 1)
            if moment == "inflight":
                ports.level = 2
            release.set()
            await call.speech_task
            if moment == "cached":
                assert call.speech_cache
                ports.level = 2
            speech = {
                **binding,
                "operation": "speech",
                "turn_id": "turn-1",
                "segment": 0,
            }
            with pytest.raises(CallError, match="privacy"):
                await service.invoke("owner", speech)
            assert not call.speech_cache and call.turn["phase"] == "complete"
            ports.level = 1
            with pytest.raises(CallError, match="privacy"):
                await service.invoke("owner", speech)
            await service.invoke("owner", {**speech, "retry": True})
            await call.speech_task
            assert (await service.invoke("owner", speech))["ready"]
            assert adapters.tts == 2 and len(ports.accepted) == 1
        finally:
            release.set()
            await service.close()


async def test_privacy_revocation_in_later_segment_invalidates_cached_audio(tmp_path):
    service, ports, adapters, base, _ = setup(tmp_path, "cloud")
    started, release = asyncio.Event(), asyncio.Event()
    original_result = ports.result

    def result(*args):
        return {**original_result(*args), "text": "A" * 600 + "B" * 100}

    async def synthesize(*args):
        adapters.tts += 1
        if adapters.tts == 2:
            started.set()
            await release.wait()
        return {"media_type": "audio/wav", "content_b64": wav()}

    ports.result = result
    adapters.synthesize = synthesize
    binding, _, _ = await start(service, base, True)
    try:
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
        await finish_task(service)
        call = service.calls["call-1"]
        await call.speech_task
        speech = {**binding, "operation": "speech", "turn_id": "turn-1", "segment": 0}
        assert len(call.turn["speech_segments"]) >= 2
        assert (await service.invoke("owner", speech))["ready"]
        await service.invoke("owner", {**speech, "segment": 1})
        await asyncio.wait_for(started.wait(), 1)
        ports.level = 2
        release.set()
        await call.speech_task
        assert not call.speech_cache
        assert set(call.speech_errors) == {0, 1}
        assert call.turn["phase"] == "complete" and call.turn["answer"]
        ports.level = 1
        with pytest.raises(CallError, match="privacy"):
            await service.invoke("owner", speech)
        await service.invoke("owner", {**speech, "retry": True})
        await call.speech_task
        assert (await service.invoke("owner", speech))["ready"]
        assert adapters.tts == 3 and len(ports.accepted) == 1
    finally:
        release.set()
        await service.close()


async def test_rejected_speech_retry_preserves_recovery_after_privacy_returns(tmp_path):
    service, ports, adapters, base, _ = setup(tmp_path, "cloud")
    binding, _, _ = await start(service, base, True)
    original_result = ports.result

    def result(*args):
        answer = original_result(*args)
        ports.level = 2
        return answer

    ports.result = result
    try:
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
        await finish_task(service)
        call = service.calls["call-1"]
        await call.speech_task
        speech = {**binding, "operation": "speech", "turn_id": "turn-1", "segment": 0}
        with pytest.raises(CallError, match="privacy"):
            await service.invoke("owner", {**speech, "retry": True})
        assert call.speech_errors[0] == "call_cloud_privacy_unqualified"
        assert call.speech_attempts[0] == 1 and adapters.tts == 0
        ports.level = 1
        await service.invoke("owner", {**speech, "retry": True})
        await call.speech_task
        assert (await service.invoke("owner", speech))["ready"]
        assert call.turn["phase"] == "complete"
        assert adapters.tts == 1 and len(ports.accepted) == 1
    finally:
        await service.close()


async def test_phone_busy_and_end_lease_are_separate_from_agent_lifecycle(tmp_path):
    service, ports, adapters, base, clock = setup(tmp_path)
    ports.phone = True
    with pytest.raises(CallError, match="already_active"):
        await start(service, base)
    ports.phone = False
    binding, _, _ = await start(service, base)
    assert service.busy("owner")
    ports.finished = False
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
    await asyncio.sleep(0.01)
    assert len(ports.accepted) == 1
    clock[0] += 46
    service.expire()
    assert not service.busy("owner")
    assert (await service.invoke("owner", {**binding, "operation": "end"}))[
        "phase"
    ] == "ended"
    assert len(ports.accepted) == 1
    await service.close()


async def test_unknown_fields_never_reach_adapters(tmp_path):
    service, ports, adapters, base, _ = setup(tmp_path)
    for field in ["owner_id", "api_key", "base_url", "provider_session_id"]:
        with pytest.raises(CallError, match="unknown_field"):
            await service.invoke(
                "owner", {**base, "operation": "context", field: "injected"}
            )
    assert not adapters.stt
