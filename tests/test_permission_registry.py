"""Explicit managed registry uses the existing policy and a supervised port."""
import asyncio

import pytest

from orchestrator.permissions.authority import PermissionAuthority
from orchestrator.permissions.models import ExecutionContext, normalize_scope
from orchestrator.permissions.registry import ManagedToolRegistry
from tests.test_permission_execution_gate import Issuer, TestFileSupervisor, find_pending
from tools.registry import ToolRegistry, ToolResult


@pytest.fixture
def environment(tmp_path):
    context = ExecutionContext(
        instance_id="i", owner_id="owner", agent_id="agent", task_id="task",
        run_id="run", session_id="session", turn_id="turn", worker_generation="worker",
        context_generation="generation", execution_epoch="epoch",
        backend_id="herv3", executor_id="registry", policy_revision=1,
    )
    issuer = Issuer()
    authority = PermissionAuthority(tmp_path / "ledger.sqlite", verifier=issuer,
                                    clock=lambda: 1_000.0, monotonic=lambda: 1_000.0)
    authority.open_task(context)
    ceiling = normalize_scope("file", str(tmp_path), ["create", "write"], cwd=tmp_path)
    supervisor = TestFileSupervisor(authority, tmp_path)
    registry = ManagedToolRegistry(
        allowed_tools=["file_write"], access_root=tmp_path, workspace_dir=tmp_path,
        secrets={}, audit_context={"agent_name": "agent", "workspace_dir": str(tmp_path),
                                  "safety_mode": "read_write"},
        permission_authority=authority, permission_context=context,
        permission_supervisor=supervisor, requestable_scopes=(ceiling,),
    )
    return authority, issuer, context, supervisor, registry


def test_l0_catalogue_is_unchanged_and_managed_mode_adds_only_control_tools(environment, tmp_path):
    _, _, _, _, managed = environment
    legacy = ToolRegistry(
        allowed_tools=["file_write"], access_root=tmp_path, workspace_dir=tmp_path, secrets={},
    )
    legacy_names = {item["function"]["name"] for item in legacy.get_tool_definitions()}
    managed_names = {item["function"]["name"] for item in managed.get_tool_definitions()}
    assert not {"request_access", "get_access_status", "release_access"} & legacy_names
    assert managed_names == legacy_names | {"request_access", "get_access_status", "release_access"}


def test_managed_registry_rejects_shared_credentials(environment, tmp_path):
    authority, _, context, supervisor, _ = environment
    ceiling = normalize_scope("file", str(tmp_path), ["create", "write"], cwd=tmp_path)
    with pytest.raises(ValueError):
        ManagedToolRegistry(
            allowed_tools=["file_write"], access_root=tmp_path, workspace_dir=tmp_path,
            secrets={"shared_secret": "not-for-managed-runner"},
            permission_authority=authority, permission_context=context,
            permission_supervisor=supervisor, requestable_scopes=(ceiling,),
        )


@pytest.mark.asyncio
async def test_real_registry_runs_only_after_approval_with_same_operation(environment, tmp_path):
    authority, issuer, context, _, registry = environment
    args = {"path": "result.txt", "content": "original"}
    task = asyncio.create_task(registry.execute("file_write", args, "provider-call-1"))
    request = await find_pending(authority, context)
    assert not (tmp_path / "result.txt").exists()
    operation = authority.operation_status_for_task(context, request.operation_id)
    issuer.approve(authority, context, request, operation.arguments_digest)
    result = await asyncio.wait_for(task, 2)
    assert isinstance(result, ToolResult)
    assert not result.is_error
    assert (tmp_path / "result.txt").read_text(encoding="utf-8") == "original"
    assert authority.operation_state(request.operation_id) == "succeeded"


@pytest.mark.asyncio
async def test_legacy_admission_and_stable_identity_cannot_be_bypassed(environment, tmp_path):
    authority, _, context, _, registry = environment
    denied = await registry.execute("file_read", {"path": "secret.txt"}, "read-call")
    assert isinstance(denied, ToolResult)
    assert denied.is_error
    missing_identity = await registry.execute("file_write", {"path": "out", "content": "x"})
    assert missing_identity.state == "unavailable"
    assert authority.list_requests(context) == ()
    assert not (tmp_path / "out").exists()


@pytest.mark.asyncio
async def test_request_allowlist_also_applies_to_permission_tools(environment):
    authority, _, context, _, registry = environment
    args = {
        "reason": "work", "resources": [{"kind": "capability", "name": "x", "actions": ["use"]}],
        "duration": {"mode": "task", "max_seconds": 60}, "request_key": "request",
    }
    result = await registry.execute_with_audit_context(
        "request_access", args, "ask-call", audit_context={"request_tool_allowlist": ["file_write"]},
    )
    assert isinstance(result, ToolResult)
    assert result.is_error
    assert authority.list_requests(context) == ()


@pytest.mark.asyncio
async def test_revoke_while_actual_registry_is_queued_blocks_the_file(environment, tmp_path):
    authority, issuer, context, supervisor, registry = environment
    await supervisor.resource.acquire()
    task = asyncio.create_task(registry.execute(
        "file_write", {"path": "queued.txt", "content": "private"}, "queued-call",
    ))
    try:
        request = await find_pending(authority, context)
        operation = authority.operation_status_for_task(context, request.operation_id)
        lease = issuer.approve(authority, context, request, operation.arguments_digest)
        registry._permission_waiter.notify()
        await asyncio.wait_for(supervisor.queued.wait(), 1)
        authority.revoke(context, lease.lease_id)
    finally:
        supervisor.resource.release()
    result = await asyncio.wait_for(task, 1)
    assert result.state == "permission_changed"
    assert not (tmp_path / "queued.txt").exists()


