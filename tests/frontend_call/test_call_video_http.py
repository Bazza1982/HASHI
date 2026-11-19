"""Real HTTP tests for vendor-neutral VideoTTS and the AP output port."""

import asyncio
import base64
import hashlib
import json
import struct
from types import SimpleNamespace

import pytest
from aiohttp import web

from orchestrator.agent_presence_call_video import AgentPresenceCallVideo
from orchestrator.compute_services import parse_compute_manifest, validate_compute_node
from orchestrator.frontend_call.contract import CallError
from orchestrator.frontend_call.video_tts import CallVideoTTS, VideoTTSSelection
from test_call_http import serve

# Only a transport signature: AP's native MP4 validator separately proves AV.
MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isomiso2"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(">II", 512, 288)


def manifest():
    return {
        "schema": "hashi.compute.capabilities",
        "schema_version": 1,
        "name": "Independent avatar provider",
        "version": "1",
        "config_revision": 1,
        "privacy_location": "local",
        "health": {"status": "ready", "path": "/health"},
        "capacity": {"max_concurrency": 1, "in_flight": 0, "busy": False},
        "services": [
            {
                "id": "avatar",
                "kind": "video_tts",
                "enabled": True,
                "status": "ready",
                "capacity": {"max_concurrency": 1, "in_flight": 0, "busy": False},
                "models": [
                    {
                        "id": "talker",
                        "selected": True,
                        "capabilities": {
                            "input_modalities": ["text", "image"],
                            "output_modalities": ["audio", "video"],
                            "tools": False,
                            "thinking": False,
                            "context_length": None,
                            "image": {
                                "max_count": 1,
                                "max_bytes": 4194304,
                                "max_total_bytes": 4194304,
                                "formats": ["png"],
                            },
                            "audio": {
                                "input_formats": [],
                                "output_formats": ["aac_in_mp4"],
                            },
                        },
                        "interfaces": [
                            {
                                "id": "speech",
                                "protocol": "video.speech.async.v1",
                                "method": "POST",
                                "path": "/avatar/render",
                                "streaming": ["none"],
                            }
                        ],
                    }
                ],
            }
        ],
    }


@pytest.mark.anyio
async def test_ap_probe_declines_role_without_video_tts():
    app, requests = web.Application(), []

    async def capabilities(request):
        requests.append(request.path)
        return web.json_response(
            {
                "features": {"call_video": True},
                "call_video_contract": {
                    "version": "ap-call-video/1",
                    "requires_commit": True,
                    "video_tts_supported": False,
                    "video_tts_unavailable_reason": "character_has_no_human_face",
                },
            }
        )

    async def binding(request):
        requests.append(request.path)
        return web.json_response({"configured": True, "instance_id": "test-instance"})

    app.router.add_get("/v1/capabilities", capabilities)
    app.router.add_get("/v1/hashi", binding)
    runner, origin = await serve(app)
    try:
        port = AgentPresenceCallVideo(
            origin,
            "fixture-only",
            "test-instance",
            "call-one",
        )
        assert await port.probe() is False
        assert requests == ["/v1/capabilities", "/v1/hashi"]
        assert port.context_id == ""
        assert port.reference == b""
    finally:
        await runner.cleanup()


def selection(**overrides):
    return VideoTTSSelection.parse(
        {
            "enabled": True,
            "reference": {
                "node_id": "studio",
                "service_id": "avatar",
                "model_id": "talker",
            },
            "voice": "warm",
            "job_path": "/avatar/jobs/{job_id}",
            **overrides,
        }
    )


