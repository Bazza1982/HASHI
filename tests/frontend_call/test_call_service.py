import asyncio
import json
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

    async def admit(self, owner, binding, turn, text, observation, captured_at):
        self.accepted.append((turn, text, observation, captured_at))
        return {"run_id": "run-1", "request_id": "req-1", "message_id": "msg-1"}

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

    async def transcribe(self, *args):
        self.stt += 1
        if self.wait:
            await self.wait.wait()
        return {
            "text": "Please check the task.",
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


def setup(tmp_path, location="local"):
    path = tmp_path / "call_profiles.json"
    path.write_text(json.dumps(document(location)))
    config = CallConfig(path)
    ports = Ports()
    adapters = Adapters()
    clock = [100.0]
    service = CallService(
        config, ports, adapters, clock=lambda: clock[0], poll_seconds=0.001
    )
    base = {
        "client_id": "client-1",
        "agent_id": "agent-a",
        "session_id": "sess-a",
        "context_generation": 1,
    }
    return service, ports, adapters, base, clock


async def start(service, base, cloud=False):
    info = await service.invoke("owner", {**base, "operation": "context"})
    if info.get("route") != "call":
        service.config.select_route("owner", base["agent_id"], info["revision"], "call")
        info = await service.invoke("owner", {**base, "operation": "context"})
    body = {
        **base,
        "operation": "start",
        "call_id": "call-1",
        "generation": info["generation"],
        "revision": info["revision"],
        "allow_cloud": cloud,
    }
    result = await service.invoke("owner", body)
    return {**base, "call_id": "call-1", "generation": info["generation"]}, body, result


async def finish_task(service):
    await service.calls["call-1"].task


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
        await service.invoke("owner", {**binding, "operation": "turn",
            "turn_id": "turn-1", "sequence": 1, "audio_b64": wav()})
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
        turn = {**binding, "operation": "turn", "turn_id": "turn-1",
                "sequence": 1, "audio_b64": wav()}
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
        await service.invoke("owner", {**binding, "operation": "turn",
            "turn_id": "turn-1", "sequence": 1, "audio_b64": wav()})
        await finish_task(service)
        call = service.calls["call-1"]
        assert call.speech_task is not None
        await call.speech_task
        assert call.turn["phase"] == "complete" and call.turn["answer"]
        assert adapters.tts == 0 and not call.speech_cache
        assert "privacy" in call.speech_errors[0]
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
