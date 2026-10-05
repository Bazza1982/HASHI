"""Exercise call boundaries that previously disappeared between request logs."""

import asyncio
import base64
import json
import logging
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from aiohttp import ClientSession, web

from orchestrator.frontend_call.adapters import MediaAdapters
from orchestrator.frontend_call.contract import CallError, MAX_BODY, MAX_CALL_SECONDS
from orchestrator.frontend_call.routes import register_call_api
from test_call_http import serve
from test_call_service import finish_task, setup, start, wav


def events(caplog, event=None):
    rows = [
        json.loads(record.getMessage().removeprefix("call diagnostic "))
        for record in caplog.records
        if record.getMessage().startswith("call diagnostic ")
    ]
    return [row for row in rows if event is None or row["event"] == event]


async def test_call_termination_reports_real_reason_without_repeated_poll_logs(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="orchestrator.frontend_call")
    for end_kind in ("explicit_end", "lease_expired", "max_duration", "service_shutdown"):
        scenario = tmp_path / end_kind
        scenario.mkdir()
        caplog.clear()
        service, _, _, base, clock = setup(scenario)
        binding, _, _ = await start(service, base)
        before_poll = len(events(caplog))
        for _ in range(3):
            await service.invoke("owner", {**binding, "operation": "snapshot"})
        assert len(events(caplog)) == before_poll
        assert len(events(caplog, "call_started")) == 1
        call = service.calls["call-1"]
        if end_kind == "explicit_end":
            await service.invoke("owner", {**binding, "operation": "end"})
        elif end_kind == "lease_expired":
            clock[0] += 46
            service.expire()
        elif end_kind == "max_duration":
            clock[0] += MAX_CALL_SECONDS
            call.expires = clock[0] + 45
            service.expire()
        else:
            await service.close()
        assert call.phase == "ended"
        ended = events(caplog, "call_ended")
        assert len(ended) == 1 and ended[0]["reason"] == end_kind
        assert ended[0]["phase"] == "ended"
        assert ended[0]["generation"] == binding["generation"]
        assert ended[0]["call_id"] == "call-1"
        await service.close()
        assert len(events(caplog, "call_ended")) == 1


async def test_camera_switch_cancels_observation_with_epoch_and_keeps_call_active(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="orchestrator.frontend_call")
    service, _, adapters, base, _ = setup(tmp_path)
    info = service.config.context("owner", "agent-a")
    info["profile"]["vision"] = {"target_id": "eyes", "options": {}}
    service.config.save("owner", "agent-a", info["revision"], info["profile"])
    binding, _, _ = await start(service, base)
    await service.invoke("owner", {**binding, "operation": "camera", "enabled": True})
    began, release = asyncio.Event(), asyncio.Event()

    async def observe(*_args):
        began.set()
        await release.wait()
        return "PRIVATE_IMAGE_DESCRIPTION"

    adapters.observe = observe
    await service.invoke("owner", {
        **binding, "operation": "observe", "frame_sequence": 1,
        "image_b64": base64.b64encode(bytes.fromhex("ffd8ffc00008080010001000ffd9")).decode(),
        "captured_at": datetime.now(timezone.utc).isoformat(),
    })
    await asyncio.wait_for(began.wait(), 1)
    await service.invoke("owner", {**binding, "operation": "camera", "enabled": False})
    call = service.calls["call-1"]
    await call.vision_task
    assert call.phase == "active" and not call.observation
    changes = events(caplog, "camera_changed")
    assert [row["camera_epoch"] for row in changes] == [1, 2]
    assert changes[-1]["reason"] == "camera_disabled"
    cancelled = events(caplog, "vision_cancelled")[-1]
    assert cancelled["call_id"] == "call-1" and cancelled["camera_epoch"] == 1
    assert cancelled["frame_sequence"] == 1 and cancelled["reason"] == "camera_disabled"
    assert not events(caplog, "call_ended")
    assert "PRIVATE_IMAGE_DESCRIPTION" not in caplog.text
    await service.close()