def test_video_wait_budget_scales_with_text_and_keeps_tracking_longer():
    policy = selection()
    assert policy.first_segment_sentences == 2
    assert policy.group_sentences == 4
    assert policy.max_inflight_groups == 2
    assert policy.queue_timeout_seconds == 180
    assert policy.foreground_wait_seconds("短句。") == 20
    assert policy.foreground_wait_seconds("字" * 161) == pytest.approx(74.85)
    assert policy.job_timeout_seconds > policy.foreground_wait_seconds("字" * 400)
    tuned = selection(
        first_segment_sentences=3, group_sentences=6, max_inflight_groups=5
    )
    assert (
        tuned.first_segment_sentences,
        tuned.group_sentences,
        tuned.max_inflight_groups,
    ) == (3, 6, 5)
    with pytest.raises(CallError, match="call_video_configuration_invalid"):
        selection(job_timeout_seconds=30, wait_max_seconds=90)
    with pytest.raises(CallError, match="call_video_configuration_invalid"):
        selection(queue_timeout_seconds=0)
    with pytest.raises(CallError, match="call_video_configuration_invalid"):
        selection(first_segment_sentences=0)
    with pytest.raises(CallError, match="call_video_configuration_invalid"):
        selection(group_sentences=33)
    with pytest.raises(CallError, match="call_video_configuration_invalid"):
        selection(max_inflight_groups=0)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "outcome,code",
    [
        ("success", None),
        ("failed", "call_video_native_failure"),
        ("cross_origin", "call_video_result_url_invalid"),
        ("redirect", "call_video_service_rejected"),
        ("oversized", "call_video_response_too_large"),
    ],
)
async def test_independent_provider_manifest_job_and_result_boundaries(
    tmp_path, monkeypatch, outcome, code
):
    app, posts, redirected, diagnostics, receipts = web.Application(), [], [], [], []
    monkeypatch.setattr(
        "orchestrator.frontend_call.video_tts.emit",
        lambda event, **facts: diagnostics.append({"event": event, **facts}),
    )

    async def discovery(request):
        return web.json_response(manifest())

    async def render(request):
        posts.append(await request.json())
        return web.json_response({"id": "job-one", "status": "queued"}, status=202)

    async def job(request):
        if outcome == "failed":
            return web.json_response(
                {
                    "id": "job-one",
                    "status": "failed",
                    "error": {
                        "code": "video_native_failure",
                        "diagnostic_id": "diagnostic-one",
                    },
                }
            )
        url = (
            "https://unselected.example/private"
            if outcome == "cross_origin"
            else "/avatar/result"
        )
        return web.json_response(
            {"id": "job-one", "status": "completed", "video_url": url}
        )

    async def video(request):
        if outcome == "redirect":
            raise web.HTTPFound("/unselected")
        return web.Response(
            body=MP4 + (b"x" * 100 if outcome == "oversized" else b""),
            content_type="video/mp4",
        )

    async def unselected(request):
        redirected.append(True)
        return web.Response(body=MP4, content_type="video/mp4")

    app.router.add_get("/manifest", discovery)
    app.router.add_post("/avatar/render", render)
    app.router.add_get("/avatar/jobs/{job_id}", job)
    app.router.add_get("/avatar/result", video)
    app.router.add_get("/unselected", unselected)
    runner, origin = await serve(app)
    config = tmp_path / "compute_services.json"
    config.write_text(
        json.dumps(
            {
                "version": 1,
                "nodes": [
                    {
                        "id": "studio",
                        "location": "local",
                        "base_url": origin,
                        "manifest_path": "/manifest",
                    }
                ],
            }
        )
    )
    consumer = CallVideoTTS(config, poll_seconds=0)
    if outcome == "oversized":
        monkeypatch.setattr("orchestrator.frontend_call.video_tts.MAX_VIDEO_BYTES", 40)
    try:
        if code:
            with pytest.raises(CallError, match=code):
                await consumer.synthesize(
                    selection(),
                    PNG,
                    "Hello.",
                    privacy_level=1,
                    request_id="one",
                    on_job=receipts.append,
                )
        else:
            result = await consumer.synthesize(
                selection(),
                PNG,
                "Hello.",
                privacy_level=1,
                request_id="one",
                on_job=receipts.append,
            )
            assert (
                result["media"] == MP4
                and result["provider_receipt"]["job_id"] == "job-one"
            )
        assert len(posts) == 1 and redirected == []
        assert (
            posts[0]["image"]
            == "data:image/png;base64," + base64.b64encode(PNG).decode()
        )
        assert posts[0]["voice"] == "warm" and posts[0]["model"] == "talker"
        if outcome == "failed":
            assert any(
                event.get("event") == "video_tts_failed"
                and event.get("provider_error_code") == "video_native_failure"
                and event.get("provider_diagnostic_id") == "diagnostic-one"
                for event in diagnostics
            )
            assert receipts[-1]["diagnostic_id"] == "diagnostic-one"
        if outcome == "redirect":
            assert any(
                event.get("event") == "video_tts_service_rejected"
                and event.get("provider_http_status") == 302
                for event in diagnostics
            )
    finally:
        await runner.cleanup()


