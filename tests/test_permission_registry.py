"""Explicit managed registry uses the existing policy and a supervised port."""
import asyncio
import json

import pytest

from orchestrator.permissions.authority import PermissionAuthority
from orchestrator.permissions.models import ExecutionContext, normalize_scope
from orchestrator.permissions.registry import ManagedCatalogueRegistry, ManagedToolRegistry
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
    # Managed tool discovery awaits a qualified supervised execution channel.
    assert "tool_catalog" in legacy_names and "tool_catalog" not in managed_names
    assert managed_names == (legacy_names - {"tool_catalog"}) | {"request_access", "get_access_status", "release_access"}


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
async def test_managed_catalogue_shows_only_qualified_tool_channels(environment, tmp_path):
    authority, _, context, supervisor, base = environment
    registry = ManagedToolRegistry(
        allowed_tools=["file_write", "web_search", "hashi_scheduler_list", "desktop_screenshot"],
        access_root=tmp_path, workspace_dir=tmp_path, secrets={}, durable_wait=True,
        permission_authority=authority, permission_context=context, permission_supervisor=supervisor,
        requestable_scopes=base._permission_gate._requestable,
    )
    definitions = {item["function"]["name"] for item in registry.get_tool_definitions()}
    assert "file_write" in definitions
    assert not {"web_search", "hashi_scheduler_list", "desktop_screenshot"} & definitions
    assert registry.tool_availability("hashi_scheduler_list")["reason"] == "managed_channel_unqualified"
    result = await registry.execute("hashi_scheduler_list", {}, "scheduler-call")
    assert isinstance(result, ToolResult) and result.is_error
    assert result.details["reason"] == "managed_channel_unqualified"
    assert authority.list_requests(context) == ()


def test_pre_run_catalogue_does_not_advertise_unimplemented_managed_tools(tmp_path):
    registry = ManagedCatalogueRegistry(
        allowed_tools=["file_write", "web_search", "hashi_scheduler_list", "desktop_screenshot"],
        access_root=tmp_path, workspace_dir=tmp_path, secrets={},
    )
    definitions = {item["function"]["name"] for item in registry.get_tool_definitions()}
    assert "file_write" in definitions
    assert not {"web_search", "hashi_scheduler_list", "desktop_screenshot"} & definitions
    assert registry.tool_availability("hashi_scheduler_list")["reason"] == "managed_channel_unqualified"


def test_managed_catalogue_does_not_offer_questions_without_a_question_channel(environment, tmp_path):
    authority, _, context, supervisor, base = environment
    registry = ManagedToolRegistry(
        allowed_tools=["ask_user", "get_user_answer"], access_root=tmp_path, workspace_dir=tmp_path,
        secrets={}, durable_wait=True, permission_authority=authority, permission_context=context,
        permission_supervisor=supervisor, requestable_scopes=base._permission_gate._requestable,
    )
    definitions = {item["function"]["name"] for item in registry.get_tool_definitions()}
    assert not {"ask_user", "get_user_answer"} & definitions


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