async def test_turn_timing_correlates_pao_and_tts_and_never_logs_speech(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="orchestrator.frontend_call")
    service, ports, adapters, base, clock = setup(tmp_path)

    async def transcribe(*_args):
        clock[0] += .012
        return {"text": "PRIVATE_TRANSCRIPT", "provider_receipt": {
            "generation_id": "gen-stt-1", "gateway": "OpenRouter",
            "requested_model": "openai/whisper-large-v3", "actual_provider": "Groq",
            "verification": "verified",
        }}

    def result(*_args):
        clock[0] += .02
        return {"terminal": True, "state": "completed", "text": "PRIVATE_ANSWER"}

    adapters.transcribe, ports.result = transcribe, result
    binding, _, _ = await start(service, base)
    await service.invoke("owner", {**binding, "operation": "turn", "turn_id": "turn-1", "sequence": 1, "audio_b64": wav()})
    await finish_task(service)
    await service.invoke("owner", {**binding, "operation": "speech", "turn_id": "turn-1", "segment": 0})
    await service.calls["call-1"].speech_task
    transcribed = events(caplog, "stt_completed")[-1]
    assert transcribed["provider_generation_id"] == "gen-stt-1" and transcribed["duration_ms"] >= 11
    assert transcribed["gateway"] == "OpenRouter" and transcribed["actual_provider"] == "Groq"
    assert transcribed["requested_model"] == "openai/whisper-large-v3"
    admitted = events(caplog, "admission_completed")[-1]
    assert admitted["run_id"] == "run-1" and admitted["request_id"] == "req-1"
    completed = events(caplog, "turn_completed")[-1]
    assert completed["turn_id"] == "turn-1" and completed["run_id"] == "run-1"
    assert completed["duration_ms"] >= 31
    tts = events(caplog, "tts_completed")[-1]
    assert tts["call_id"] == "call-1" and tts["turn_id"] == "turn-1" and tts["segment"] == 0
    assert "PRIVATE_TRANSCRIPT" not in caplog.text and "PRIVATE_ANSWER" not in caplog.text
    assert wav() not in caplog.text
    await service.close()


async def test_ending_during_admission_preserves_uncertain_request_and_records_detach(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="orchestrator.frontend_call")
    service, ports, _, base, _ = setup(tmp_path)
    began, release = asyncio.Event(), asyncio.Event()
    original = ports.admit

    async def admit(*args):
        began.set()
        await release.wait()
        return await original(*args)

    ports.admit = admit
    binding, _, _ = await start(service, base)
    await service.invoke("owner", {**binding, "operation": "turn", "turn_id": "turn-1", "sequence": 1, "audio_b64": wav()})
    await asyncio.wait_for(began.wait(), 1)
    await service.invoke("owner", {**binding, "operation": "end"})
    assert not service.calls["call-1"].task.done()
    ended = events(caplog, "call_ended")[-1]
    assert ended["admission_in_flight"] is True
    release.set()
    await finish_task(service)
    assert len(ports.accepted) == 1
    detached = events(caplog, "turn_detached")[-1]
    assert detached["reason"] == "call_ended" and detached["run_id"] == "run-1"
    assert not events(caplog, "turn_cancelled")
    await service.close()


async def test_background_failure_and_operation_validation_have_safe_correlated_codes(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="orchestrator.frontend_call")
    service, ports, adapters, base, _ = setup(tmp_path)
    binding, _, _ = await start(service, base)

    async def transcribe(*_args):
        raise RuntimeError("PRIVATE_PROVIDER_ERROR https://private.example TOKEN=SECRET")

    adapters.transcribe = transcribe
    await service.invoke("owner", {**binding, "operation": "turn", "turn_id": "turn-1", "sequence": 1, "audio_b64": wav()})
    await finish_task(service)
    failure = events(caplog, "turn_failed")[-1]
    assert failure["stage"] == "stt" and failure["error_code"] == "call_turn_failed"
    assert failure["exception_type"] == "RuntimeError" and failure["turn_id"] == "turn-1"
    assert not ports.accepted
    with pytest.raises(CallError, match="unknown_field"):
        await service.invoke("owner", {**binding, "operation": "snapshot", "private": "PRIVATE_BODY"})
    rejected = events(caplog, "operation_failed")[-1]
    assert rejected["operation"] == "snapshot" and rejected["error_code"] == "call_unknown_field"
    assert rejected["call_id"] == "call-1"
    assert "PRIVATE_PROVIDER_ERROR" not in caplog.text and "TOKEN=SECRET" not in caplog.text
    assert "PRIVATE_BODY" not in caplog.text
    await service.close()