async def test_privacy_and_frozen_origin_precede_render_submission():
    node = validate_compute_node(
        {"id": "studio", "location": "internet", "base_url": "https://selected.example"}
    )
    payload = manifest()
    payload["privacy_location"] = "internet"
    catalogue = parse_compute_manifest(node, payload).catalogue()

    class Directory:
        async def refresh(self):
            return SimpleNamespace(catalogue=catalogue)

    class NeverRequest:
        async def request(self, *args, **kwargs):
            pytest.fail("privacy/frozen origin must reject before provider invocation")

    consumer = CallVideoTTS("unused", directory=Directory(), client=NeverRequest())
    with pytest.raises(CallError, match="call_video_capability_mismatch"):
        await consumer.synthesize(
            selection(), PNG, "Hello.", privacy_level=3, request_id="one"
        )
    with pytest.raises(CallError, match="call_video_configuration_changed"):
        await consumer.synthesize(
            selection(),
            PNG,
            "Hello.",
            privacy_level=1,
            request_id="one",
            expected_origin="https://previous.example",
        )
    model = payload["services"][0]["models"][0]
    model["capabilities"]["image"]["max_total_bytes"] = 8
    catalogue = parse_compute_manifest(node, payload).catalogue()
    with pytest.raises(CallError, match="call_video_reference_unsupported"):
        await consumer.synthesize(
            selection(), PNG, "Hello.", privacy_level=1, request_id="one"
        )
    model["capabilities"]["image"]["max_total_bytes"] = 4194304
    model["interfaces"][0]["method"] = "GET"
    catalogue = parse_compute_manifest(node, payload).catalogue()
    with pytest.raises(CallError, match="call_video_interface_invalid"):
        await consumer.synthesize(
            selection(), PNG, "Hello.", privacy_level=1, request_id="one"
        )


@pytest.mark.anyio
async def test_async_video_queue_accepts_busy_capability_but_sync_does_not():
    node = validate_compute_node(
        {"id": "studio", "location": "lan", "base_url": "https://10.0.0.41"}
    )
    payload = manifest()
    payload["privacy_location"] = "lan"
    payload["capacity"].update(in_flight=1, busy=True)
    payload["services"][0]["status"] = "busy"
    payload["services"][0]["capacity"].update(in_flight=1, busy=True)
    catalogue = parse_compute_manifest(node, payload).catalogue()

    class Directory:
        async def refresh(self):
            return SimpleNamespace(catalogue=catalogue)

    consumer = CallVideoTTS("unused", directory=Directory())
    resolved = await consumer.resolve(selection(), privacy_level=1)
    assert resolved.offering.service_id == "avatar"

    with pytest.raises(CallError, match="call_video_compute_node_busy"):
        await consumer.resolve(
            selection(protocol="video.speech.mp4.v1"), privacy_level=1
        )