@pytest.mark.asyncio
async def test_same_provider_call_cannot_repeat_or_replace_completed_write(environment, tmp_path):
    authority, issuer, context, _, registry = environment
    arguments = {"path": "once.txt", "content": "original"}
    task = asyncio.create_task(registry.execute("file_write", arguments, "same-call"))
    request = await find_pending(authority, context)
    operation = authority.operation_status_for_task(context, request.operation_id)
    issuer.approve(authority, context, request, operation.arguments_digest)
    registry._permission_waiter.notify()
    result = await asyncio.wait_for(task, 1)
    assert isinstance(result, ToolResult)
    assert not result.is_error

    same = await registry.execute("file_write", arguments, "same-call")
    changed = await registry.execute(
        "file_write", {"path": "once.txt", "content": "replacement"}, "same-call",
    )
    assert same.state == "conflict"
    assert changed.state == "conflict"
    assert (tmp_path / "once.txt").read_text(encoding="utf-8") == "original"
    assert len(authority.list_requests(context)) == 1


@pytest.mark.parametrize("control_state", ["unavailable", "unknown"])
@pytest.mark.asyncio
async def test_evidence_wrapper_never_records_incomplete_permission_control_as_success(
    environment, monkeypatch, control_state,
):
    from adapters.her_v2_provider import _EvidenceRecordingToolRegistry
    from orchestrator.her_v2.interfaces import StageInvocationError
    from orchestrator.her_v2.models import Effort, Stage, StageRequest, ToolReceiptStatus
    from orchestrator.permissions.execution_gate import GateResult

    _, _, _, _, registry = environment

    async def incomplete(*args, **kwargs):
        return GateResult(control_state, "original-operation")

    monkeypatch.setattr(registry._permission_gate, "execute", incomplete)
    request = StageRequest(
        turn_id="turn", request_ref="request", stage=Stage.EXECUTION, role="primary",
        attempt=1, goal="execute", classification=None, effort=Effort.HIGH, allow_tools=True,
    )
    wrapper = _EvidenceRecordingToolRegistry(registry, request)
    with pytest.raises(StageInvocationError) as caught:
        await wrapper.execute("file_write", {"path": "file", "content": "data"}, "call")
    assert caught.value.retryable is False
    assert caught.value.code == "PERMISSION_CONTROL_INCOMPLETE"
    assert caught.value.side_effects_possible is (control_state == "unknown")
    assert wrapper.receipts[-1].status is ToolReceiptStatus.FAILED
    assert wrapper.receipts[-1].completed is False


@pytest.mark.asyncio
async def test_herv3_direct_turn_waits_for_approved_tool_and_finishes_once(environment, tmp_path):
    from adapters.her_v2_provider import _EvidenceRecordingToolRegistry
    from orchestrator.her_v2.audit import DurableAuditLog
    from orchestrator.her_v2.config import HERv2Config
    from orchestrator.her_v2.interfaces import RecordingDelivery
    from orchestrator.her_v2.ledger import LedgerStore
    from orchestrator.her_v2.models import StageResponse, ToolReceiptStatus
    from orchestrator.her_v2.runtime import HERv2Runtime

    authority, issuer, context, _, registry = environment

    class Provider:
        def __init__(self):
            self.calls = 0
            self.receipts = ()

        def tool_catalogue(self, *, allow_side_effects, delegated_tools=None):
            return ()

        async def invoke(self, profile, request):
            self.calls += 1
            wrapped = _EvidenceRecordingToolRegistry(registry, request)
            output = await wrapped.execute(
                "file_write", {"path": "her-result.txt", "content": "hello"}, "her-call",
            )
            assert not output.is_error
            self.receipts = wrapped.receipts
            return StageResponse(
                data={"message": "Report saved."}, provider=profile.engine, model=profile.model,
                tool_receipts=self.receipts,
            )

    provider = Provider()
    store = tmp_path / "her"
    store.mkdir()
    runtime = HERv2Runtime(
        config=HERv2Config.from_mapping({"main": {"provider": "fake-api", "model": "fake"}}),
        provider=provider, ledger_store=LedgerStore(store / "ledgers"),
        audit_log=DurableAuditLog(store / "audit.jsonl", store / "fallback.jsonl"),
        delivery=RecordingDelivery(),
    )
    turn = asyncio.create_task(runtime.run_turn("Create the report", "request"))
    request = await find_pending(authority, context)
    assert provider.calls == 1
    assert not turn.done()
    assert not (tmp_path / "her-result.txt").exists()
    operation = authority.operation_status_for_task(context, request.operation_id)
    issuer.approve(authority, context, request, operation.arguments_digest)
    registry._permission_waiter.notify()
    result = await asyncio.wait_for(turn, 2)
    assert result.terminal_state.value == "COMPLETED"
    assert provider.calls == 1
    assert (tmp_path / "her-result.txt").read_text(encoding="utf-8") == "hello"
    assert len(provider.receipts) == 1
    assert provider.receipts[0].status is ToolReceiptStatus.SUCCESS