async def test_http_pre_service_failures_record_stage_status_without_unauthenticated_body(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="orchestrator.frontend_call")
    api = SimpleNamespace(
        app=web.Application(), config_path=tmp_path / "agents.json", admin_token="PRIVATE_TOKEN",
        live_voice_manager=SimpleNamespace(), _is_governed_profile=lambda: False,
        _check_admin_auth=lambda request: request.headers.get("X-Workbench-Token") == "PRIVATE_TOKEN",
        _v1_owner_id=lambda request: "owner",
    )
    service = register_call_api(api)
    runner, url = await serve(api.app)
    try:
        async with ClientSession() as session:
            async with session.post(url + "/api/v1/call/operation", json={"operation": "end", "call_id": "untrusted-SECRET"}) as response:
                assert response.status == 403
            async with session.post(url + "/api/v1/call/operation", json={"operation": "context"}, headers={"X-Workbench-Token": "PRIVATE_TOKEN"}) as response:
                assert (await response.json())["error_code"] == "call_not_configured"
        rejected = events(caplog, "http_operation_failed")
        assert [(row["stage"], row["http_status"]) for row in rejected] == [("auth", 403), ("config_read", 503)]
        assert rejected[0]["generation"] == service.generation and "operation" not in rejected[0]
        assert rejected[1]["operation"] == "context"
        assert "untrusted-SECRET" not in caplog.text and "PRIVATE_TOKEN" not in caplog.text
    finally:
        await runner.cleanup()


async def test_provider_rejection_records_real_status_id_and_no_sensitive_payload(caplog):
    caplog.set_level(logging.INFO, logger="orchestrator.frontend_call")
    app = web.Application()

    async def reject(_request):
        return web.Response(status=429, text="PRIVATE_PROVIDER_BODY SECRET", headers={"X-Request-Id": "req-provider-1", "X-Generation-Id": "gen-provider-1"})

    app.router.add_post("/audio/transcriptions", reject)
    runner, url = await serve(app)
    try:
        with pytest.raises(CallError, match="call_provider_rejected"):
            await MediaAdapters().transcribe({"base_url": url, "model": "PRIVATE_MODEL https://private.example"}, {"options": {}}, base64.b64decode(wav()))
        failed = events(caplog, "provider_request_failed")[-1]
        assert failed["http_status"] == 429 and failed["provider_request_id"] == "req-provider-1"
        assert failed["provider_generation_id"] == "gen-provider-1" and failed["media_kind"] == "stt"
        assert "PRIVATE_PROVIDER_BODY" not in caplog.text and "PRIVATE_MODEL" not in caplog.text
    finally:
        await runner.cleanup()


async def test_real_provider_success_keeps_task_correlation_and_run_polls_are_quiet(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="orchestrator.frontend_call")
    app, requests = web.Application(), []

    async def stt(_request):
        requests.append("stt")
        return web.json_response({"text": "PRIVATE_TRANSCRIPT"}, headers={"X-Generation-Id": "gen-stt-2"})

    async def tts(_request):
        requests.append("tts")
        return web.Response(body=base64.b64decode(wav()), content_type="audio/wav")

    app.router.add_post("/v1/audio/transcriptions", stt)
    app.router.add_post("/v1/audio/speech", tts)
    runner, url = await serve(app)
    service, ports, _, base, _ = setup(tmp_path)
    document = json.loads(service.config.path.read_text())
    for target in document["targets"]:
        target["base_url"] = url + "/v1"
        target.pop("credential_env", None)
    service.config.path.write_text(json.dumps(document))
    service.adapters = MediaAdapters()
    polls = []

    def result(*_args):
        polls.append(1)
        terminal = len(polls) >= 4
        return {"terminal": terminal, "state": "completed" if terminal else "running", "text": "PRIVATE_ANSWER"}

    ports.result = result
    try:
        binding, _, _ = await start(service, base)
        await service.invoke("owner", {**binding, "operation": "turn", "turn_id": "turn-1", "sequence": 1, "audio_b64": wav()})
        await finish_task(service)
        speech = {**binding, "operation": "speech", "turn_id": "turn-1", "segment": 0}
        await service.invoke("owner", speech)
        await service.calls["call-1"].speech_task
        before_polls = len(events(caplog))
        for _ in range(3):
            assert (await service.invoke("owner", speech))["ready"]
            await service.invoke("owner", {**binding, "operation": "snapshot"})
        assert len(events(caplog)) == before_polls
        assert requests == ["stt", "tts"] and len(ports.accepted) == 1 and len(polls) == 4
        states = events(caplog, "run_state_changed")
        assert [row["run_state"] for row in states] == ["running", "completed"]
        received = events(caplog, "provider_request_completed")
        assert len(received) == 2
        for row in received:
            assert row["call_id"] == "call-1" and row["turn_id"] == "turn-1"
            assert row["generation"] == binding["generation"] and row["http_status"] == 200
            assert row["requested_model"] == "fixture-model"
        assert received[0]["provider_generation_id"] == "gen-stt-2"
        assert received[1]["run_id"] == "run-1" and received[1]["segment"] == 0
        assert "PRIVATE_TRANSCRIPT" not in caplog.text and "PRIVATE_ANSWER" not in caplog.text
    finally:
        await service.close()
        await runner.cleanup()


