from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from adapters.deepseek_api import DeepSeekAdapter
from adapters.her_v2_provider import HashiStageProvider
from adapters.openrouter_api import _APIResult
from orchestrator.her_v2.backend_session import HerBackendSessionCoordinator
from orchestrator.her_v2.config import ProviderProfile
from orchestrator.her_v2.models import Effort, Stage, StageRequest, TriageClassification
from orchestrator.her_v2.provider_continuation import pending_tool_calls
from orchestrator.her_v2.session_store import HerSessionStore
from orchestrator.permissions.continuation import PermissionPark
from orchestrator.permissions.execution_gate import GateResult
from tools.registry import ToolResult


PROFILE = ProviderProfile("execution", "deepseek-api", "deepseek-v4-pro")


def _request(goal="Follow the new instruction"):
    return StageRequest(
        turn_id="test-turn",
        request_ref="test-request",
        stage=Stage.EXECUTION,
        role="execution",
        attempt=1,
        goal=goal,
        classification=TriageClassification.COMPLEX_TASK,
        effort=Effort.HIGH,
        context={},
        allow_tools=False,
        allow_side_effects=False,
    )


def _call(call_id, name="file_read"):
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps({"path": call_id}),
        },
    }


def _accept(coordinator, request_id, user_message):
    encoded, _ = coordinator.prepare_transport(
        session_id="test-session",
        sections=[{
            "key": "persona",
            "title": "PERSONA",
            "text": "Friendly",
            "authority": "persona",
            "order": 0,
        }],
        resources=[],
        user_message=user_message,
        request_id=request_id,
        message_id="message-" + request_id,
        instance_id="test-instance",
        agent_id="test-agent",
        owner_id="test-owner",
        hashi_conversation_id="test-conversation",
        context_generation=1,
        workzone_identity="test-workzone",
    )
    return coordinator.accept(encoded)


def _new_store(tmp_path):
    coordinator = HerBackendSessionCoordinator(tmp_path / "state")
    return coordinator, _accept(coordinator, "test-turn", "Read the files, then write")


def _claim(state="running"):
    return {
        "run_id": "run-test-turn",
        "request_id": "test-turn",
        "attempt": 1,
        "attempt_id": "run-test-turn:attempt:1",
        "state_revision": 1,
        "input_digest": "sha256:test-input",
        "state": state,
    }


def _observer(coordinator, accepted):
    claim = _claim()

    def save(checkpoint):
        if checkpoint["boundary"] != "pending_tool_batch":
            return
        coordinator.record_provider_context_checkpoint(
            accepted,
            {
                **checkpoint,
                "provider": PROFILE.engine,
                "model": PROFILE.model,
                "stage": "execution",
                "invocation_id": "test-turn:execution:1",
                "producer_run_claim": claim,
            },
        )

    return save


def _read_checkpoint(coordinator, accepted):
    reopened = HerSessionStore(coordinator.store.path)
    result = reopened.provider_context_for_turn(
        accepted.session_id, accepted.turn_id
    )
    assert result is not None
    return result


class Registry:
    permission_durable_wait = True
    supports_parallel_readonly = True

    def __init__(self, executed, *, fails=None, wait_for=None):
        self.executed = executed
        self.fails = dict(fails or {})
        self.wait_for = wait_for
        self.read_a_done = asyncio.Event()

    def get_tool_definitions(self, tiers=None):
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": name,
                    "parameters": {"type": "object", "properties": {}},
                },
            }
            for name in ("file_read", "file_write")
        ]

    async def execute(self, tool_name, arguments, tool_call_id=""):
        self.executed.append(tool_call_id)
        if tool_call_id == self.wait_for:
            await self.read_a_done.wait()
            await asyncio.sleep(0)
        if tool_call_id in self.fails:
            if self.fails[tool_call_id] == "park":
                raise PermissionPark(GateResult("waiting", "op-" + tool_call_id, "req-" + tool_call_id))
            raise RuntimeError("read interrupted")
        if tool_call_id in {"r1", "r3", "readA"}:
            self.read_a_done.set()
        return ToolResult(
            tool_call_id=tool_call_id,
            output="completed " + tool_call_id,
            details={"smart_repeat_warning": "same_result_repeated"} if tool_name == "file_read" else {},
        )


