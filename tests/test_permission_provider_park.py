"""A real provider loop parks at a durable Tool boundary and resumes that batch."""
from dataclasses import replace
import copy
import json

import pytest

from adapters.openrouter_api import _APIResult
from orchestrator.her_v2.provider_continuation import pending_tool_calls
from orchestrator.permissions.continuation import PermissionPark
from orchestrator.permissions.descriptors import describe_tool
from orchestrator.permissions.registry import ManagedToolRegistry
from tests.test_deepseek_api import _adapter
from tests.test_permission_execution_gate import RecoverableFileSupervisor
pytest_plugins = ("tests.test_permission_registry",)


def durable_registry(base, authority, context, supervisor, tmp_path):
    return ManagedToolRegistry(
        allowed_tools=["file_write"], access_root=tmp_path, workspace_dir=tmp_path, secrets={},
        audit_context={"agent_name": "agent", "workspace_dir": str(tmp_path), "safety_mode": "read_write"},
        permission_authority=authority, permission_context=context, permission_supervisor=supervisor,
        requestable_scopes=base._permission_gate._requestable, durable_wait=True,
    )


def tool(identity, path):
    return {"id": identity, "type": "function",
            "function": {"name": "file_write", "arguments": json.dumps({"path": path, "content": identity})}}


@pytest.mark.asyncio
async def test_pending_batch_survives_new_epoch_without_repeating_completed_write(environment, tmp_path, monkeypatch):
    authority, issuer, context, _, base = environment
    supervisor = RecoverableFileSupervisor(authority, tmp_path)
    registry = durable_registry(base, authority, context, supervisor, tmp_path)
    calls = [tool("first-call", "first.txt"), tool("second-call", "second.txt")]
    first_args = json.loads(calls[0]["function"]["arguments"])
    descriptor = describe_tool("file_write", first_args, cwd=tmp_path, read_roots=(tmp_path,))
    first_op = registry._operation_id("first-call")
    request = authority.request_access(context, descriptor.scopes, purpose="first",
                                       operation_id=first_op, arguments_digest=descriptor.arguments_digest)
    issuer.approve(authority, context, request, descriptor.arguments_digest)
    adapter = _adapter(tmp_path)
    adapter.tool_registry = registry
    checkpoints, provider_calls = [], []
    adapter.set_provider_context_observer(lambda checkpoint: checkpoints.append(copy.deepcopy(checkpoint)))

    async def initial(payload, headers, callback):
        provider_calls.append(copy.deepcopy(payload["messages"]))
        return _APIResult("", calls, "tool_calls")

    monkeypatch.setattr(adapter, "_stream_api_once", initial)
    with pytest.raises(PermissionPark) as caught:
        await adapter.generate_response("Write both files", "request", on_stream_event=lambda event: _nothing())
    assert len(provider_calls) == 1
    assert (tmp_path / "first.txt").read_text(encoding="utf-8") == "first-call"
    assert not (tmp_path / "second.txt").exists()
    checkpoint = checkpoints[-1]
    assert checkpoint["boundary"] == "pending_tool_batch"
    assert [call["id"] for call in pending_tool_calls(checkpoint["messages"])] == ["second-call"]
    pending = authority.get_request(context, caught.value.result.request_id)
    status = authority.operation_status_for_task(context, pending.operation_id)
    issuer.proofs["second-proof"] = replace(
        issuer.proofs["proof"], nonce="second-approval", idempotency_key="second-approval",
        request_id=pending.request_id, request_revision=pending.revision,
        requested_digest=pending.scope_digest, approved_digest=pending.scope_digest,
        operation_id=pending.operation_id, arguments_digest=status.arguments_digest,
    )
    authority.decide("second-proof", approved_scopes=pending.scopes)

    # Host replacement keeps the Run and operation, fences only the old epoch.
    authority.fence_execution(context)
    successor = replace(context, execution_epoch="new-epoch", worker_generation="new-worker")
    authority.bind_execution(successor)
    next_registry = durable_registry(base, authority, successor, supervisor, tmp_path)
    resumed = _adapter(tmp_path)
    resumed.tool_registry = next_registry
    resumed.set_provider_context_observer(lambda value: checkpoints.append(copy.deepcopy(value)))

    async def after_tools(payload, headers, callback):
        provider_calls.append(copy.deepcopy(payload["messages"]))
        results = [m for m in payload["messages"] if m["role"] == "tool"]
        assert [row["tool_call_id"] for row in results] == ["first-call", "second-call"]
        return _APIResult("Both files saved.", None, "stop")

    monkeypatch.setattr(resumed, "_stream_api_once", after_tools)
    response = await resumed.generate_response(
        "Write both files", "request", continuation_messages=checkpoint["messages"],
        resume_pending_tools=True, on_stream_event=lambda event: _nothing(),
    )
    assert response.is_success, response.error
    assert len(provider_calls) == 2, "No model polling or repeated planning"
    assert supervisor.execute_calls == 2
    assert (tmp_path / "first.txt").read_text(encoding="utf-8") == "first-call"
    assert (tmp_path / "second.txt").read_text(encoding="utf-8") == "second-call"
    assert authority.decision_count(pending.request_id) == 1


async def _nothing():
    return None


@pytest.mark.parametrize("messages", [
    [{"role": "assistant", "tool_calls": [tool("call", "x")]}, {"role": "user", "content": "continue"}],
    [{"role": "assistant", "tool_calls": [tool("call", "x"), tool("call", "y")]}],
    [{"role": "tool", "tool_call_id": "orphan", "content": "pretend done"}],
])
def test_incomplete_or_ambiguous_recovery_protocol_is_rejected(messages):
    with pytest.raises(ValueError):
        pending_tool_calls(messages)
