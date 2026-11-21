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
async def test_question_availability_is_current_and_bound_to_this_run(channel):
    from orchestrator.permissions.host_protocol import unpack
    from orchestrator.permissions.questions import ScopedRunQuestions

    owner, _, _, _ = channel
    questions = ScopedRunQuestions(owner._guard._store, owner.binding, owner._guard)
    owner.registry._ordinary_questions = questions
    args = {"args": [], "kwargs": {}}
    assert unpack(await owner.runtime_port(operation="question_availability", arguments=args)) is True
    questions.questions.set_question_policy(
        scope="agent", agent_id=owner.binding.context.agent_id, enabled=False,
    )
    assert unpack(await owner.runtime_port(operation="question_availability", arguments=args)) is False
    questions.questions.set_question_policy(
        scope="agent", agent_id=owner.binding.context.agent_id, enabled=True,
    )
    questions.questions.set_question_policy(
        scope="instance", agent_id=owner.binding.context.agent_id, enabled=False,
    )
    assert unpack(await owner.runtime_port(operation="question_availability", arguments=args)) is False
    with pytest.raises(PermissionDenied):
        await owner.runtime_port(
            operation="question_availability",
            arguments={"args": [], "kwargs": {"agent_id": "another-agent"}},
        )
    owner._guard._store.cancel_run(
        owner.binding.context.run_id, owner_id=owner.binding.context.owner_id, reason="test stop",
    )
    with pytest.raises(PermissionDenied):
        await owner.runtime_port(operation="question_availability", arguments=args)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["L1", "L2"])
async def test_restricted_stage_prompt_reads_current_bound_question_policy(channel, tmp_path, mode, caplog):
    from adapters.her_v2_provider import HashiStageProvider
    from orchestrator.her_v2.config import ProviderProfile
    from orchestrator.her_v2.models import Stage, StageRequest
    from orchestrator.her_v2.v3_prompt import QUESTION_CONTRACT
    from orchestrator.permissions.clients import RemoteRunExecutor
    from orchestrator.permissions.host_protocol import pack
    from orchestrator.permissions.questions import ScopedRunQuestions
    from orchestrator.permissions.remote_registry import BrokeredToolRegistry
    from orchestrator.permissions.runtime_ports import HostRuntimePorts
    from tests.test_her_v2_adapter import _FakeManager, _stage_request
    from tools.schemas import TOOL_SCHEMA_MAP

    owner, _, _, _ = channel
    questions = ScopedRunQuestions(owner._guard._store, owner.binding, owner._guard)
    owner.registry._ordinary_questions = questions

    class Client:
        def call(self, method, arguments):
            assert method == "runtime_port" and arguments["operation"] == "manifest"
            return pack({"operations": [], "allow_immediate_response": False,
                         "workzone_ref": str(tmp_path)})

        async def call_async(self, method, arguments):
            assert method == "runtime_port"
            return await owner.runtime_port(**arguments)

    client = Client()
    definitions = [TOOL_SCHEMA_MAP[name] for name in ("ask_user", "get_user_answer")]
    registry = BrokeredToolRegistry(client, definitions=definitions, scratch_root=tmp_path)
    manager = _FakeManager()
    provider = HashiStageProvider(backend_manager=manager, tool_registry=registry)
    if mode == "L1":
        executor = RemoteRunExecutor.__new__(RemoteRunExecutor)
        executor._execution_client = client
        executor.registry = registry
        executor.bind_provider(provider)
    else:
        ports = HostRuntimePorts(client)
        provider.bind_question_availability_port(lambda: ports._async("question_availability"))

    base = _stage_request(Stage.DIRECT, allow_tools=True, allow_side_effects=True)
    request = StageRequest(**{
        **base.__dict__,
        "context": {"her_v3": True, "pcm_input": {"current_request": "Hello"}},
    })
    profile = ProviderProfile("main", "openrouter-api", "configured/model")
    for enabled in (True, False, True):
        questions.questions.set_question_policy(
            scope="agent", agent_id=owner.binding.context.agent_id, enabled=enabled,
        )
        await provider.invoke(profile, request)
        assert (QUESTION_CONTRACT in manager.backends[-1].sys_prompt) is enabled

    async def unavailable():
        raise OSError("synthetic channel unavailable")

    provider.bind_question_availability_port(unavailable)
    await provider.invoke(profile, request)
    assert QUESTION_CONTRACT not in manager.backends[-1].sys_prompt
    assert "Question-card policy could not be read" in caplog.text


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
