"""Agent-facing permission tools cannot approve or select another identity."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest

from orchestrator.permissions.authority import DecisionReceipt, PermissionAuthority, PermissionDenied
from orchestrator.permissions.models import ExecutionContext, normalize_scope, scope_digest
from tools.request_access import PermissionAgentTools, PermissionToolError, permission_tool_definitions


class Issuer:
    def __init__(self):
        self.receipts = {}

    def verify(self, reference):
        return self.receipts.get(reference)


@pytest.fixture
def environment(tmp_path):
    context = ExecutionContext(
        instance_id="i", owner_id="o", agent_id="a", task_id="task-a", run_id="run-a",
        session_id="s", turn_id="t", worker_generation="w", context_generation="g",
        execution_epoch="epoch-a", backend_id="herv3", executor_id="registry", policy_revision=1,
    )
    issuer = Issuer()
    authority = PermissionAuthority(tmp_path / "ledger.sqlite", verifier=issuer,
                                    clock=lambda: 1_000.0, monotonic=lambda: 1_000.0)
    authority.open_task(context)
    base = tmp_path / "project"
    base.mkdir()
    maximum = normalize_scope("file", str(base), ["read", "list", "write"], cwd=base)
    tools = PermissionAgentTools(authority, context, cwd=base, requestable_scopes=(maximum,))
    return authority, issuer, context, base, tools


def call(tools, name, arguments):
    return asyncio.run(tools.execute(name, arguments))


def test_permission_schemas_are_opt_in_and_never_offer_approval():
    from jsonschema import Draft202012Validator
    from tools.schemas import ALL_TOOL_NAMES

    definitions = permission_tool_definitions()
    names = [entry["function"]["name"] for entry in definitions]
    assert names == ["request_access", "get_access_status", "release_access"]
    assert not set(names).intersection(ALL_TOOL_NAMES)
    for definition in definitions:
        Draft202012Validator.check_schema(definition["function"]["parameters"])
    request = definitions[0]["function"]["parameters"]
    sample = {
        "reason": "Read",
        "resources": [{"kind": "filesystem", "path": "reports", "actions": ["read"]}],
        "duration": {"mode": "task", "max_seconds": 300},
        "request_key": "report-read",
    }
    assert not list(Draft202012Validator(request).iter_errors(sample))
    assert list(Draft202012Validator(request).iter_errors({**sample, "approved": True}))


def test_proactive_request_is_persisted_and_key_replay_does_not_expand_scope(environment):
    authority, _, context, base, tools = environment
    args = {
        "reason": "Read project material",
        "resources": [{"kind": "filesystem", "path": str(base), "actions": ["read"]}],
        "duration": {"mode": "task", "max_seconds": 300},
        "request_key": "project-read",
    }
    first = call(tools, "request_access", args)
    assert first["status"] == "pending"
    assert "approved" not in first
    persisted = authority.get_request(context, first["request_id"])
    assert persisted.purpose == args["reason"]
    assert persisted.suggested_lifetime == "task"
    assert persisted.suggested_seconds == 300
    assert call(tools, "request_access", args)["request_id"] == first["request_id"]
    changed = {**args, "resources": [{"kind": "filesystem", "path": str(base), "actions": ["write"]}]}
    with pytest.raises(PermissionToolError):
        call(tools, "request_access", changed)
    assert not authority.evaluate(context, [normalize_scope("file", str(base), ["read"], cwd=base)])


def test_equivalent_pending_requests_keep_both_idempotency_keys(environment):
    authority, _, context, base, tools = environment
    first_args = {
        "reason": "Read",
        "resources": [{"kind": "filesystem", "path": str(base), "actions": ["read"]}],
        "duration": {"mode": "task", "max_seconds": 300},
        "request_key": "key-one",
    }
    first = call(tools, "request_access", first_args)
    second_args = {**first_args, "request_key": "key-two"}
    second = call(tools, "request_access", second_args)
    assert first["request_id"] == second["request_id"]
    assert len(authority.list_requests(context)) == 1
    assert call(tools, "request_access", first_args)["request_id"] == first["request_id"]
    assert call(tools, "request_access", second_args)["request_id"] == first["request_id"]
    changed = {**second_args, "reason": "Different purpose"}
    with pytest.raises(PermissionToolError):
        call(tools, "request_access", changed)


def test_concurrent_replays_create_one_request(environment):
    authority, _, context, base, tools = environment
    args = {
        "reason": "Read",
        "resources": [{"kind": "filesystem", "path": str(base), "actions": ["read"]}],
        "duration": {"mode": "task", "max_seconds": 300},
        "request_key": "concurrent",
    }
    barrier = Barrier(2)

    def submit():
        barrier.wait()
        return call(tools, "request_access", args)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(submit) for _ in range(2)]
        requests = [future.result() for future in futures]
    assert requests[0]["request_id"] == requests[1]["request_id"]
    assert len(authority.list_requests(context)) == 1


def test_model_cannot_supply_approval_identity_or_unknown_tool(environment):
    authority, _, context, base, tools = environment
    base_args = {
        "reason": "Read",
        "resources": [{"kind": "filesystem", "path": str(base), "actions": ["read"]}],
        "duration": {"mode": "task", "max_seconds": 300},
        "request_key": "request",
    }
    for field in ("approved", "owner_id", "agent_id", "task_id", "actor_id", "policy_revision"):
        with pytest.raises(PermissionToolError):
            call(tools, "request_access", {**base_args, field: True})
    for forbidden in ("approve_access", "extend_expiry", "change_safety_level"):
        with pytest.raises(PermissionToolError):
            call(tools, forbidden, {})
    assert authority.list_requests(context) == ()


def test_request_ceiling_is_checked_before_recording(environment, tmp_path):
    authority, _, context, _, tools = environment
    outside = tmp_path / "outside"
    outside.mkdir()
    args = {
        "reason": "outside",
        "resources": [{"kind": "filesystem", "path": str(outside), "actions": ["read"]}],
        "duration": {"mode": "task", "max_seconds": 300},
        "request_key": "outside",
    }
    result = call(tools, "request_access", args)
    assert result["status"] == "unavailable"
    assert authority.list_requests(context) == ()


def test_status_and_release_are_task_scoped(environment, monkeypatch):
    authority, issuer, context, base, tools = environment
    scope = normalize_scope("file", str(base), ["read"], cwd=base)
    request = authority.request_access(context, [scope], purpose="work")
    receipt = DecisionReceipt(
        nonce="nonce", request_id=request.request_id, request_revision=request.revision,
        requested_digest=request.scope_digest, approved_digest=scope_digest([scope]),
        instance_id=context.instance_id, task_id=context.task_id, run_id=context.run_id,
        execution_epoch=context.execution_epoch, operation_id=None, arguments_digest=None,
        policy_revision=context.policy_revision, owner_id=context.owner_id,
        agent_id=context.agent_id, session_id=context.session_id,
        context_generation=context.context_generation, principal_id=context.owner_id,
        choice="approve", lifetime="task", duration_seconds=60,
        idempotency_key="approved", expires_at=1_030.0,
    )
    issuer.receipts["proof"] = receipt
    lease = authority.decide("proof", approved_scopes=[scope])
    assert call(tools, "get_access_status", {"request_id": request.request_id})["status"] == "decided"
    other = replace(context, task_id="task-b", run_id="run-b", execution_epoch="epoch-b")
    authority.open_task(other)
    other_tools = PermissionAgentTools(authority, other, cwd=base, requestable_scopes=(scope,))
    with pytest.raises(PermissionDenied):
        call(other_tools, "get_access_status", {"request_id": request.request_id})
    with pytest.raises(PermissionDenied):
        call(other_tools, "release_access", {"lease_id": lease.lease_id})
    assert authority.evaluate(context, [scope])
    already_granted = {
        "reason": "existing grant",
        "resources": [{"kind": "filesystem", "path": str(base), "actions": ["read"]}],
        "duration": {"mode": "task", "max_seconds": 60},
        "request_key": "existing-grant",
    }
    assert call(tools, "request_access", already_granted)["status"] == "granted_existing"
    assert call(tools, "release_access", {"lease_id": lease.lease_id})["status"] == "revoked"
    assert not authority.evaluate(context, [scope])
    with pytest.raises(PermissionToolError):
        call(tools, "request_access", already_granted)

    again = authority.request_access(context, [scope], purpose="next work")
    issuer.receipts["next-proof"] = replace(
        receipt, nonce="next", request_id=again.request_id,
        requested_digest=again.scope_digest, idempotency_key="next-approved",
    )
    next_lease = authority.decide("next-proof", approved_scopes=[scope])
    monkeypatch.setattr(authority, "_clock", lambda: 1_061.0)
    monkeypatch.setattr(authority, "_monotonic", lambda: 1_061.0)
    assert call(tools, "get_access_status", {"lease_id": next_lease.lease_id})["status"] == "expired"
    assert not authority.evaluate(context, [scope])
