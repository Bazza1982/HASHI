from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from orchestrator.enterprise import (
    EnterpriseAuditLedger,
    IdentityService,
    PolicyDecision,
    PolicyEvaluator,
    evaluate_governance_policy,
    install_default_connector_policy,
)
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime


def _init_org(tmp_path, org_id: str = "ORG-001") -> None:
    identity = IdentityService.from_path(tmp_path / "state" / "enterprise.sqlite")
    identity.create_organization(org_id=org_id, name="Acme")


def _global_config(tmp_path, *, profile: str = "enterprise", org_id: str | None = "ORG-001"):
    return SimpleNamespace(
        deployment_profile=profile,
        organization_id=org_id,
        bridge_home=tmp_path,
    )


def _audit_events(tmp_path) -> list[dict]:
    path = tmp_path / "state" / "enterprise_audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_policy_evaluator_defaults_to_allow_without_rules(tmp_path):
    _init_org(tmp_path)
    evaluator = PolicyEvaluator.from_path(tmp_path / "state" / "enterprise.sqlite", org_id="ORG-001")

    result = evaluator.evaluate(
        "command.execute",
        resource="command:status",
        context={"agent_id": "zelda", "command_name": "status"},
    )

    assert result.allowed is True
    assert result.decision == PolicyDecision.ALLOW
    assert result.rule_id is None


def test_default_connector_policy_allows_github_reads_and_requires_write_or_egress_approval(tmp_path):
    _init_org(tmp_path)
    evaluator = PolicyEvaluator.from_path(tmp_path / "state" / "enterprise.sqlite", org_id="ORG-001")

    rules = install_default_connector_policy(evaluator)

    assert len(rules) == 9
    read = evaluator.evaluate("connector.execute", resource="connector:github:repo.read")
    issue = evaluator.evaluate("connector.execute", resource="connector:github:issue.create")
    merge = evaluator.evaluate("connector.execute", resource="connector:github:pr.merge")
    slack_send = evaluator.evaluate("connector.execute", resource="connector:slack:message.send")
    google_chat_send = evaluator.evaluate("connector.execute", resource="connector:google_chat:message.send")
    teams_send = evaluator.evaluate("connector.execute", resource="connector:teams:message.send")
    feishu_send = evaluator.evaluate("connector.execute", resource="connector:feishu:message.send")
    assert read.allowed is True
    assert read.rule_id == "tpl-connector-github-repo-read-allow"
    assert issue.allowed is False
    assert issue.decision == PolicyDecision.APPROVAL_REQUIRED
    assert issue.rule_id == "tpl-connector-github-issue-create-approval"
    assert merge.allowed is False
    assert merge.decision == PolicyDecision.APPROVAL_REQUIRED
    assert slack_send.allowed is False
    assert slack_send.decision == PolicyDecision.APPROVAL_REQUIRED
    assert slack_send.rule_id == "tpl-connector-slack-message-send-approval"
    assert google_chat_send.allowed is False
    assert google_chat_send.decision == PolicyDecision.APPROVAL_REQUIRED
    assert google_chat_send.rule_id == "tpl-connector-google-chat-message-send-approval"
    assert teams_send.allowed is False
    assert teams_send.decision == PolicyDecision.APPROVAL_REQUIRED
    assert teams_send.rule_id == "tpl-connector-teams-message-send-approval"
    assert feishu_send.allowed is False
    assert feishu_send.decision == PolicyDecision.APPROVAL_REQUIRED
    assert feishu_send.rule_id == "tpl-connector-feishu-message-send-approval"


def test_default_connector_policy_install_is_idempotent(tmp_path):
    _init_org(tmp_path)
    evaluator = PolicyEvaluator.from_path(tmp_path / "state" / "enterprise.sqlite", org_id="ORG-001")

    install_default_connector_policy(evaluator)
    install_default_connector_policy(evaluator)

    assert len(evaluator.list_rules()) == 9


def test_policy_evaluator_denies_matching_command_rule(tmp_path):
    _init_org(tmp_path)
    evaluator = PolicyEvaluator.from_path(tmp_path / "state" / "enterprise.sqlite", org_id="ORG-001")
    rule = evaluator.add_rule(
        action="command.execute",
        resource="command:backend",
        effect="deny",
        conditions={"command_name": "backend"},
    )

    result = evaluator.evaluate(
        "command.execute",
        resource="command:backend",
        context={"command_name": "backend"},
    )

    assert result.allowed is False
    assert result.decision == PolicyDecision.DENY
    assert result.rule_id == rule.id