@pytest.mark.parametrize("state,disposition", [
    ("unsupported", "unavailable"), ("denied", "denied"), ("withdrawn", "denied"),
])
@pytest.mark.asyncio
async def test_durable_control_failures_are_errors_in_registry_and_mcp(environment, tmp_path, monkeypatch, state, disposition):
    from types import SimpleNamespace
    from adapters.her_v2_provider import _EvidenceRecordingToolRegistry
    from orchestrator.her_v2.models import Effort, Stage, StageRequest, ToolReceiptStatus
    from orchestrator.permissions.execution_gate import GateResult
    from tools.gateway.mcp_stdio import ToolGateway

    _, _, _, _, registry = environment
    registry.permission_durable_wait = True
    calls = []

    async def gate(operation_id, *args, **kwargs):
        calls.append(operation_id)
        return GateResult(state, operation_id)

    monkeypatch.setattr(registry._permission_gate, "execute", gate)
    request = StageRequest(turn_id="turn", request_ref="request", stage=Stage.EXECUTION,
                           role="primary", attempt=1, goal="execute", classification=None,
                           effort=Effort.HIGH, allow_tools=True)
    wrapper = _EvidenceRecordingToolRegistry(registry, request)
    result = await wrapper.execute("file_write", {"path": "not-written", "content": "x"}, "call")
    assert isinstance(result, ToolResult) and result.is_error
    assert wrapper.receipts[-1].status is ToolReceiptStatus.FAILED
    assert not (tmp_path / "not-written").exists()
    assert result.details["control_disposition"] == disposition
    assert result.details["retryable"] is False
    assert len(calls) == 1
    projected = ToolGateway._result(SimpleNamespace(context=SimpleNamespace(vision_enabled=False)),
                                    result.output, result.is_error, details=result.details)
    assert projected["isError"] is True
    assert projected["structuredContent"]["hashi"]["details"]["control_disposition"] == disposition
    assert projected["structuredContent"]["hashi"]["details"]["retryable"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("durable", [False, True])
async def test_oversized_managed_arguments_explain_transport_limit_without_effect(
    environment, tmp_path, durable,
):
    """An oversized payload is a transport limit, not an unsupported policy."""
    authority, _, context, supervisor, registry = environment
    registry.permission_durable_wait = durable
    arguments = {"path": "large-result.txt", "content": "x" * (1024 * 1024) + "终"}
    result = await registry.execute("file_write", arguments, "large-arguments")

    assert isinstance(result, ToolResult) and result.is_error
    assert result.details["error_code"] == "managed_argument_transport_limit"
    assert result.details["limit_bytes"] == 1024 * 1024
    encoded = json.dumps(
        arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    assert result.details["actual_bytes"] == len(encoded) > result.details["limit_bytes"]
    assert result.details["effect_state"] == "not_started"
    assert result.details["tool_executed"] is False
    assert str(len(encoded)) in result.output
    assert str(1024 * 1024) in result.output
    assert "transport" in result.output.lower()
    assert "not started" in result.output.lower()
    assert authority.list_requests(context) == ()
    assert not supervisor.queued.is_set()
    assert not (tmp_path / "large-result.txt").exists()

    from types import SimpleNamespace
    from tools.gateway.mcp_stdio import ToolGateway

    gateway = ToolGateway(SimpleNamespace(
        audit={}, build_registry=lambda: registry,
        enforce_legacy_limits=False, vision_enabled=False,
    ))
    projected = await gateway.call("hashi_file_write", arguments, "large-through-mcp")
    assert projected["isError"] is True
    details = projected["structuredContent"]["hashi"]["details"]
    assert details["error_code"] == "managed_argument_transport_limit"
    assert details["actual_bytes"] == len(encoded)
    assert details["effect_state"] == "not_started"
    assert details["tool_executed"] is False
    assert authority.list_requests(context) == ()
    assert not supervisor.queued.is_set()
    assert not (tmp_path / "large-result.txt").exists()



@pytest.mark.asyncio
async def test_successful_managed_receipt_remains_success(environment, monkeypatch):
    from orchestrator.permissions.execution_gate import GateResult

    _, _, _, _, registry = environment
    registry.permission_durable_wait = True

    async def successful(operation_id, *args, **kwargs):
        return GateResult("succeeded", operation_id, payload="verified result")

    monkeypatch.setattr(registry._permission_gate, "execute", successful)
    result = await registry.execute("file_write", {"path": "file", "content": "x"}, "call")
    assert not result.is_error and result.output == "verified result"
    assert result.details["control_disposition"] == "allowed"


@pytest.mark.asyncio
async def test_unknown_durable_effect_stays_parked_without_a_retry_claim(environment, monkeypatch):
    from orchestrator.permissions.continuation import PermissionPark
    from orchestrator.permissions.execution_gate import GateResult

    _, _, _, _, registry = environment
    registry.permission_durable_wait = True

    async def unknown(operation_id, *args, **kwargs):
        return GateResult("unknown", operation_id)

    monkeypatch.setattr(registry._permission_gate, "execute", unknown)
    with pytest.raises(PermissionPark) as caught:
        await registry.execute("file_write", {"path": "possibly-written", "content": "x"}, "unknown-call")
    assert caught.value.result.state == "unknown"


@pytest.mark.asyncio
async def test_disabled_search_and_managed_office_return_typed_unavailability(environment, tmp_path, monkeypatch):
    from tools.gateway.mcp_stdio import ToolGateway
    from types import SimpleNamespace

    authority, _, context, supervisor, base = environment
    registry = ManagedToolRegistry(
        allowed_tools=["file_search", "office_edit"], access_root=tmp_path, workspace_dir=tmp_path,
        secrets={}, tool_options={"file_search": {"enabled": False}},
        permission_authority=authority, permission_context=context, permission_supervisor=supervisor,
        requestable_scopes=base._permission_gate._requestable, durable_wait=True,
    )

    async def must_not_dispatch(*args, **kwargs):
        pytest.fail("unavailable capabilities must not dispatch")

    monkeypatch.setattr(registry._permission_gate, "execute", must_not_dispatch)
    for name, expected_reason in [("file_search", "scoped_search_disabled"),
                                  ("office_edit", "office_managed_channel_unqualified")]:
        result = await registry.execute(name, {}, name + "-call")
        assert isinstance(result, ToolResult) and result.is_error
        assert result.details["control_disposition"] == "unavailable"
        assert result.details["reason"] == expected_reason
        projected = ToolGateway._result(SimpleNamespace(context=SimpleNamespace(vision_enabled=False)),
                                        result.output, result.is_error, details=result.details)
        assert projected["isError"] is True
        assert projected["structuredContent"]["hashi"]["details"]["reason"] == expected_reason

    note = tmp_path / "ordinary.txt"
    note.write_text("readable", encoding="utf-8")
    ordinary = ToolRegistry(allowed_tools=["file_read"], access_root=tmp_path, workspace_dir=tmp_path, secrets={})
    success = await ordinary.execute("file_read", {"path": str(note)}, "read-call")
    assert not success.is_error and "readable" in success.output

    monkeypatch.setattr(ordinary, "tool_availability", lambda _: {"available": False, "reason": "fixture_unavailable"})
    future = ordinary._capability_unavailable_result("file_read", tool_call_id="future")
    assert future.is_error and future.details["reason"] == "fixture_unavailable"
    assert "Next step:" in future.output


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
    assert same.state == "result_unavailable"
    assert changed.state == "conflict"
    assert (tmp_path / "once.txt").read_text(encoding="utf-8") == "original"
    assert len(authority.list_requests(context)) == 1


@pytest.mark.parametrize("control_state", ["unavailable", "unknown", "result_unavailable", "conflict"])
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
    # The authority cannot exclude a prior side effect merely from an incomplete
    # control state for a previously assigned operation ID.
    assert caught.value.side_effects_possible is True
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


@pytest.mark.asyncio
async def test_herv3_receipt_marks_recovered_output_without_repeating_file_effect(environment, tmp_path):
    from adapters.her_v2_provider import _EvidenceRecordingToolRegistry
    from orchestrator.her_v2.models import Effort, Stage, StageRequest
    from tests.test_permission_execution_gate import RecoverableFileSupervisor

    authority, issuer, context, _, registry = environment
    supervisor = RecoverableFileSupervisor(authority, tmp_path)
    registry._permission_gate._supervisor = supervisor
    invocation = StageRequest(
        turn_id="turn", request_ref="request", stage=Stage.EXECUTION, role="primary",
        attempt=1, goal="execute", classification=None, effort=Effort.HIGH, allow_tools=True,
    )
    wrapper = _EvidenceRecordingToolRegistry(registry, invocation)
    arguments = {"path": "once.txt", "content": "created"}
    task = asyncio.create_task(wrapper.execute("file_write", arguments, "provider-call"))
    request = await find_pending(authority, context)
    operation = authority.operation_status_for_task(context, request.operation_id)
    issuer.approve(authority, context, request, operation.arguments_digest)
    registry._permission_waiter.notify()
    first = await asyncio.wait_for(task, 1)
    assert first.output.startswith("written")
    assert wrapper.receipts[-1].details["permission_replayed"] is False

    (tmp_path / "once.txt").write_text("later change", encoding="utf-8")
    second = await wrapper.execute("file_write", arguments, "provider-call")
    assert second.output.startswith("written")
    assert wrapper.receipts[-1].details["permission_replayed"] is True
    assert supervisor.execute_calls == 1 and supervisor.recover_calls == 1
    assert (tmp_path / "once.txt").read_text(encoding="utf-8") == "later change"
    assert authority.decision_count(request.request_id) == 1

def test_managed_http_catalogue_exposes_only_supported_redirect_options(environment, tmp_path):
    _, _, _, supervisor, managed = environment
    managed._allowed.add("http_request")
    supervisor.supports = lambda tool: tool in {"file_write", "http_request"}
    definitions = {item["function"]["name"]: item["function"]
                   for item in managed.get_tool_definitions()}
    properties = definitions["http_request"]["parameters"]["properties"]
    assert properties["follow_redirects"]["type"] == "boolean"
    assert properties["max_redirects"]["minimum"] == 1
    assert properties["session_cookies"]["type"] == "boolean"
    assert properties["response_encoding"]["enum"] == ["text", "base64"]
    assert properties["response_file"]["type"] == "string"
    catalogue = ManagedCatalogueRegistry(allowed_tools=["http_request"], access_root=tmp_path,
                                        workspace_dir=tmp_path, secrets={})
    pre_run = {item["function"]["name"]: item["function"] for item in catalogue.get_tool_definitions()}
    assert pre_run["http_request"]["parameters"]["properties"]["session_cookies"]["type"] == "boolean"
    assert pre_run["http_request"]["parameters"]["properties"]["response_encoding"]["enum"] == ["text", "base64"]
    assert pre_run["http_request"]["parameters"]["properties"]["response_file"]["type"] == "string"
    legacy = ToolRegistry(allowed_tools=["http_request"], access_root=tmp_path,
                          workspace_dir=tmp_path, secrets={})
    old = {item["function"]["name"]: item["function"] for item in legacy.get_tool_definitions()}
    assert "follow_redirects" not in old["http_request"]["parameters"]["properties"]
    assert "response_encoding" not in old["http_request"]["parameters"]["properties"]
    assert "response_file" not in old["http_request"]["parameters"]["properties"]

# HASHI-Fingerprint: hfp_b482e1c53e084b3aba83876c071f6806