async def test_tts_failure_retry_and_cancellation_record_segment_without_repeating_admission(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="orchestrator.frontend_call")
    service, ports, adapters, base, _ = setup(tmp_path)
    binding, _, _ = await start(service, base)
    turn = {**binding, "operation": "turn", "turn_id": "turn-1", "sequence": 1, "audio_b64": wav()}
    await service.invoke("owner", turn)
    await finish_task(service)
    speech = {**binding, "operation": "speech", "turn_id": "turn-1", "segment": 0}
    adapters.fail_tts = True
    await service.invoke("owner", speech)
    await service.calls["call-1"].speech_task
    failure = events(caplog, "tts_failed")[-1]
    assert failure["error_code"] == "call_provider_rejected" and failure["segment"] == 0
    assert failure["turn_id"] == "turn-1" and failure["run_id"] == "run-1"
    adapters.fail_tts = False
    await service.invoke("owner", {**speech, "retry": True})
    await service.calls["call-1"].speech_task
    assert adapters.tts == 2 and len(ports.accepted) == 1
    assert [row["attempt"] for row in events(caplog, "tts_started")] == [1, 2]
    await service.invoke("owner", {**turn, "turn_id": "turn-2", "sequence": 2})
    await finish_task(service)
    began, release = asyncio.Event(), asyncio.Event()

    async def synthesize(*_args):
        began.set()
        await release.wait()

    adapters.synthesize = synthesize
    await service.invoke("owner", {**speech, "turn_id": "turn-2"})
    await asyncio.wait_for(began.wait(), 1)
    await service.invoke("owner", {**binding, "operation": "end"})
    await service.calls["call-1"].speech_task
    cancelled = events(caplog, "tts_cancelled")[-1]
    assert cancelled["turn_id"] == "turn-2" and cancelled["segment"] == 0
    assert cancelled["reason"] == "call_ended" and len(ports.accepted) == 2
    await service.close()


async def test_vision_failure_and_late_result_are_logged_without_ending_call(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="orchestrator.frontend_call")
    service, _, adapters, base, clock = setup(tmp_path)
    info = service.config.context("owner", "agent-a")
    info["profile"]["vision"] = {"target_id": "eyes", "options": {}}
    service.config.save("owner", "agent-a", info["revision"], info["profile"])
    binding, _, _ = await start(service, base)
    await service.invoke("owner", {**binding, "operation": "camera", "enabled": True})
    image = base64.b64encode(bytes.fromhex("ffd8ffc00008080010001000ffd9")).decode()

    def frame(sequence):
        return {**binding, "operation": "observe", "frame_sequence": sequence, "image_b64": image, "captured_at": datetime.now(timezone.utc).isoformat()}

    async def fail(*_args):
        raise CallError("call_provider_unavailable", 502)

    adapters.observe = fail
    await service.invoke("owner", frame(1))
    call = service.calls["call-1"]
    await call.vision_task
    failure = events(caplog, "vision_failed")[-1]
    assert failure["error_code"] == "call_provider_unavailable" and failure["frame_sequence"] == 1
    assert failure["camera_epoch"] == 1 and call.phase == "active"
    began, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def late(*_args):
        began.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
        return "PRIVATE_LATE_OBSERVATION"

    adapters.observe = late
    clock[0] += 2
    await service.invoke("owner", frame(2))
    await asyncio.wait_for(began.wait(), 1)
    await service.invoke("owner", {**binding, "operation": "camera", "enabled": False})
    await asyncio.wait_for(cancelled.wait(), 1)
    await service.invoke("owner", {**binding, "operation": "camera", "enabled": True})
    release.set()
    await call.vision_task
    discarded = events(caplog, "vision_discarded")[-1]
    assert discarded["reason"] == "epoch_changed" and discarded["camera_epoch"] == 1
    assert discarded["frame_sequence"] == 2 and call.camera_epoch == 3
    assert call.phase == "active" and not call.observation and not events(caplog, "call_ended")
    assert "PRIVATE_LATE_OBSERVATION" not in caplog.text
    await service.close()