def test_policy_evaluator_can_require_approval_for_project_scope(tmp_path):
    _init_org(tmp_path)
    evaluator = PolicyEvaluator.from_path(tmp_path / "state" / "enterprise.sqlite", org_id="ORG-001")
    evaluator.add_rule(
        action="file.write",
        resource="*",
        effect="approval_required",
        scope_type="project",
        scope_id="prj-finance",
    )

    result = evaluator.evaluate(
        "file.write",
        resource="workspace:reports",
        context={"project_id": "prj-finance"},
    )

    assert result.allowed is False
    assert result.decision == PolicyDecision.APPROVAL_REQUIRED


def test_evaluate_governance_policy_uses_enterprise_db(tmp_path):
    _init_org(tmp_path)
    evaluator = PolicyEvaluator.from_path(tmp_path / "state" / "enterprise.sqlite", org_id="ORG-001")
    evaluator.add_rule(action="command.execute", resource="command:sys", effect="deny")

    result = evaluate_governance_policy(
        "command.execute",
        {
            "global_config": _global_config(tmp_path),
            "resource": "command:sys",
            "command_name": "sys",
        },
    )

    assert result.allowed is False
    assert result.decision == PolicyDecision.DENY
    event = _audit_events(tmp_path)[-1]
    assert event["event_type"] == "policy"
    assert event["action"] == "command.execute"
    assert event["status"] == "denied"
    assert event["context"]["resource"] == "command:sys"
    assert event["context"]["decision"] == "deny"
    ledger = EnterpriseAuditLedger.from_path(tmp_path / "state" / "enterprise.sqlite", org_id="ORG-001")
    ledger_events = ledger.query(event_type="policy")
    assert len(ledger_events) == 1
    assert ledger_events[0].action == "command.execute"
    assert ledger_events[0].status == "denied"
    assert ledger_events[0].context["resource"] == "command:sys"


def test_evaluate_governance_policy_creates_approval_request(tmp_path):
    _init_org(tmp_path)
    evaluator = PolicyEvaluator.from_path(tmp_path / "state" / "enterprise.sqlite", org_id="ORG-001")
    rule = evaluator.add_rule(
        action="file.write",
        resource="file:/tmp/report.md",
        effect="approval_required",
    )

    result = evaluate_governance_policy(
        "file.write",
        {
            "global_config": _global_config(tmp_path),
            "resource": "file:/tmp/report.md",
            "actor_id": "usr-1",
            "project_id": "prj-finance",
            "tool_arguments": {"path": "/tmp/report.md", "content": "secret"},
        },
    )

    assert result.allowed is False
    assert result.decision == PolicyDecision.APPROVAL_REQUIRED
    assert result.rule_id == rule.id
    assert result.approval_request_id
    requests = evaluator.list_approval_requests(status="pending")
    assert len(requests) == 1
    request = requests[0]
    assert request.id == result.approval_request_id
    assert request.actor_id == "usr-1"
    assert request.action == "file.write"
    assert request.resource == "file:/tmp/report.md"
    assert request.status == "pending"
    assert request.rule_id == rule.id
    assert request.context["project_id"] == "prj-finance"
    assert "global_config" not in request.context
    event = _audit_events(tmp_path)[-1]
    assert event["event_type"] == "policy"
    assert event["action"] == "file.write"
    assert event["status"] == "approval_required"
    assert event["context"]["approval_request_id"] == result.approval_request_id
    assert event["context"]["decision"] == "approval_required"
    ledger = EnterpriseAuditLedger.from_path(tmp_path / "state" / "enterprise.sqlite", org_id="ORG-001")
    ledger_events = ledger.query(event_type="policy")
    assert len(ledger_events) == 1
    assert ledger_events[0].status == "approval_required"
    assert ledger_events[0].context["approval_request_id"] == result.approval_request_id


