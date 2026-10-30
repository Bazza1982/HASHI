"""An L2 channel cannot execute effects before an exact durable boundary."""
import json

import pytest

from orchestrator.permissions.authority import PermissionAuthority, PermissionDenied
from orchestrator.permissions.execution_channel import PermissionExecutionChannel
from orchestrator.permissions.models import RuntimeExecutionBinding, normalize_scope
from orchestrator.permissions.pao_run_fence import PaoRunFence
from orchestrator.permissions.registry import ManagedToolRegistry
from tests.test_permission_execution_gate import Issuer, RecoverableFileSupervisor
from tests.test_permission_pao_fence import setup_run


@pytest.fixture
def channel(tmp_path):
    store, _, accepted, fence, context = setup_run(tmp_path)
    issuer = Issuer()
    scope = normalize_scope("file", str(tmp_path), ("create", "write"), cwd=tmp_path)
    authority = PermissionAuthority(tmp_path / "permissions.sqlite", verifier=issuer,
                                    clock=lambda: 1000.0, monotonic=lambda: 1000.0,
                                    baseline_resolver=lambda current: (scope,))
    authority.open_task(context)
    supervisor = RecoverableFileSupervisor(authority, tmp_path)
    registry = ManagedToolRegistry(
        allowed_tools=["file_write"], access_root=tmp_path, workspace_dir=tmp_path, secrets={},
        audit_context={"agent_name": context.agent_id, "workspace_dir": str(tmp_path), "safety_mode": "read_write"},
        permission_authority=authority, permission_context=context,
        permission_supervisor=supervisor, requestable_scopes=(scope,), durable_wait=True,
    )
    binding = RuntimeExecutionBinding(context, accepted.request_id, fence, "INSTANCE:agent")
    class Models:
        calls = 0
        async def provider_call(self, **kwargs):
            self.calls += 1
            return "provider-result"
    models = Models()
    checkpoint_file = tmp_path / "private-checkpoint.json"
    def save(binding, checkpoint):
        checkpoint_file.write_text(json.dumps(checkpoint), encoding="utf-8")
        return {"committed": True}
    owner = PermissionExecutionChannel(
        binding, authority=authority, run_guard=PaoRunFence(
            store, context=context, request_id=accepted.request_id, fencing_token=fence, worker_id=binding.worker_id,
        ), registry=registry, model_broker=models, checkpoint_writer=save,
        audit_writer=lambda record: "saved", require_checkpoints=True,
    )
    return owner, supervisor, models, checkpoint_file


@pytest.mark.asyncio
async def test_tool_requires_the_exact_saved_call_and_cannot_be_reissued_after_completion(channel, tmp_path):
    owner, supervisor, _, checkpoint_file = channel
    arguments = {"path": "report.txt", "content": "saved"}
    with pytest.raises(PermissionDenied, match="durable pending"):
        await owner.tool_execute(tool_name="file_write", arguments=arguments, tool_call_id="call")
    assert supervisor.execute_calls == 0 and not (tmp_path / "report.txt").exists()
    checkpoint = {"boundary": "pending_tool_batch", "messages": [
        {"role": "assistant", "tool_calls": [{"id": "call", "type": "function",
          "function": {"name": "file_write", "arguments": json.dumps(arguments)}}]},
    ]}
    await owner.provider_checkpoint(checkpoint=checkpoint)
    assert json.loads(checkpoint_file.read_text(encoding="utf-8")) == checkpoint
    with pytest.raises(PermissionDenied, match="exact durable"):
        await owner.tool_execute(tool_name="file_write", arguments={**arguments, "content": "substituted"}, tool_call_id="call")
    result = await owner.tool_execute(tool_name="file_write", arguments=arguments, tool_call_id="call")
    assert not result.is_error and supervisor.execute_calls == 1
    with pytest.raises(PermissionDenied):
        await owner.tool_execute(tool_name="file_write", arguments=arguments, tool_call_id="call")
    assert supervisor.execute_calls == 1


@pytest.mark.asyncio
async def test_model_transport_requires_the_exact_saved_target_and_outbound_messages(channel):
    owner, _, models, _ = channel
    messages = [{"role": "user", "content": "continue"}]
    arguments = {"engine": "deepseek-api", "model": "configured/model", "payload": {"messages": messages},
                 "use_stream": False}
    with pytest.raises(PermissionDenied, match="durable ready"):
        await owner.provider_call(**arguments)
    await owner.provider_checkpoint(checkpoint={
        "boundary": "ready_provider_request", "provider": arguments["engine"], "model": arguments["model"],
        "messages": messages,
    })
    with pytest.raises(PermissionDenied):
        await owner.provider_call(**{**arguments, "model": "different/model"})
    assert models.calls == 0
    assert await owner.provider_call(**arguments) == "provider-result"
    assert models.calls == 1
    with pytest.raises(PermissionDenied):
        await owner.provider_call(**arguments)
    assert models.calls == 1


@pytest.mark.asyncio
async def test_a_failed_checkpoint_writer_does_not_enable_the_pending_effect(channel):
    owner, supervisor, _, _ = channel
    def failed(*args):
        raise OSError("private checkpoint write failed")
    owner._checkpoint = failed
    with pytest.raises(OSError):
        await owner.provider_checkpoint(checkpoint={"boundary": "ready_provider_request", "messages": []})
    with pytest.raises(PermissionDenied):
        await owner.tool_execute(tool_name="file_write", arguments={"path": "report.txt", "content": "saved"}, tool_call_id="call")
    assert supervisor.execute_calls == 0
