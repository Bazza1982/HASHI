import asyncio
import base64
import json
from datetime import datetime, timezone, timedelta
import pytest

from test_call_service import setup, start
from orchestrator.frontend_call.contract import CallError


def test_video_policy_has_a_human_readable_readiness_window(tmp_path):
    service, *_ = setup(tmp_path)
    policy = service.config.video_policy()
    assert policy["freshness_seconds"] == 20
    assert policy["responsive_seconds"] == 12
    assert policy["startup_timeout_seconds"] == 8
    assert policy["interval_ms"] == 500
    assert policy["max_observations_per_minute"] == 120


def test_call_readiness_defaults_to_call_and_keeps_explicit_phone(tmp_path):
    service, *_ = setup(tmp_path)
    config = service.config
    assert config.route("owner", "agent-a")["route"] == "call"
    context = config.context("owner", "agent-a")
    config.save("owner", "agent-a", context["revision"], context["profile"])
    assert config.route("owner", "agent-a")["route"] == "call"
    context = config.context("owner", "agent-a")
    context["profile"]["vision"] = {"target_id": "eyes", "options": {}}
    config.save("owner", "agent-a", context["revision"], context["profile"])
    readiness = config.route("owner", "agent-a")
    assert readiness["call_ready"] is True and readiness["camera_available"] is True
    context = config.context("owner", "agent-a")
    config.select_route("owner", "agent-a", context["revision"], "phone")
    assert type(config)(config.path).route("owner", "agent-a")["route"] == "phone"
    assert config.route("owner", "other")["route"] == "call"
    with pytest.raises(CallError, match="configuration_changed"):
        config.select_route("owner", "agent-a", context["revision"], "call")


async def test_direct_call_starts_without_switching_configuration_and_camera_is_manual(
    tmp_path,
):
    service, _, _, base, _ = setup(tmp_path)
    context = service.config.context("owner", base["agent_id"])
    context["profile"]["vision"] = {"target_id": "eyes", "options": {}}
    service.config.save(
        "owner", base["agent_id"], context["revision"], context["profile"]
    )
    before = service.config.path.read_bytes()
    info = await service.invoke("owner", {**base, "operation": "context"})
    assert info["route"] == "call" and info["call_ready"] is True
    body = {
        **base,
        "operation": "start",
        "call_id": "call-1",
        "generation": info["generation"],
        "revision": info["revision"],
    }
    try:
        result = await service.invoke("owner", body)
        assert result["phase"] == "active" and result["camera"]["enabled"] is False
        assert service.config.path.read_bytes() == before
        camera = await service.invoke(
            "owner",
            {
                **base,
                "operation": "camera",
                "call_id": "call-1",
                "generation": info["generation"],
                "enabled": True,
            },
        )
        assert camera["camera"]["enabled"] is True
        assert service.config.path.read_bytes() == before
    finally:
        await service.close()


async def test_disabled_call_context_keeps_minimal_phone_readiness(tmp_path):
    service, _, _, base, _ = setup(tmp_path)
    doc = json.loads(service.config.path.read_text())
    doc["enabled"] = False
    service.config.path.write_text(json.dumps(doc))
    info = await service.invoke("owner", {**base, "operation": "context"})
    assert info["route"] == "phone" and info["call_ready"] is False
    assert (
        info["camera_available"] is False and info["video_policy"]["interval_ms"] >= 500
    )
    assert "profile" not in info and "targets" not in info
    await service.close()