def test_policy_evaluator_decides_approval_request_and_writes_ledger(tmp_path):
    _init_org(tmp_path)
    evaluator = PolicyEvaluator.from_path(tmp_path / "state" / "enterprise.sqlite", org_id="ORG-001")
    request = evaluator.create_approval_request(
        action="file.write",
        resource="file:report.md",
        context={"actor_id": "usr-1", "project_id": "prj-finance", "task_id": "task-1"},
        rule_id="rule-1",
        reason="high-risk file write",
        request_id="appr-test",
    )

    decided = evaluator.decide_approval_request(
        request.id,
        status="approve",
        decided_by="admin-1",
        reason="approved for finance report",
    )

    assert decided.status == "approved"
    assert decided.decided_by == "admin-1"
    assert decided.decided_at is not None
    assert decided.decision_reason == "approved for finance report"
    assert evaluator.list_approval_requests(status="pending") == []
    assert evaluator.list_approval_requests(status="approved") == [decided]

    ledger = EnterpriseAuditLedger.from_path(tmp_path / "state" / "enterprise.sqlite", org_id="ORG-001")
    events = ledger.query(event_type="policy")
    assert len(events) == 1
    assert events[0].action == "approval.decide"
    assert events[0].status == "approved"
    assert events[0].actor_id == "admin-1"
    assert events[0].project_id == "prj-finance"
    assert events[0].task_id == "task-1"
    assert events[0].request_id == "appr-test"
    assert events[0].context["original_action"] == "file.write"


def test_policy_evaluator_rejects_double_decision(tmp_path):
    _init_org(tmp_path)
    evaluator = PolicyEvaluator.from_path(tmp_path / "state" / "enterprise.sqlite", org_id="ORG-001")
    request = evaluator.create_approval_request(
        action="backend.switch",
        resource="backend:grok-cli",
        request_id="appr-double",
    )
    evaluator.decide_approval_request(request.id, status="deny", decided_by="admin-1")

    with pytest.raises(ValueError, match="already decided"):
        evaluator.decide_approval_request(request.id, status="approve", decided_by="admin-2")


def test_personal_profile_policy_stays_allow_by_default(tmp_path):
    result = evaluate_governance_policy(
        "command.execute",
        {
            "global_config": _global_config(tmp_path, profile="personal", org_id=None),
            "resource": "command:sys",
            "command_name": "sys",
        },
    )

    assert result.allowed is True
    assert result.reason == "personal_profile"


def test_runtime_command_allowed_honors_enterprise_policy_deny(tmp_path):
    _init_org(tmp_path)
    evaluator = PolicyEvaluator.from_path(tmp_path / "state" / "enterprise.sqlite", org_id="ORG-001")
    evaluator.add_rule(action="command.execute", resource="command:backend", effect="deny")
    runtime = object.__new__(FlexibleAgentRuntime)
    runtime.name = "zelda"
    runtime.global_config = _global_config(tmp_path)
    runtime._disabled_commands = set()
    runtime._enabled_commands = set()
    runtime._command_policy_mode = "allow_all"

    assert FlexibleAgentRuntime._is_command_allowed(runtime, "backend") is False
    assert FlexibleAgentRuntime._is_command_allowed(runtime, "status") is True


@pytest.mark.asyncio
async def test_runtime_backend_switch_honors_enterprise_policy_deny(tmp_path):
    _init_org(tmp_path)
    evaluator = PolicyEvaluator.from_path(tmp_path / "state" / "enterprise.sqlite", org_id="ORG-001")
    evaluator.add_rule(action="backend.switch", resource="backend:claude-cli", effect="deny")
    runtime = object.__new__(FlexibleAgentRuntime)
    runtime.name = "zelda"
    runtime.global_config = _global_config(tmp_path)
    runtime.config = SimpleNamespace(allowed_backends=[{"engine": "claude-cli"}])

    ok, message = await FlexibleAgentRuntime._switch_backend_mode(runtime, 123, "claude-cli")

    assert ok is False
    assert message == "Backend switch blocked by policy: claude-cli"


@pytest.mark.asyncio
async def test_runtime_backend_switch_blocks_approval_required_policy(tmp_path):
    _init_org(tmp_path)
    evaluator = PolicyEvaluator.from_path(tmp_path / "state" / "enterprise.sqlite", org_id="ORG-001")
    evaluator.add_rule(action="backend.switch", resource="backend:claude-cli", effect="approval_required")
    runtime = object.__new__(FlexibleAgentRuntime)
    runtime.name = "zelda"
    runtime.global_config = _global_config(tmp_path)
    runtime.config = SimpleNamespace(allowed_backends=[{"engine": "claude-cli"}])

    ok, message = await FlexibleAgentRuntime._switch_backend_mode(runtime, 123, "claude-cli")

    assert ok is False
    assert message == "Backend switch requires approval: claude-cli"