async def test_http_parse_validation_and_response_failures_are_logged_once(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="orchestrator.frontend_call")
    api = SimpleNamespace(
        app=web.Application(), config_path=tmp_path / "agents.json", admin_token="test-token",
        live_voice_manager=SimpleNamespace(), _is_governed_profile=lambda: False,
        _check_admin_auth=lambda _request: True, _v1_owner_id=lambda _request: "owner",
    )
    service = register_call_api(api)
    runner, url = await serve(api.app)
    try:
        async with ClientSession() as session:
            for kwargs, status, stage in (
                ({"data": "PRIVATE_BAD_CONTENT"}, 415, "content_type"),
                ({"data": "PRIVATE_BROKEN_JSON", "headers": {"Content-Type": "application/json"}}, 400, "json"),
                ({"data": b"x" * (MAX_BODY + 1), "headers": {"Content-Type": "application/json"}}, 413, "body_read"),
            ):
                before = len(events(caplog, "http_operation_failed"))
                async with session.post(url + "/api/v1/call/operation", **kwargs) as response:
                    assert response.status == status
                failures = events(caplog, "http_operation_failed")
                assert len(failures) == before + 1 and failures[-1]["stage"] == stage
                assert failures[-1]["http_status"] == status
            before = len(events(caplog))
            async with session.post(url + "/api/v1/call/operation", json={"operation": "route", "private": "PRIVATE_BODY"}) as response:
                assert (await response.json())["error_code"] == "call_unknown_field"
            validation = events(caplog)[before:]
            assert len(validation) == 1 and validation[0]["event"] == "operation_failed"
            assert validation[0]["stage"] == "validation"

            async def invalid_response(*_args):
                return {"private": object()}

            service.invoke = invalid_response
            async with session.post(url + "/api/v1/call/operation", json={"operation": "route"}) as response:
                assert response.status == 500 and (await response.json())["error_code"] == "call_internal_error"
            failure = events(caplog, "http_operation_failed")[-1]
            assert failure["stage"] == "response" and failure["exception_type"] == "TypeError"
        assert "PRIVATE_BAD_CONTENT" not in caplog.text and "PRIVATE_BROKEN_JSON" not in caplog.text
        assert "PRIVATE_BODY" not in caplog.text
    finally:
        await runner.cleanup()


async def test_receipt_logging_keeps_only_safe_ids_and_adds_no_lookups(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="orchestrator.frontend_call")
    app, requests = web.Application(), []

    async def stt(_request):
        requests.append("stt")
        return web.json_response({"text": "PRIVATE_TRANSCRIPT"}, headers={"X-Generation-Id": "gen-stt-3", "X-Request-Id": "PRIVATE_REQUEST https://private.example"})

    async def receipt(_request):
        requests.append("receipt")
        return web.json_response({"data": {"id": "gen-stt-3", "provider_name": "PRIVATE_PROVIDER_TEXT TOKEN=SECRET https://private.example"}})

    app.router.add_post("/v1/audio/transcriptions", stt)
    app.router.add_get("/v1/generation", receipt)
    runner, url = await serve(app)
    try:
        monkeypatch.setattr("orchestrator.frontend_call.adapters.OPENROUTER_API_BASE", url + "/v1")
        result = await MediaAdapters().transcribe({"base_url": url + "/v1", "model": "PRIVATE_MODEL https://private.example"}, {"options": {}}, base64.b64decode(wav()))
        assert result["provider_receipt"]["actual_provider"] == "PRIVATE_PROVIDER_TEXT TOKEN=SECRET https://private.example"
        assert requests == ["stt", "receipt"]
        recorded = events(caplog, "provider_receipt")[-1]
        assert recorded["provider_generation_id"] == "gen-stt-3" and recorded["verification"] == "verified"
        assert recorded["lookup_count"] == 1 and recorded["http_status"] == 200
        completed = events(caplog, "provider_request_completed")[-1]
        assert "provider_request_id" not in completed
        assert "actual_provider" not in completed and "requested_model" not in completed
        assert recorded["gateway"] == "OpenRouter"
        for private in ("PRIVATE_TRANSCRIPT", "PRIVATE_REQUEST", "PRIVATE_PROVIDER_TEXT", "PRIVATE_MODEL"):
            assert private not in caplog.text
    finally:
        await runner.cleanup()