def _adapter(tmp_path, registry, monkeypatch):
    config = SimpleNamespace(
        name="test", engine="deepseek-api", workspace_dir=tmp_path,
        model=PROFILE.model, system_md=None, extra={},
    )
    adapter = DeepSeekAdapter(config, SimpleNamespace(), api_key="test-key")
    adapter.TRANSIENT_PROVIDER_CALL_RETRY_DELAY_S = 0
    adapter.tool_registry = registry
    translate = adapter._tool_result_messages

    def with_image(*, tool_name, tool_call_id, output, content=None):
        result = translate(
            tool_name=tool_name, tool_call_id=tool_call_id,
            output=output, content=content,
        )
        if tool_name == "file_read":
            result.append({
                "role": "user",
                "content": [{
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64," + tool_call_id},
                }],
            })
        return result

    monkeypatch.setattr(adapter, "_tool_result_messages", with_image)
    return adapter


def _brokered_registry(tmp_path, registry):
    from orchestrator.permissions.remote_registry import BrokeredToolRegistry

    class Broker:
        async def call_async(self, method, arguments, **options):
            assert method == "tool_execute"
            try:
                return await registry.execute(
                    arguments["tool_name"], arguments["arguments"],
                    tool_call_id=arguments["tool_call_id"],
                )
            except PermissionPark as park:
                return park.result

    return BrokeredToolRegistry(
        Broker(), definitions=registry.get_tool_definitions(), scratch_root=tmp_path
    )


async def _start(adapter, calls, monkeypatch):
    async def provider(payload, headers, callback):
        return _APIResult("", calls, "tool_calls")

    monkeypatch.setattr(adapter, "_stream_api_once", provider)
    return await adapter.generate_response(
        "Read the files, then write", "test-request",
        on_stream_event=lambda event: asyncio.sleep(0),
    )


async def _resume(adapter, checkpoint, monkeypatch, captured):
    async def provider(payload, headers, callback):
        captured.append(copy.deepcopy(payload["messages"]))
        return _APIResult("done", None, "stop")

    monkeypatch.setattr(adapter, "_stream_api_once", provider)
    return await adapter.generate_response(
        "continue", "test-request",
        continuation_messages=checkpoint["messages"],
        deferred_followups=checkpoint["deferred_followups"],
        resume_pending_tools=True,
        on_stream_event=lambda event: asyncio.sleep(0),
    )


def _followup_ids(messages):
    images = [
        item["content"][0]["image_url"]["url"].split(",")[-1]
        for item in messages
        if item.get("role") == "user"
        and isinstance(item.get("content"), list)
        and item["content"] and item["content"][0].get("type") == "image_url"
    ]
    advisories = [
        next((call_id for call_id in ("r1", "r2", "r3", "r4", "readA", "readB")
              if 'tool_call_id="' + call_id + '"' in item.get("content", "")), None)
        for item in messages
        if item.get("role") == "system"
        and "[HASHI-generated tool advisory" in item.get("content", "")
    ]
    return images, advisories