async def test_async_cancellation_keeps_job_identity_and_submits_no_second_job(
    tmp_path,
):
    app, events, polled = web.Application(), [], asyncio.Event()

    async def discovery(request):
        return web.json_response(manifest())

    async def render(request):
        events.append("submit")
        return web.json_response({"id": "job-one", "status": "queued"}, status=202)

    async def job(request):
        polled.set()
        return web.json_response({"id": "job-one", "status": "running"})

    async def cancel(request):
        events.append("cancel:" + request.match_info["job_id"])
        return web.json_response({"status": "cancelled"})

    app.router.add_get("/manifest", discovery)
    app.router.add_post("/avatar/render", render)
    app.router.add_get("/avatar/jobs/{job_id}", job)
    app.router.add_post("/avatar/jobs/{job_id}/cancel", cancel)
    runner, origin = await serve(app)
    config = tmp_path / "compute_services.json"
    config.write_text(
        json.dumps(
            {
                "version": 1,
                "nodes": [
                    {
                        "id": "studio",
                        "location": "local",
                        "base_url": origin,
                        "manifest_path": "/manifest",
                    }
                ],
            }
        )
    )
    consumer = CallVideoTTS(config, poll_seconds=0.01)
    try:
        task = asyncio.create_task(
            consumer.synthesize(
                selection(cancel_path="/avatar/jobs/{job_id}/cancel"),
                PNG,
                "Hello.",
                privacy_level=1,
                request_id="one",
            )
        )
        await asyncio.wait_for(polled.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert events == ["submit", "cancel:job-one"]
    finally:
        await runner.cleanup()


async def test_ap_reference_preload_commit_and_generation_race():
    app, mutations = web.Application(), []
    context = "a" * 32
    state = {
        "call_id": "call-one",
        "context_id": context,
        "instance_id": "test-instance",
        "generation": 0,
        "call_active": True,
        "audio_owner": "agentpresence",
        "stage": "idle",
        "phase": "day",
        "replies": [],
    }
    stale_started, release_stale = asyncio.Event(), asyncio.Event()
    delayed = False

    async def route(request):
        nonlocal delayed
        assert request.headers.get("X-AP-Token") == "fixture-only"
        path = request.path
        if path == "/v1/call/video/start":
            state["reference"] = {
                "url": "https://never-follow.example/token",
                "mime": "image/png",
                "sha256": hashlib.sha256(PNG).hexdigest(),
            }
            mutations.append("start")
            return web.json_response(state)
        if path.endswith("/reference"):
            return web.Response(body=PNG, content_type="image/png")
        if path.endswith("/media"):
            assert await request.read() == MP4
            state["replies"].append(
                {
                    "utterance_id": request.query["utterance_id"],
                    "state": "prepared",
                    "generation": 0,
                }
            )
            mutations.append("prepare")
            return web.json_response(state["replies"][-1], status=202)
        if path.endswith("/commit"):
            mutations.append("commit")
            state["replies"][0]["state"] = "playing"
            return web.json_response(state["replies"][0])
        if path.endswith("/interrupt"):
            state["generation"] += 1
            mutations.append("interrupt")
            return web.json_response(state)
        if path.endswith("/end"):
            mutations.append("end")
            state["call_active"] = False
            return web.json_response(state)
        if delayed:
            snapshot = json.loads(json.dumps(state))
            stale_started.set()
            await release_stale.wait()
            return web.json_response(snapshot)
        return web.json_response(state)

    app.router.add_route("*", "/v1/call/video/{operation}", route)
    runner, origin = await serve(app)
    port = AgentPresenceCallVideo(
        origin, "fixture-only", "test-instance", "call-one", poll_seconds=0
    )
    try:
        await port.start()
        assert port.reference == PNG
        await port.prepare("utt-one", MP4, epoch=0)
        assert mutations == ["start", "prepare"]
        await port.commit("utt-one", epoch=0)
        assert mutations[-1] == "commit"
        delayed = True
        old_status = asyncio.create_task(port.refresh())
        await asyncio.wait_for(stale_started.wait(), 2)
        port.fence("turn-one")
        await port.interrupt()
        release_stale.set()
        await old_status
        assert port.generation == 1
        with pytest.raises(CallError, match="call_video_interrupted"):
            await port.commit("utt-one", epoch=0)
        await port.end()
        assert mutations[-1] == "end"
    finally:
        release_stale.set()
        await runner.cleanup()


def test_video_configured_larger_capacity_and_timeout_policy():
    policy = selection(
        request_timeout_seconds=1200,
        job_timeout_seconds=7200,
        queue_timeout_seconds=8000,
        wait_max_seconds=3600,
        max_input_chars=5001,
    )
    assert (
        policy.request_timeout_seconds,
        policy.job_timeout_seconds,
        policy.queue_timeout_seconds,
        policy.max_input_chars,
    ) == (1200, 7200, 8000, 5001)
    for invalid in (0, -1, float("inf"), float("nan")):
        with pytest.raises(CallError, match="call_video_configuration_invalid"):
            selection(job_timeout_seconds=invalid)


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["recover", "wrong_key", "wrong_job", "no_lookup"])
async def test_video_submission_reconciliation_never_reposts_or_crosses_job(
    tmp_path, mode
):
    events = []
    app = web.Application()
    payload = manifest()
    interfaces = payload["services"][0]["models"][0]["interfaces"]
    if mode != "no_lookup":
        interfaces.append(
            {
                "id": "lookup",
                "protocol": "video.speech.lookup.v1",
                "method": "GET",
                "path": "/avatar/by-key/{idempotency_key}",
                "streaming": ["none"],
            }
        )
    expected_key = hashlib.sha256(b"one").hexdigest()

    async def discovery(request):
        return web.json_response(payload)

    async def render(request):
        events.append("submit")
        assert request.headers["Idempotency-Key"] == expected_key
        if mode != "wrong_job":
            request.transport.close()
            return web.Response(status=202)
        return web.json_response({"id": "job-one", "status": "queued"}, status=202)

    async def lookup(request):
        events.append("lookup")
        assert request.match_info["idempotency_key"] == expected_key
        return web.json_response(
            {
                "id": "job-one",
                "status": "queued",
                "idempotency_key": "wrong" if mode == "wrong_key" else expected_key,
            }
        )

    async def job(request):
        events.append("poll")
        return web.json_response(
            {
                "id": "job-other" if mode == "wrong_job" else "job-one",
                "status": "completed",
                "video_url": "/avatar/result",
            }
        )

    async def video(request):
        events.append("download")
        return web.Response(body=MP4, content_type="video/mp4")

    app.router.add_get("/manifest", discovery)
    app.router.add_post("/avatar/render", render)
    app.router.add_get("/avatar/by-key/{idempotency_key}", lookup)
    app.router.add_get("/avatar/jobs/{job_id}", job)
    app.router.add_get("/avatar/result", video)
    runner, origin = await serve(app)
    config = tmp_path / "compute_services.json"
    config.write_text(
        json.dumps(
            {
                "version": 1,
                "nodes": [
                    {
                        "id": "studio",
                        "location": "local",
                        "base_url": origin,
                        "manifest_path": "/manifest",
                    }
                ],
            }
        )
    )
    consumer = CallVideoTTS(config, poll_seconds=0)
    try:
        if mode == "recover":
            result = await consumer.synthesize(
                selection(), PNG, "Hello.", privacy_level=1, request_id="one"
            )
            assert result["media"] == MP4
            assert events == ["submit", "lookup", "poll", "download"]
        else:
            code = (
                "call_video_job_invalid"
                if mode == "wrong_job"
                else "call_video_outcome_unknown"
            )
            with pytest.raises(CallError, match=code):
                await consumer.synthesize(
                    selection(), PNG, "Hello.", privacy_level=1, request_id="one"
                )
            assert events.count("submit") == 1
            assert "download" not in events
            assert ("lookup" in events) is (mode == "wrong_key")
    finally:
        await runner.cleanup()