async def test_configured_cloud_start_needs_no_frontend_consent_and_preserves_privacy(
    tmp_path,
):
    service, ports, _, base, _ = setup(tmp_path, "cloud")
    info = await service.invoke("owner", {**base, "operation": "context"})
    service.config.select_route("owner", base["agent_id"], info["revision"], "call")
    info = await service.invoke("owner", {**base, "operation": "context"})
    body = {
        **base,
        "operation": "start",
        "call_id": "call-1",
        "generation": info["generation"],
        "revision": info["revision"],
    }
    result = await service.invoke("owner", body)
    assert result["phase"] == "active"
    await service.close()
    (tmp_path / "private").mkdir()
    service, ports, _, base, _ = setup(tmp_path / "private", "cloud")
    ports.level = 2
    info = await service.invoke("owner", {**base, "operation": "context"})
    service.config.select_route("owner", base["agent_id"], info["revision"], "call")
    info = await service.invoke("owner", {**base, "operation": "context"})
    with pytest.raises(CallError, match="privacy"):
        await service.invoke(
            "owner",
            {**body, "generation": info["generation"], "revision": info["revision"]},
        )
    await service.close()


async def test_live_video_publishes_completed_fresh_frame_while_newer_frame_is_pending(
    tmp_path,
):
    service, _, adapters, base, clock = setup(tmp_path)
    info = service.config.context("owner", "agent-a")
    info["profile"]["vision"] = {"target_id": "eyes", "options": {}}
    service.config.save("owner", "agent-a", info["revision"], info["profile"])
    binding, _, _ = await start(service, base)
    await service.invoke("owner", {**binding, "operation": "camera", "enabled": True})
    first, second = asyncio.Event(), asyncio.Event()
    launches = []

    async def observe(*args):
        launches.append(args)
        await (first if len(launches) == 1 else second).wait()
        return "A visible cup."

    adapters.observe = observe
    for sequence, jpeg in (
        (1, "ffd8ffc00008080010001000ffd9"),
        (2, "ffd8ffc00008080011001000ffd9"),
    ):
        clock[0] += 2
        await service.invoke(
            "owner",
            {
                **binding,
                "operation": "observe",
                "frame_sequence": sequence,
                "image_b64": base64.b64encode(bytes.fromhex(jpeg)).decode(),
                "captured_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        await asyncio.sleep(0)
    first.set()
    await asyncio.sleep(0)
    view = await service.invoke("owner", {**binding, "operation": "snapshot"})
    assert (
        view["camera"]["state"] == "fresh" and view["camera"]["observed_sequence"] == 1
    )
    assert len(launches) == 2
    await service.close()


async def test_silent_video_latest_frame_is_bounded_and_camera_off_discards_late_observation(
    tmp_path,
):
    service, ports, adapters, base, clock = setup(tmp_path)
    info = service.config.context("owner", "agent-a")
    info["profile"]["vision"] = {"target_id": "eyes", "options": {}}
    service.config.save("owner", "agent-a", info["revision"], info["profile"])
    binding, _, _ = await start(service, base)
    await service.invoke("owner", {**binding, "operation": "camera", "enabled": True})
    # Structurally valid bounded JPEG fixture used by the public media validator.
    image = base64.b64encode(bytes.fromhex("ffd8ffc00008080010001000ffd9")).decode()
    waiting = asyncio.Event()
    calls = []

    async def observe(*args):
        calls.append(args)
        await waiting.wait()
        return "A raised hand is visible."

    adapters.observe = observe

    def frame(sequence):
        return {
            **binding,
            "operation": "observe",
            "frame_sequence": sequence,
            "image_b64": image,
            "captured_at": datetime.now(timezone.utc).isoformat(),
        }

    await service.invoke("owner", frame(1))
    await asyncio.sleep(0)
    for seq in range(2, 20):
        clock[0] += 0.1
        await service.invoke("owner", frame(seq))
    assert len(calls) == 1 and not ports.accepted
    call = service.calls["call-1"]
    assert call.vision_pending["frame_sequence"] == 19
    await service.invoke("owner", {**binding, "operation": "camera", "enabled": False})
    waiting.set()
    await asyncio.sleep(0)
    view = await service.invoke("owner", {**binding, "operation": "snapshot"})
    assert view["camera"]["state"] == "off"
    assert not call.vision_pending and not call.observation
    await service.close()


async def test_one_failed_observation_keeps_the_last_usable_agent_view(tmp_path):
    service, _, adapters, base, clock = setup(tmp_path)
    info = service.config.context("owner", "agent-a")
    info["profile"]["vision"] = {"target_id": "eyes", "options": {}}
    service.config.save("owner", "agent-a", info["revision"], info["profile"])
    binding, _, _ = await start(service, base)
    await service.invoke("owner", {**binding, "operation": "camera", "enabled": True})

    def frame(sequence, jpeg):
        return {
            **binding,
            "operation": "observe",
            "frame_sequence": sequence,
            "image_b64": base64.b64encode(bytes.fromhex(jpeg)).decode(),
            "captured_at": datetime.now(timezone.utc).isoformat(),
        }

    await service.invoke("owner", frame(1, "ffd8ffc00008080010001000ffd9"))
    await service.calls["call-1"].vision_task

    async def fail(*_args):
        raise CallError("call_provider_rejected", 502)

    adapters.observe = fail
    clock[0] += 2
    await service.invoke("owner", frame(2, "ffd8ffc00008080011001000ffd9"))
    await service.calls["call-1"].vision_task
    view = await service.invoke("owner", {**binding, "operation": "snapshot"})

    assert view["camera"]["state"] == "fresh"
    assert view["camera"]["observed_sequence"] == 1
    assert view["camera"]["error"] == "call_provider_rejected"
    await service.close()


async def test_agent_view_reports_delay_then_falls_back_when_frame_ingress_stops(
    tmp_path,
):
    service, _, _, base, clock = setup(tmp_path)
    info = service.config.context("owner", "agent-a")
    info["profile"]["vision"] = {"target_id": "eyes", "options": {}}
    service.config.save("owner", "agent-a", info["revision"], info["profile"])
    binding, _, _ = await start(service, base)
    await service.invoke("owner", {**binding, "operation": "camera", "enabled": True})
    image = base64.b64encode(bytes.fromhex("ffd8ffc00008080010001000ffd9")).decode()
    await service.invoke(
        "owner",
        {
            **binding,
            "operation": "observe",
            "frame_sequence": 1,
            "image_b64": image,
            "captured_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    await service.calls["call-1"].vision_task

    call = service.calls["call-1"]
    delayed = datetime.now(timezone.utc) - timedelta(seconds=13)
    call.observation.update(
        captured_at=delayed.isoformat(), captured_unix=delayed.timestamp()
    )
    view = await service.invoke("owner", {**binding, "operation": "snapshot"})
    assert view["camera"]["state"] == "delayed"
    assert view["camera"]["enabled"] is True

    expired = datetime.now(timezone.utc) - timedelta(seconds=21)
    call.observation.update(
        captured_at=expired.isoformat(), captured_unix=expired.timestamp()
    )
    clock[0] += 21
    view = await service.invoke("owner", {**binding, "operation": "snapshot"})
    assert view["phase"] == "active"
    assert view["camera"]["state"] == "off"
    assert view["camera"]["reason"] == "frame_unavailable"
    await service.close()


async def test_selected_slow_camera_policy_survives_projection_and_stale_frames_are_rejected(
    tmp_path,
):
    from orchestrator.frontend_call.context import project_call_context

    service, _ports, _adapters, base, clock = setup(tmp_path)
    doc = json.loads(service.config.path.read_text())
    doc["default_profile"]["vision"] = {"target_id": "eyes", "options": {}}
    doc["video_policy"] = {
        "interval_ms": 15000,
        "freshness_seconds": 60,
        "responsive_seconds": 40,
        "startup_timeout_seconds": 45,
        "max_observations_per_minute": 200,
        "sample_window_seconds": 10,
        "max_frames_per_turn": 10,
        "max_buffer_frames": 32,
        "max_buffer_bytes": 0,
    }
    service.config.path.write_text(json.dumps(doc))
    try:
        info = await service.invoke("owner", {**base, "operation": "context"})
        assert info["video_policy"] == doc["video_policy"]
        binding, _, _ = await start(service, base, video_requested=True)
        call = service.calls["call-1"]
        assert call.video_policy == doc["video_policy"]

        clock[0] += 30
        pending = await service.invoke("owner", {**binding, "operation": "snapshot"})
        assert pending["camera"]["state"] == "pending"
        captured = datetime.now(timezone.utc) - timedelta(seconds=45)
        frame = {
            **binding,
            "operation": "observe",
            "frame_sequence": 1,
            "image_b64": base64.b64encode(
                bytes.fromhex("ffd8ffc00008080010001000ffd9")
            ).decode(),
            "captured_at": captured.isoformat(),
        }
        await service.invoke("owner", frame)
        await call.vision_task
        view = await service.invoke("owner", {**binding, "operation": "snapshot"})
        assert view["camera"]["state"] == "delayed"
        projected = project_call_context(
            {
                "version": 2,
                "call_id": binding["call_id"],
                "turn_id": "turn-1",
                "mode": "video",
                "camera": view["camera"],
                "captured_at": captured.isoformat(),
                "observation": call.observation["text"],
                "freshness_seconds": 60,
            }
        )
        assert projected["camera"]["state"] == "delayed"
        assert projected["camera"]["observation"] == "A book is visible."
        assert projected["camera"]["captured_at"] == captured.isoformat()

        stale = (datetime.now(timezone.utc) - timedelta(seconds=61)).isoformat()
        stale_projection = project_call_context(
            {
                "version": 2,
                "call_id": binding["call_id"],
                "turn_id": "turn-2",
                "mode": "video",
                "camera": {"state": "delayed"},
                "captured_at": stale,
                "observation": "Old observation",
                "freshness_seconds": 60,
            }
        )
        assert stale_projection["camera"]["state"] == "unavailable"
        assert stale_projection["camera"]["observation"] is None
        with pytest.raises(CallError, match="call_frame_stale"):
            await service.invoke(
                "owner", {**frame, "frame_sequence": 2, "captured_at": stale}
            )
        assert (await service.invoke("owner", {**binding, "operation": "snapshot"}))[
            "phase"
        ] == "active"
        with pytest.raises(CallError, match="call_scope_changed"):
            await service.invoke(
                "owner", {**frame, "frame_sequence": 2, "client_id": "other-device"}
            )
    finally:
        await service.close()


async def test_turn_frames_respect_freshness_even_when_sampling_window_is_longer(
    tmp_path,
):
    service, _ports, _adapters, base, _clock = setup(tmp_path)
    try:
        binding, _, _ = await start(service, base)
        call = service.calls["call-1"]
        call.camera_enabled = True
        call.video_policy["freshness_seconds"] = 3
        now = datetime.now(timezone.utc).timestamp()
        call.frame_buffer = [
            {"image": b"old", "frame_sequence": 1, "captured_unix": now - 4},
            {"image": b"new", "frame_sequence": 2, "captured_unix": now - 1},
        ]
        selected = service._select_turn_frames(call, now)
        assert [frame["frame_sequence"] for frame in selected] == [2]
    finally:
        await service.close()


@pytest.mark.parametrize("value", [0, -1, True, "60", 2**31])
def test_video_policy_rejects_invalid_resource_values(tmp_path, value):
    service, *_ = setup(tmp_path)
    with pytest.raises(CallError, match="call_video_policy_invalid"):
        service.config.video_policy({"video_policy": {"freshness_seconds": value}})


async def test_queued_turn_drops_frames_that_expire_before_admission(
    tmp_path, monkeypatch
):
    from orchestrator.frontend_call import service as service_module
    from test_call_contract import wav

    service, ports, adapters, base, _clock = setup(tmp_path)
    doc = json.loads(service.config.path.read_text())
    doc["default_profile"]["vision"] = {"target_id": "eyes", "options": {}}
    doc["video_policy"] = {"freshness_seconds": 3}
    service.config.path.write_text(json.dumps(doc))
    wall = [datetime.now(timezone.utc).timestamp()]
    monkeypatch.setattr(service_module.time, "time", lambda: wall[0])
    release = asyncio.Event()
    adapters.wait = release
    try:
        binding, _, _ = await start(service, base, video_requested=True)
        captured = datetime.fromtimestamp(wall[0], timezone.utc).isoformat()
        await service.invoke(
            "owner",
            {
                **binding,
                "operation": "observe",
                "frame_sequence": 1,
                "image_b64": base64.b64encode(
                    bytes.fromhex("ffd8ffc00008080010001000ffd9")
                ).decode(),
                "captured_at": captured,
            },
        )
        await service.invoke(
            "owner",
            {
                **binding,
                "operation": "turn",
                "turn_id": "queued-frame",
                "sequence": 1,
                "audio_b64": wav(),
            },
        )
        await asyncio.sleep(0)
        wall[0] += 4
        release.set()
        await service.calls["call-1"].task
        assert len(ports.accepted) == 1
        assert ports.accepted_frames == [()]
        assert service.calls["call-1"].turn["live_frame_count"] == 0
        sample = (await service.invoke("owner", {**binding, "operation": "snapshot"}))[
            "turn"
        ]["camera_sample"]
        assert sample["selected_count"] == 1 and sample["submitted_count"] == 0
        assert sample["discarded_as_stale"] == 1 and sample["span_seconds"] is None
    finally:
        release.set()
        await service.close()


async def test_selected_camera_sample_window_reports_actual_admission_coverage(
    tmp_path,
):
    from test_call_contract import wav

    service, ports, _adapters, base, _clock = setup(tmp_path)
    doc = json.loads(service.config.path.read_text())
    doc["default_profile"]["vision"] = {"target_id": "eyes", "options": {}}
    doc["video_policy"] = {"freshness_seconds": 60, "sample_window_seconds": 45}
    service.config.path.write_text(json.dumps(doc))
    try:
        binding, _, _ = await start(service, base, video_requested=True)
        call = service.calls["call-1"]
        now = datetime.now(timezone.utc)
        image = base64.b64encode(bytes.fromhex("ffd8ffc00008080010001000ffd9")).decode()
        for sequence, age in enumerate((55, 25, 4), start=1):
            await service.invoke(
                "owner",
                {
                    **binding,
                    "operation": "observe",
                    "frame_sequence": sequence,
                    "captured_at": (now - timedelta(seconds=age)).isoformat(),
                    "image_b64": image,
                },
            )
        assert [frame["frame_sequence"] for frame in call.frame_buffer] == [2, 3]
        await service.invoke(
            "owner",
            {
                **binding,
                "operation": "turn",
                "turn_id": "coverage",
                "sequence": 1,
                "audio_b64": wav(),
            },
        )
        await call.task
        view = await service.invoke("owner", {**binding, "operation": "snapshot"})
        sample = view["turn"]["camera_sample"]
        assert sample["kind"] == "sampled_snapshots"
        assert sample["window_seconds"] == 45
        assert sample["buffered_candidates"] == 2
        assert sample["selected_count"] == 2
        assert sample["submitted_count"] == len(ports.accepted_frames[0]) == 2
        assert sample["discarded_as_stale"] == 0
        assert sample["span_seconds"] == 21
        assert sample["maximum_frames"] == 10
        assert [frame["frame_sequence"] for frame in ports.accepted_frames[0]] == [2, 3]
    finally:
        await service.close()


async def test_camera_clock_information_survives_capture_and_server_time_is_available(
    tmp_path,
):
    service, _ports, _adapters, base, _clock = setup(tmp_path)
    doc = json.loads(service.config.path.read_text())
    doc["default_profile"]["vision"] = {"target_id": "eyes", "options": {}}
    service.config.path.write_text(json.dumps(doc))
    try:
        info = await service.invoke("owner", {**base, "operation": "context"})
        server_time = datetime.fromisoformat(info["server_time"])
        assert server_time.tzinfo is not None
        assert abs((datetime.now(timezone.utc) - server_time).total_seconds()) < 2
        binding, _, _ = await start(service, base, video_requested=True)
        timestamp = datetime.now(timezone.utc).isoformat()
        clock = {
            "basis": "server_roundtrip_lower_bound",
            "client_captured_at": (
                datetime.now(timezone.utc) + timedelta(minutes=2)
            ).isoformat(),
            "uncertainty_ms": 400,
        }
        frame = {
            **binding,
            "operation": "observe",
            "frame_sequence": 1,
            "image_b64": base64.b64encode(
                bytes.fromhex("ffd8ffc00008080010001000ffd9")
            ).decode(),
            "captured_at": timestamp,
            "capture_clock": clock,
        }
        await service.invoke("owner", frame)
        call = service.calls["call-1"]
        assert call.frame_buffer[-1]["capture_clock"] == clock
        await call.vision_task
        view = await service.invoke("owner", {**binding, "operation": "snapshot"})
        assert view["camera"]["capture_clock"] == clock
        assert view["camera"]["captured_at"] == timestamp
        from orchestrator.frontend_call.context import project_call_context
        from orchestrator.message_context import render_message_context_section

        projected = project_call_context(
            {
                "version": 2,
                "call_id": "call-1",
                "turn_id": "turn-1",
                "mode": "video",
                "camera": view["camera"],
                "captured_at": timestamp,
                "observation": call.observation["text"],
            }
        )
        assert projected["camera"]["capture_clock"] == clock
        rendered = render_message_context_section({"call": projected})
        assert '"uncertainty_ms": 400' in rendered
        with pytest.raises(CallError, match="call_capture_clock_invalid"):
            await service.invoke(
                "owner",
                {
                    **frame,
                    "frame_sequence": 2,
                    "capture_clock": {**clock, "uncertainty_ms": -1},
                },
            )
    finally:
        await service.close()


async def test_camera_sample_coverage_reflects_transport_omission(tmp_path):
    from test_call_contract import wav

    service, ports, _adapters, base, _clock = setup(tmp_path)
    doc = json.loads(service.config.path.read_text())
    doc["default_profile"]["vision"] = {"target_id": "eyes", "options": {}}
    service.config.path.write_text(json.dumps(doc))
    original_admit = ports.admit

    async def limited_admit(
        owner, binding, turn, text, observation, captured_at, frames
    ):
        result = await original_admit(
            owner, binding, turn, text, observation, captured_at, frames[-1:]
        )
        return {
            **result,
            "_admitted_frame_sequences": [frames[-1]["frame_sequence"]],
            "_omitted_frames_for_transport": len(frames) - 1,
        }

    ports.admit = limited_admit
    try:
        binding, _, _ = await start(service, base, video_requested=True)
        now = datetime.now(timezone.utc)
        image = base64.b64encode(bytes.fromhex("ffd8ffc00008080010001000ffd9")).decode()
        for index in (1, 2):
            await service.invoke(
                "owner",
                {
                    **binding,
                    "operation": "observe",
                    "frame_sequence": index,
                    "captured_at": (now - timedelta(seconds=3 - index)).isoformat(),
                    "image_b64": image,
                },
            )
        await service.invoke(
            "owner",
            {
                **binding,
                "operation": "turn",
                "turn_id": "transport",
                "sequence": 1,
                "audio_b64": wav(),
            },
        )
        await service.calls["call-1"].task
        view = await service.invoke("owner", {**binding, "operation": "snapshot"})
        sample = view["turn"]["camera_sample"]
        assert sample["selected_count"] == 2 and sample["submitted_count"] == 1
        assert sample["omitted_for_transport"] == 1 and sample["span_seconds"] == 0
        assert len(ports.accepted_frames[0]) == 1
    finally:
        await service.close()


async def test_selected_camera_frame_and_buffer_budgets_above_old_limits(tmp_path):
    from test_call_contract import wav

    service, ports, _adapters, base, _clock = setup(tmp_path)
    doc = json.loads(service.config.path.read_text())
    doc["default_profile"]["vision"] = {"target_id": "eyes", "options": {}}
    doc["video_policy"] = {
        "freshness_seconds": 60,
        "sample_window_seconds": 45,
        "max_frames_per_turn": 20,
        "max_buffer_frames": 40,
    }
    service.config.path.write_text(json.dumps(doc))
    try:
        binding, _, _ = await start(service, base, video_requested=True)
        call = service.calls["call-1"]
        now = datetime.now(timezone.utc)
        image = base64.b64encode(bytes.fromhex("ffd8ffc00008080010001000ffd9")).decode()
        for index in range(1, 36):
            await service.invoke(
                "owner",
                {
                    **binding,
                    "operation": "observe",
                    "frame_sequence": index,
                    "captured_at": (now - timedelta(seconds=36 - index)).isoformat(),
                    "image_b64": image,
                },
            )
        assert len(call.frame_buffer) == 35
        await service.invoke(
            "owner",
            {
                **binding,
                "operation": "turn",
                "turn_id": "larger-sample",
                "sequence": 1,
                "audio_b64": wav(),
            },
        )
        await call.task
        frames = ports.accepted_frames[0]
        assert len(frames) == 20
        assert frames[0]["frame_sequence"] == 1 and frames[-1]["frame_sequence"] == 35
        sample = (await service.invoke("owner", {**binding, "operation": "snapshot"}))[
            "turn"
        ]["camera_sample"]
        assert sample["maximum_frames"] == 20 and sample["buffered_candidates"] == 35
        assert sample["submitted_count"] == 20
    finally:
        await service.close()


async def test_selected_camera_buffer_byte_budget_keeps_fitting_recent_frames(tmp_path):
    service, _ports, _adapters, base, _clock = setup(tmp_path)
    doc = json.loads(service.config.path.read_text())
    doc["default_profile"]["vision"] = {"target_id": "eyes", "options": {}}
    doc["video_policy"] = {"max_buffer_frames": 40, "max_buffer_bytes": 28}
    service.config.path.write_text(json.dumps(doc))
    try:
        binding, _, _ = await start(service, base, video_requested=True)
        now = datetime.now(timezone.utc)
        image = base64.b64encode(bytes.fromhex("ffd8ffc00008080010001000ffd9")).decode()
        for index in range(1, 5):
            await service.invoke(
                "owner",
                {
                    **binding,
                    "operation": "observe",
                    "frame_sequence": index,
                    "captured_at": (now - timedelta(seconds=5 - index)).isoformat(),
                    "image_b64": image,
                },
            )
        call = service.calls["call-1"]
        assert [frame["frame_sequence"] for frame in call.frame_buffer] == [3, 4]
        assert sum(len(frame["image"]) for frame in call.frame_buffer) == 28
        assert call.video_policy["max_buffer_bytes"] == 28
    finally:
        await service.close()


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("max_frames_per_turn", 0),
        ("max_frames_per_turn", True),
        ("max_buffer_frames", -1),
        ("max_buffer_bytes", -1),
        ("max_buffer_bytes", True),
    ],
)
def test_camera_sample_budget_rejects_invalid_selected_values(tmp_path, key, value):
    service, *_ = setup(tmp_path)
    with pytest.raises(CallError, match="call_video_policy_invalid"):
        service.config.video_policy({"video_policy": {key: value}})


async def test_single_frame_sample_selects_most_recent_eligible_frame(tmp_path):
    service, _ports, _adapters, base, _clock = setup(tmp_path)
    try:
        await start(service, base)
        call = service.calls["call-1"]
        call.camera_enabled = True
        call.video_policy["max_frames_per_turn"] = 1
        now = datetime.now(timezone.utc).timestamp()
        call.frame_buffer = [
            {"image": b"x", "frame_sequence": index, "captured_unix": now - 4 + index}
            for index in range(1, 4)
        ]
        selected = service._select_turn_frames(call, now)
        assert [frame["frame_sequence"] for frame in selected] == [3]
    finally:
        await service.close()
