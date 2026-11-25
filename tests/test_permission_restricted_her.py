"""The complete HER engine and API adapter park and resume through narrow ports."""
import copy
from dataclasses import replace

import pytest
from types import SimpleNamespace
from tests.permission_forensic_fixture import forensic_port

from adapters.openrouter_api import _APIResult
from orchestrator.her_v2.config import HERv2Config
from orchestrator.her_v2.backend_session import HerBackendSessionCoordinator
from orchestrator.her_v2.models import TerminalState
from orchestrator.permissions.continuation import PermissionPark
from orchestrator.permissions.host_protocol import pack, unpack
from orchestrator.permissions.model_broker import api_result_to_wire
from orchestrator.permissions.restricted_her_host import run_command
from tests.test_permission_execution_gate import RecoverableFileSupervisor
from tests.test_permission_provider_park import durable_registry, tool

from tests.test_permission_registry import environment as environment


def command(registry):
    profiles = {name: {"engine": "deepseek-api", "model": "deepseek-v4-pro"}
                for name in ("lightweight", "triage", "premium", "reviewer", "orchestrator")}
    config = HERv2Config.from_mapping({"profiles": profiles})
    assert unpack(pack(config)) == config
    return {
        "config": pack(config), "profiles": [profiles["premium"]],
        "tool_definitions": registry.get_tool_definitions(), "privacy_level": 1,
        "prompt": "Write the report and confirm it is saved.", "request_id": "request",
        "turn_id": "turn", "effort": "medium", "request_content": None,
        "request_mode": "text", "pcm_input": {}, "recovery": None, "skills_catalogue": [],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("context_refresh", [False, True])
async def test_full_her_host_preserves_pending_batch_without_replanning(environment, tmp_path, context_refresh):
    authority, issuer, context, _, base = environment
    supervisor = RecoverableFileSupervisor(authority, tmp_path)
    registry = durable_registry(base, authority, context, supervisor, tmp_path)
    checkpoints, model_calls, audits = [], [], []
    calls = [tool("report-write", "report.txt")]
    coordinator = HerBackendSessionCoordinator(tmp_path / "trusted-her")
    encoded, _ = coordinator.prepare_transport(
        session_id="her-session", sections=[], resources=[], user_message="Write report",
        request_id="request", message_id="message", instance_id="i", agent_id="agent",
        owner_id="owner", hashi_conversation_id="session", context_generation=1,
        workzone_identity="workspace",
    )
    turn = coordinator.accept(encoded)

    forensic, forensic_store = forensic_port(tmp_path / "trusted-forensics", SimpleNamespace(
        request_id="request", context=SimpleNamespace(turn_id="turn", execution_epoch="host-test")))

    class Connection:
        def call(self, method, arguments):
            if method == "runtime_port" and arguments["operation"] == "provider_forensic":
                return forensic(arguments)
            if method == "runtime_port":
                assert arguments["operation"] == "manifest"
                return pack({"operations": [], "allow_immediate_response": False, "workzone_ref": str(tmp_path)})
            if method == "provider_checkpoint":
                metadata = coordinator.record_provider_context_checkpoint(turn, arguments["checkpoint"])
                checkpoints.append(coordinator.store.provider_context_for_turn(turn.session_id, turn.turn_id))
                return metadata
            assert method == "audit_record"
            coordinator.record_runtime_event(turn, arguments["record"])
            audits.append(copy.deepcopy(arguments["record"]))
            return str(len(audits))

        async def call_async(self, method, arguments, *, on_event=None):
            if method == "tool_execute":
                try:
                    return await registry.execute(**arguments)
                except PermissionPark as park:
                    return park.result
            assert method == "provider_call"
            messages = copy.deepcopy(arguments["payload"]["messages"])
            model_calls.append(messages)
            if len(model_calls) == 1:
                return api_result_to_wire(_APIResult("", calls, "tool_calls"))
            results = [m for m in messages if m.get("role") == "tool"]
            assert [m["tool_call_id"] for m in results] == ["report-write"]
            return api_result_to_wire(_APIResult("Report saved.", None, "stop"))

    events = []

    async def emit(event):
        events.append(event)

    host_root = tmp_path / "host"
    host_root.mkdir()
    payload = command(registry)
    payload["turn_id"] = turn.turn_id
    first = await run_command(payload, client=Connection(), scratch_root=host_root, emit=emit,
                              brokered_forensics=True, execution_epoch=context.execution_epoch)
    assert first["state"] == "parked", first
    assert len(model_calls) == 1
    assert not (tmp_path / "report.txt").exists()
    assert checkpoints[-1]["boundary"] == "pending_tool_batch"
    request = authority.list_requests(context)[0]
    op = authority.operation_status_for_task(context, request.operation_id)
    issuer.approve(authority, context, request, op.arguments_digest)
    authority.fence_execution(context)
    successor = replace(context, execution_epoch="resumed", worker_generation="new-worker")
    authority.bind_execution(successor)
    registry = durable_registry(base, authority, successor, supervisor, tmp_path)
    payload["recovery"] = checkpoints[-1]
    if context_refresh:
        payload["prompt"] = "Fresh PCM context after approval. " + payload["prompt"]
    second = await run_command(payload, client=Connection(), scratch_root=host_root, emit=emit,
                               brokered_forensics=True, execution_epoch=successor.execution_epoch)
    assert {record["payload"].get("execution_epoch") for record in audits} >= {
        context.execution_epoch, successor.execution_epoch,
    }
    assert second["state"] == "completed", second
    result = unpack(second["result"])
    assert result.terminal_state is TerminalState.COMPLETED, result.error
    assert result.text == "Report saved."
    assert len(model_calls) == 2
    assert supervisor.execute_calls == 1
    assert (tmp_path / "report.txt").read_text(encoding="utf-8") == "report-write"
    assert audits and events

    from orchestrator.her_v2.session_store import HerSessionStoreError
    existing = next(row for row in audits if row["event"] == "request_received")
    with pytest.raises(HerSessionStoreError, match="different immutable facts"):
        coordinator.record_runtime_event(turn, {
            **existing, "payload": {**existing["payload"], "request": "changed in the same epoch"},
        })


@pytest.mark.asyncio
async def test_service_loss_before_first_model_answer_retains_the_exact_ready_request(environment, tmp_path):
    authority, _, context, _, base = environment
    supervisor = RecoverableFileSupervisor(authority, tmp_path)
    registry = durable_registry(base, authority, context, supervisor, tmp_path)
    coordinator = HerBackendSessionCoordinator(tmp_path / "private-her")
    encoded, _ = coordinator.prepare_transport(
        session_id="her-session", sections=[], resources=[], user_message="Answer safely",
        request_id="request", message_id="message", instance_id="i", agent_id="agent",
        owner_id="owner", hashi_conversation_id="session", context_generation=1,
        workzone_identity="workspace",
    )
    turn = coordinator.accept(encoded)
    checkpoints, messages = [], []

    forensic, forensic_store = forensic_port(tmp_path / "trusted-forensics", SimpleNamespace(
        request_id="request", context=SimpleNamespace(turn_id="turn", execution_epoch="host-test")))

    class Connection:
        def call(self, method, arguments):
            if method == "runtime_port" and arguments["operation"] == "provider_forensic":
                return forensic(arguments)
            if method == "runtime_port":
                assert arguments["operation"] == "manifest"
                return pack({"operations": [], "allow_immediate_response": False, "workzone_ref": str(tmp_path)})
            if method == "provider_checkpoint":
                result = coordinator.record_provider_context_checkpoint(turn, arguments["checkpoint"])
                checkpoints.append(coordinator.store.provider_context_for_turn(turn.session_id, turn.turn_id))
                return result
            assert method == "audit_record"
            return "audit"

        async def call_async(self, method, arguments, *, on_event=None):
            assert method == "provider_call"
            messages.append(copy.deepcopy(arguments["payload"]["messages"]))
            if len(messages) == 1:
                raise OSError("private service connection lost")
            return api_result_to_wire(_APIResult("Continued safely.", None, "stop"))

    async def emit(event):
        pass
    root = tmp_path / "host"
    root.mkdir()
    payload = command(registry)
    first = await run_command(payload, client=Connection(), scratch_root=root, emit=emit,
                              brokered_forensics=True)
    assert first["state"] == "parked"
    assert checkpoints[-1]["boundary"] == "ready_provider_request"
    assert supervisor.execute_calls == 0
    payload["recovery"] = checkpoints[-1]
    second = await run_command(payload, client=Connection(), scratch_root=root, emit=emit,
                               brokered_forensics=True)
    assert second["state"] == "completed"
    assert unpack(second["result"]).text == "Continued safely."
    assert messages[0] == messages[1]
    assert supervisor.execute_calls == 0


@pytest.mark.asyncio
async def test_restricted_successor_steer_keeps_completed_result_without_replay(tmp_path):
    import json

    wire, checkpoints = [], []
    calls = [tool("done-write", "done.txt"), tool("pending-write", "pending.txt")]

    class Connection:
        def call(self, method, arguments):
            if method == "runtime_port":
                assert arguments["operation"] == "manifest"
                return pack({"operations": [], "allow_immediate_response": False,
                             "workzone_ref": str(tmp_path)})
            if method == "provider_checkpoint":
                checkpoints.append(copy.deepcopy(arguments["checkpoint"]))
                return {}
            assert method == "audit_record"
            return "audit"

        async def call_async(self, method, arguments, *, on_event=None):
            assert method == "provider_call", "steering must not replay either tool"
            wire.append(copy.deepcopy(arguments["payload"]["messages"]))
            return api_result_to_wire(_APIResult("Continued safely.", None, "stop"))

    payload = command(SimpleNamespace(get_tool_definitions=lambda: []))
    payload["prompt"] = "Stop the pending write and verify"
    payload["pcm_input"] = {
        "current_request": payload["prompt"], "history": [],
        "sections": [{"key": "reference", "authority": "runtime_context",
                      "title": "Reference", "text": "OLD_REFERENCE_MARKER", "order": 0}],
        "runtime_context": {"active_turn_recovery": {"safe_to_resume": False}},
    }
    payload["recovery"] = {
        "format": "herv3-provider-context-v1", "provider": "deepseek-api",
        "model": "deepseek-v4-pro", "stage": "direct", "boundary": "pending_tool_batch",
        "messages": [
            {"role": "system", "content": "prior system"},
            {"role": "user", "content": "Original task with OLD_REFERENCE_MARKER"},
            {"role": "assistant", "content": "", "tool_calls": calls},
            {"role": "tool", "tool_call_id": "done-write", "content": "Written earlier."},
        ],
        "deferred_followups": [{"role": "user", "content": "Old write advisory."}],
        "_herv3_continuation": {"successor_turn": True, "resume_pending_tools": False,
                                "pcm_delta": []},
    }

    async def emit(_event):
        pass

    result = await run_command(payload, client=Connection(), scratch_root=tmp_path, emit=emit)
    assert result["state"] == "completed"
    assert unpack(result["result"]).text == "Continued safely."
    assert len(wire) == 1
    outgoing = wire[0]
    assert [row["tool_call_id"] for row in outgoing if row.get("role") == "tool"] == [
        "done-write", "pending-write",
    ]
    encoded = json.dumps(outgoing, ensure_ascii=False)
    assert encoded.count("OLD_REFERENCE_MARKER") == 1
    assert "Written earlier." in encoded
    assert encoded.index("Old write advisory.") < encoded.index(payload["prompt"])
    assert "verify" in next(row["content"] for row in outgoing
                            if row.get("tool_call_id") == "pending-write").lower()
    assert not (tmp_path / "pending.txt").exists()
    assert checkpoints


@pytest.mark.asyncio
async def test_restricted_host_receives_trusted_successor_continuation(tmp_path, monkeypatch):
    from adapters.her_v2_provider import HashiStageProvider

    class Bound(Exception):
        pass

    seen = {}

    def capture(_self, **kwargs):
        seen.update(kwargs)
        raise Bound

    class Connection:
        def call(self, method, arguments):
            assert method == "runtime_port" and arguments["operation"] == "manifest"
            return pack({"operations": [], "allow_immediate_response": False, "workzone_ref": str(tmp_path)})

    monkeypatch.setattr(HashiStageProvider, "bind_provider_context", capture)
    payload = command(SimpleNamespace(get_tool_definitions=lambda: []))
    payload["recovery"] = {
        "format": "herv3-provider-context-v1", "boundary": "pending_tool_batch",
        "_herv3_continuation": {
            "successor_turn": True, "resume_pending_tools": False,
            "pcm_delta": [{"op": "remove", "key": "reference"}],
        },
    }

    async def emit(_event):
        pass

    with pytest.raises(Bound):
        await run_command(payload, client=Connection(), scratch_root=tmp_path, emit=emit)
    assert seen["successor_turn"] is True
    assert seen["resume_pending_tools"] is False
    assert seen["pcm_delta"] == [{"op": "remove", "key": "reference"}]
    assert "_herv3_continuation" not in seen["recovery"]


def test_host_codec_rejects_unregistered_classes_and_changed_config_fields():
    with pytest.raises(ValueError):
        unpack(["dto", "PermissionAuthority", []])
    with pytest.raises(ValueError):
        unpack(["dto", "ProviderTarget", [["engine", ["scalar", "deepseek-api"]]]])
# HASHI-Fingerprint: hfp_e51db88258b84978a4bd93c276486b55