@pytest.mark.anyio
async def test_video_service_advertised_large_reference_and_configured_large_output(
    tmp_path, monkeypatch
):
    reference = (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + struct.pack(">II", 4000, 3000)
        + b"x" * (4 * 1024 * 1024)
    )
    video_data = MP4 + b"x" * (64 * 1024 * 1024)
    image_cap = 6 * 1024 * 1024
    media_cap = len(video_data) + 1
    monkeypatch.setenv("HASHI_CALL_VIDEO_MAX_REFERENCE_BYTES", str(image_cap))
    monkeypatch.setenv("HASHI_CALL_VIDEO_MAX_REFERENCE_PIXELS", "12000000")
    monkeypatch.setenv("HASHI_CALL_VIDEO_MAX_MEDIA_BYTES", str(media_cap))
    payload = manifest()
    limits = payload["services"][0]["models"][0]["capabilities"]["image"]
    limits["max_bytes"] = limits["max_total_bytes"] = image_cap
    events = []
    app = web.Application(client_max_size=10 * 1024 * 1024)

    async def discovery(request):
        return web.json_response(payload)

    async def render(request):
        data = await request.json()
        assert len(data["input"]) == 4001
        assert base64.b64decode(data["image"].split(",", 1)[1]) == reference
        events.append("submit")
        return web.json_response({"id": "job-one", "status": "queued"}, status=202)

    async def job(request):
        return web.json_response(
            {"id": "job-one", "status": "completed", "video_url": "/avatar/result"}
        )

    async def video(request):
        return web.Response(body=video_data, content_type="video/mp4")

    app.router.add_get("/manifest", discovery)
    app.router.add_post("/avatar/render", render)
    app.router.add_get("/avatar/jobs/{job_id}", job)
    app.router.add_get("/avatar/result", video)
    runner, origin = await serve(app)
    config = tmp_path / "compute_services.json"
    config.write_text(
        json.dumps(
            {
                "version": 1,
                "nodes": [
                    {
                        "id": "studio",
                        "location": "local",
                        "base_url": origin,
                        "manifest_path": "/manifest",
                    }
                ],
            }
        )
    )
    try:
        result = await CallVideoTTS(config, poll_seconds=0).synthesize(
            selection(
                max_input_chars=5001,
                job_timeout_seconds=7200,
                queue_timeout_seconds=7200,
            ),
            reference,
            "x" * 4001,
            privacy_level=1,
            request_id="one",
        )
        assert result["media"] == video_data
        assert events == ["submit"]
    finally:
        await runner.cleanup()


