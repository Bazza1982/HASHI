from __future__ import annotations

from types import SimpleNamespace

from orchestrator.enterprise import IdentityService, PolicyDecision, PolicyEvaluator, evaluate_governance_policy
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
    runtime = SimpleNamespace(
        name="zelda",
        global_config=_global_config(tmp_path),
        _disabled_commands=set(),
        _enabled_commands=set(),
        _command_policy_mode="allow_all",
    )

    assert FlexibleAgentRuntime._is_command_allowed(runtime, "backend") is False
    assert FlexibleAgentRuntime._is_command_allowed(runtime, "status") is True
