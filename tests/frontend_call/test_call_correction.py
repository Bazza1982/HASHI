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


def test_call_readiness_and_camera_do_not_depend_on_legacy_selection(tmp_path):
    service, *_ = setup(tmp_path)
    config = service.config
    assert config.route("owner", "agent-a")["route"] == "phone"
    context = config.context("owner", "agent-a")
    config.save("owner", "agent-a", context["revision"], context["profile"])
    assert config.route("owner", "agent-a")["route"] == "phone"
    context = config.context("owner", "agent-a")
    context["profile"]["vision"] = {"target_id": "eyes", "options": {}}
    config.save("owner", "agent-a", context["revision"], context["profile"])
    readiness = config.route("owner", "agent-a")
    assert readiness["call_ready"] is True and readiness["camera_available"] is True
    context = config.context("owner", "agent-a")
    config.select_route("owner", "agent-a", context["revision"], "call")
    assert type(config)(config.path).route("owner", "agent-a")["route"] == "call"
    assert config.route("owner", "other")["route"] == "phone"
    with pytest.raises(CallError, match="configuration_changed"):
        config.select_route("owner", "agent-a", context["revision"], "phone")


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
    assert info["route"] == "phone" and info["call_ready"] is True
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