@pytest.mark.asyncio
async def test_persisted_steer_retains_completed_followups_without_replaying_tool(tmp_path, monkeypatch):
    coordinator, accepted = _new_store(tmp_path)
    executed = []
    first = _adapter(tmp_path, Registry(executed, fails={"write": "park"}), monkeypatch)
    first.set_provider_context_observer(_observer(coordinator, accepted))
    with pytest.raises(PermissionPark):
        await _start(first, [_call("r1"), _call("r2"), _call("write", "file_write")], monkeypatch)
    del first
    checkpoint = _read_checkpoint(coordinator, accepted)
    assert [call["id"] for call in pending_tool_calls(checkpoint["messages"])] == ["write"]

    # Exercise the actual persisted /steer transition and successor adoption,
    # then read the inherited checkpoint from a newly opened SQLite store.
    closed = coordinator.steer(accepted, reason="Stop writing")
    assert closed["status"] == "steered"
    successor = _accept(coordinator, "test-successor", "Stop writing; inspect the reads instead")
    reopened = HerSessionStore(coordinator.store.path)
    recovery = reopened.consume_recovery_context(
        session_id=successor.session_id,
        current_turn_id=successor.turn_id,
        source_run_claim=_claim("superseded"),
    )
    assert recovery["interrupted_turn_id"] == accepted.turn_id
    checkpoint = reopened.provider_context_for_turn(successor.session_id, successor.turn_id)
    assert checkpoint is not None

    captured = []
    fresh = _adapter(tmp_path, Registry(executed), monkeypatch)

    async def provider(payload, headers, callback):
        captured.append(copy.deepcopy(payload["messages"]))
        return _APIResult('{"disposition":"COMPLETED","summary":"done"}', None, "stop")

    monkeypatch.setattr(fresh, "_stream_api_once", provider)

    class Manager:
        privacy_level = 1

        def create_ephemeral_backend(self, engine, target_model=None):
            assert (engine, target_model) == (PROFILE.engine, PROFILE.model)
            return fresh

    stage = HashiStageProvider(backend_manager=Manager())
    stage.bind_provider_context(
        observer=lambda checkpoint: None,
        recovery=checkpoint,
        resume_pending_tools=False,
    )
    await stage.invoke(PROFILE, _request("Stop writing; inspect the reads instead"))
    assert executed.count("write") == 1
    assert len(captured) == 1
    messages = captured[0]
    assert [item["tool_call_id"] for item in messages if item.get("role") == "tool"] == ["r1", "r2", "write"]
    images, advisories = _followup_ids(messages)
    assert images == ["r1", "r2"]
    assert advisories == ["r1", "r2"]
    assert "Stop writing; inspect the reads instead" in messages[-1]["content"]
    assert messages[-1]["role"] == "user"
    assert max(i for i, item in enumerate(messages) if item.get("role") == "tool") < next(
        i for i, item in enumerate(messages) if item.get("role") == "user" and isinstance(item.get("content"), list)
    ) < len(messages) - 1


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupt_again", [False, True])
async def test_persisted_followups_stay_chronological_across_resumes(tmp_path, monkeypatch, interrupt_again):
    coordinator, accepted = _new_store(tmp_path)
    executed = []
    calls = [_call("r1"), _call("r2"), _call("write", "file_write"), _call("r3"), _call("r4")]
    first = _adapter(tmp_path, Registry(executed, fails={"write": "park"}), monkeypatch)
    first.set_provider_context_observer(_observer(coordinator, accepted))
    with pytest.raises(PermissionPark):
        await _start(first, calls, monkeypatch)
    del first
    checkpoint = _read_checkpoint(coordinator, accepted)
    captured = []
    next_registry = Registry(executed, fails={"r4": "park"} if interrupt_again else {}, wait_for="r4" if interrupt_again else None)
    second = _adapter(tmp_path, next_registry, monkeypatch)
    second.set_provider_context_observer(_observer(coordinator, accepted))
    if interrupt_again:
        with pytest.raises(PermissionPark):
            await _resume(second, checkpoint, monkeypatch, captured)
        del second
        checkpoint = _read_checkpoint(coordinator, accepted)
        assert [call["id"] for call in pending_tool_calls(checkpoint["messages"])] == ["r4"]
        third = _adapter(tmp_path, Registry(executed), monkeypatch)
        third.set_provider_context_observer(_observer(coordinator, accepted))
        response = await _resume(third, checkpoint, monkeypatch, captured)
    else:
        response = await _resume(second, checkpoint, monkeypatch, captured)
    assert response.is_success, response.error
    messages = captured[-1]
    assert [item["tool_call_id"] for item in messages if item.get("role") == "tool"] == ["r1", "r2", "write", "r3", "r4"]
    assert _followup_ids(messages) == (["r1", "r2", "r3", "r4"], ["r1", "r2", "r3", "r4"])
    assert executed.count("r1") == executed.count("r2") == executed.count("r3") == 1
    assert executed.count("r4") == (2 if interrupt_again else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["park", "exception"])