async def test_expiry_during_rejected_turn_never_associates_unaccepted_turn_ids(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="orchestrator.frontend_call")
    service, _, _, base, clock = setup(tmp_path)
    binding, _, _ = await start(service, base)
    clock[0] += 46
    with pytest.raises(CallError, match="call_ended"):
        await service.invoke("owner", {**binding, "operation": "turn", "turn_id": "unaccepted-turn", "sequence": 1, "audio_b64": wav()})
    ended = events(caplog, "call_ended")[-1]
    assert ended["reason"] == "lease_expired" and "turn_id" not in ended and "sequence" not in ended
    rejected = events(caplog, "operation_failed")[-1]
    assert rejected["turn_id"] == "unaccepted-turn" and rejected["error_code"] == "call_ended"
    await service.close()


async def test_failed_logging_sink_cannot_interrupt_camera_or_voice_effects(tmp_path, monkeypatch):
    app, requests, sink_calls = web.Application(), [], []

    async def stt(_request):
        requests.append("stt")
        return web.json_response({"text": "Recognized speech."})

    async def tts(_request):
        requests.append("tts")
        return web.Response(body=base64.b64decode(wav()), content_type="audio/wav")

    async def vision(_request):
        requests.append("vision")
        return web.json_response({"choices": [{"message": {"content": "A cup is visible."}}]})

    def broken_sink(*_args, **_kwargs):
        sink_calls.append(1)
        raise RuntimeError("PRIVATE_LOG_HANDLER_ERROR")

    app.router.add_post("/v1/audio/transcriptions", stt)
    app.router.add_post("/v1/audio/speech", tts)
    app.router.add_post("/v1/chat/completions", vision)
    runner, url = await serve(app)
    monkeypatch.setattr("orchestrator.frontend_call.diagnostics.logger.info", broken_sink)
    monkeypatch.setattr("orchestrator.frontend_call.diagnostics.logger.isEnabledFor", lambda _level: True)
    service = None
    try:
        service, ports, _, base, _ = setup(tmp_path)
        document = json.loads(service.config.path.read_text())
        for target in document["targets"]:
            target["base_url"] = url + "/v1"
            target.pop("credential_env", None)
        document["default_profile"]["vision"] = {"target_id": "eyes", "options": {}}
        service.config.path.write_text(json.dumps(document))
        service.adapters = MediaAdapters()
        binding, _, started = await start(service, base)
        assert started["phase"] == "active"
        await service.invoke("owner", {**binding, "operation": "camera", "enabled": True})
        await service.invoke("owner", {
            **binding, "operation": "observe", "frame_sequence": 1,
            "image_b64": base64.b64encode(bytes.fromhex("ffd8ffc00008080010001000ffd9")).decode(),
            "captured_at": datetime.now(timezone.utc).isoformat(),
        })
        call = service.calls["call-1"]
        await call.vision_task
        assert call.observation["text"] == "A cup is visible."
        await service.invoke("owner", {**binding, "operation": "turn", "turn_id": "turn-1", "sequence": 1, "audio_b64": wav()})
        await finish_task(service)
        assert call.turn["phase"] == "complete" and len(ports.accepted) == 1
        speech = {**binding, "operation": "speech", "turn_id": "turn-1", "segment": 0}
        await service.invoke("owner", speech)
        await call.speech_task
        assert (await service.invoke("owner", speech))["ready"]
        await service.invoke("owner", {**binding, "operation": "camera", "enabled": False})
        assert call.phase == "active" and not call.observation
        assert (await service.invoke("owner", {**binding, "operation": "end"}))["phase"] == "ended"
        assert requests == ["vision", "stt", "tts"] and sink_calls
    finally:
        if service:
            await service.close()
        await runner.cleanup()


async def test_shared_provider_endpoint_uses_validated_media_kind_not_path(caplog):
    caplog.set_level(logging.INFO, logger="orchestrator.frontend_call")
    app = web.Application()
    async def complete(_request):
        return web.json_response({"ok": True})

    app.router.add_post("/chat/completions", complete)
    runner, url = await serve(app)
    try:
        await MediaAdapters()._request({"base_url": url, "model": "fixture/model", "kind": "tts"}, "/chat/completions", json_body={"input": "PRIVATE_TTS_TEXT"})
        request = events(caplog, "provider_request_started")[-1]
        assert request["media_kind"] == "tts"
        assert request["requested_model"] == "fixture/model"
        assert "PRIVATE_TTS_TEXT" not in caplog.text
    finally:
        await runner.cleanup()
