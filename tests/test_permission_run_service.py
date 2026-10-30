"""The PAO owner preserves Run/task identity and fences exact executor epochs."""
from datetime import datetime, timezone

import pytest

from orchestrator.permissions.authority import PermissionAuthority, PermissionDenied
from orchestrator.permissions.models import PermissionLevel, PermissionSettings
from orchestrator.permissions.run_service import ManagedRunPolicy, PermissionRunService
from orchestrator.runtime_common import QueuedRequest
from tests.test_permission_pao_fence import setup_run
from tests.test_permission_runtime_context import NoApprovals


class ExecutorFactory:
    def __init__(self, authority):
        self.authority, self.created, self.retired = authority, [], []
        self.allow_retire = True

    def executor_id(self, policy):
        return "test-qualified-profile"

    def create(self, binding, policy):
        self.created.append(binding)
        return object()

    async def retire(self, binding):
        assert self.authority.execution_status(binding.context).binding_state == "fenced"
        self.retired.append(binding)
        return self.allow_retire


def setup(tmp_path):
    store, _, accepted, fence, old_context = setup_run(tmp_path)
    authority = PermissionAuthority(tmp_path / "permissions.db", verifier=NoApprovals())
    factory = ExecutorFactory(authority)
    policy = ManagedRunPolicy(PermissionSettings(PermissionLevel.L1), 1, tmp_path, (), (), {})
    service = PermissionRunService(authority, store, policy_resolver=lambda *args: policy,
                                    executor_factory=factory)
    arguments = dict(request_id=accepted.request_id, run_id=accepted.run_id, owner_id=old_context.owner_id,
                     agent_id=old_context.agent_id, session_id=old_context.session_id,
                     context_generation=1, fencing_token=fence, worker_id="INSTANCE:agent",
                     worker_generation="worker", turn_id="request", backend_id="her-v2")
    return store, authority, factory, service, arguments, accepted


@pytest.mark.asyncio
async def test_pao_stop_settles_task_and_does_not_reopen_a_run(tmp_path):
    store, authority, factory, service, arguments, accepted = setup(tmp_path)
    binding = await service.open_execution(**arguments)
    assert await service.open_execution(**arguments) == binding
    assert len(factory.created) == 1
    store.cancel_run(accepted.run_id, owner_id=binding.context.owner_id, reason="user stop")
    assert await service.settle_run(binding=binding) == "settled"
    status = authority.execution_status(binding.context)
    assert status.task_state == "cancelled" and status.binding_state == "fenced"
    with pytest.raises(PermissionDenied):
        await service.open_execution(**arguments)
    assert len(factory.created) == 1


@pytest.mark.parametrize("retire_ok", [True, False])
@pytest.mark.asyncio
async def test_new_worker_reuses_task_only_after_exact_old_executor_cleanup(tmp_path, retire_ok):
    store, authority, factory, service, arguments, accepted = setup(tmp_path)
    first = await service.open_execution(**arguments)
    from orchestrator.permissions.models import normalize_scope
    scope = normalize_scope("file", str(tmp_path / "report"), ("read",), cwd=tmp_path)
    request = authority.request_access(first.context, [scope], purpose="Read report")
    item = QueuedRequest(accepted.request_id, 1, "Read report", "test", "Report",
                         datetime.now(timezone.utc).isoformat(), session_id=first.context.session_id,
                         run_id=first.context.run_id, owner_id=first.context.owner_id, message_id=accepted.message_id)
    service.parking.park(first, item, checkpoint_reference={"digest": "a" * 64}, request_id=request.request_id,
                         operation_id="", state="waiting")
    restored, _ = service.parking.claim_resume(first)
    new_fence = store.mark_request_running(restored.request_id, worker_id="INSTANCE:agent")
    factory.allow_retire = retire_ok
    next_arguments = {**arguments, "fencing_token": new_fence, "worker_generation": "new-worker"}
    if not retire_ok:
        with pytest.raises(PermissionDenied):
            await service.open_execution(**next_arguments)
        assert len(factory.created) == 1
        return
    second = await service.open_execution(**next_arguments)
    assert second.context.task_id == first.context.task_id
    assert second.context.run_id == first.context.run_id
    assert second.context.execution_epoch != first.context.execution_epoch
    rebound = authority.get_request(second.context, request.request_id)
    assert rebound.expires_at == request.expires_at and rebound.revision == request.revision + 1
    await service.close_execution(binding=first)
    assert authority.execution_status(second.context).binding_state == "active"
    assert service.execution_registry(second) is not None
    assert factory.retired[-1].context.execution_epoch == first.context.execution_epoch



@pytest.mark.asyncio
async def test_terminal_settlement_after_service_restart_uses_canonical_pao_evidence(tmp_path):
    store, authority, factory, service, arguments, accepted = setup(tmp_path)
    binding = await service.open_execution(**arguments)
    # Real authority restart fences the old epoch. Cancellation independently
    # advances the PAO fence. Neither fact should strand the task's final state.
    restarted = PermissionAuthority(tmp_path / "permissions.db", verifier=NoApprovals())
    assert restarted.execution_status(binding.context).binding_state == "fenced"
    store.cancel_run(accepted.run_id, owner_id=binding.context.owner_id, reason="user stop")
    replacement = PermissionRunService(
        restarted, store, policy_resolver=service._policy, executor_factory=ExecutorFactory(restarted),
    )
    assert await replacement.settle_run(binding=binding) == "settled"
    assert restarted.execution_status(binding.context).task_state == "cancelled"