async def test_parallel_completed_read_survives_real_sqlite_restart(tmp_path, monkeypatch, failure):
    coordinator, accepted = _new_store(tmp_path)
    executed = []
    registry = Registry(executed, fails={"readB": failure}, wait_for="readB")
    first = _adapter(tmp_path, _brokered_registry(tmp_path, registry), monkeypatch)
    first.set_provider_context_observer(_observer(coordinator, accepted))
    if failure == "park":
        with pytest.raises(PermissionPark):
            await _start(first, [_call("readA"), _call("readB")], monkeypatch)
    else:
        failed = await _start(first, [_call("readA"), _call("readB")], monkeypatch)
        assert not failed.is_success
    del first
    checkpoint = _read_checkpoint(coordinator, accepted)
    assert [item["tool_call_id"] for item in checkpoint["messages"] if item.get("role") == "tool"] == ["readA"]
    assert _followup_ids(checkpoint["deferred_followups"]) == (["readA"], ["readA"])
    assert [call["id"] for call in pending_tool_calls(checkpoint["messages"])] == ["readB"]
    captured = []
    fresh = _adapter(tmp_path, _brokered_registry(tmp_path, Registry(executed)), monkeypatch)
    fresh.set_provider_context_observer(_observer(coordinator, accepted))
    response = await _resume(fresh, checkpoint, monkeypatch, captured)
    assert response.is_success, response.error
    assert executed.count("readA") == 1
    assert executed.count("readB") == 2
    assert [item["tool_call_id"] for item in captured[-1] if item.get("role") == "tool"] == ["readA", "readB"]
    assert _followup_ids(captured[-1]) == (["readA", "readB"], ["readA", "readB"])


@pytest.mark.asyncio
async def test_checkpoint_write_failure_never_claims_parallel_progress(tmp_path, monkeypatch):
    from adapters.openrouter_api import ProviderContextObserverError

    coordinator, accepted = _new_store(tmp_path)
    registry = Registry([], fails={"readB": "park"}, wait_for="readB")
    adapter = _adapter(tmp_path, _brokered_registry(tmp_path, registry), monkeypatch)
    persist = _observer(coordinator, accepted)

    def failing_observer(checkpoint):
        if any(item.get("tool_call_id") == "readA" for item in checkpoint["messages"]):
            raise OSError("storage unavailable")
        persist(checkpoint)

    adapter.set_provider_context_observer(failing_observer)
    with pytest.raises(ProviderContextObserverError):
        await _start(adapter, [_call("readA"), _call("readB")], monkeypatch)


@pytest.mark.parametrize("bad", [
    "corrupt", {"role": "user"}, [{"role": "user"}, 42],
    [{"role": "user", "content": [17]}],
])
def test_invalid_deferred_input_does_not_silently_disappear(bad):
    from adapters.openrouter_api import ProviderContextObserverError

    stage = HashiStageProvider(backend_manager=SimpleNamespace())
    stage.bind_provider_context(
        observer=None,
        recovery={
            "format": "herv3-provider-context-v1",
            "provider": PROFILE.engine,
            "model": PROFILE.model,
            "stage": "execution",
            "boundary": "pending_tool_batch",
            "messages": [{"role": "assistant", "tool_calls": [_call("write", "file_write")]}],
            "deferred_followups": bad,
        },
        resume_pending_tools=False,
    )
    with pytest.raises(ProviderContextObserverError):
        stage._provider_recovery_messages(profile=PROFILE, request=_request(), stage_prompt="steer")


def test_old_checkpoint_without_deferred_field_stays_compatible():
    stage = HashiStageProvider(backend_manager=SimpleNamespace())
    stage.bind_provider_context(
        observer=None,
        recovery={
            "format": "herv3-provider-context-v1",
            "provider": PROFILE.engine,
            "model": PROFILE.model,
            "stage": "execution",
            "boundary": "pending_tool_batch",
            "messages": [{"role": "assistant", "tool_calls": [_call("write", "file_write")]}],
        },
        resume_pending_tools=False,
    )
    result = stage._provider_recovery_messages(profile=PROFILE, request=_request(), stage_prompt="steer")
    assert result[-2]["tool_call_id"] == "write"
    assert result[-1] == {"role": "user", "content": "steer"}
# HASHI-Fingerprint: hfp_a517a743a4ad422fa34c5c80aeb23638