@pytest.mark.anyio
async def test_ap_configured_larger_media_budgets(tmp_path, monkeypatch):
    reference = PNG + b"x" * (4 * 1024 * 1024)
    media = MP4 + b"x" * (64 * 1024 * 1024)
    monkeypatch.setenv("HASHI_CALL_VIDEO_MAX_REFERENCE_BYTES", str(len(reference) + 1))
    monkeypatch.setenv("HASHI_CALL_VIDEO_MAX_MEDIA_BYTES", str(len(media) + 1))
    state = {
        "call_id": "call-one",
        "context_id": "a" * 32,
        "instance_id": "test-instance",
        "generation": 0,
        "call_active": True,
        "audio_owner": "agentpresence",
        "stage": "idle",
        "phase": "day",
        "replies": [],
        "reference": {
            "mime": "image/png",
            "sha256": hashlib.sha256(reference).hexdigest(),
        },
    }
    events = []
    app = web.Application(client_max_size=70 * 1024 * 1024)

    async def route(request):
        assert request.headers["X-AP-Token"] == "fixture-only"
        operation = request.match_info["operation"]
        if operation == "start":
            return web.json_response(state)
        if operation == "reference":
            return web.Response(body=reference, content_type="image/png")
        if operation == "media":
            received = await request.read()
            assert received == media
            events.append("media")
            state["replies"].append(
                {
                    "utterance_id": request.query["utterance_id"],
                    "state": "prepared",
                    "generation": 0,
                }
            )
            return web.json_response(state["replies"][-1], status=202)
        if operation == "end":
            state["call_active"] = False
        return web.json_response(state)

    app.router.add_route("*", "/v1/call/video/{operation}", route)
    runner, origin = await serve(app)
    port = AgentPresenceCallVideo(
        origin, "fixture-only", "test-instance", "call-one", poll_seconds=0
    )
    try:
        await port.start()
        assert port.reference == reference
        await port.prepare("utt-one", media, epoch=0)
        assert events == ["media"]
        await port.end()
    finally:
        await runner.cleanup()
