from __future__ import annotations

from orchestrator.enterprise import (
    ConnectorAction,
    ConnectorCredentialStore,
    ConnectorHealth,
    ConnectorRegistry,
    ConnectorResult,
    EnterpriseAuditLedger,
    GitHubConnector,
    IdentityService,
    PolicyEvaluator,
)
from orchestrator.enterprise.connectors import evaluate_connector_action, record_connector_event


class _FakeConnector:
    connector_type = "github"

    def health_check(self):
        return ConnectorHealth(ok=True, status="healthy", message="ready")

    def execute(self, action: ConnectorAction):
        return ConnectorResult(ok=True, status="success", message=f"executed {action.action}", data={"id": 123})


class _FakeGitHubTransport:
    def __init__(self):
        self.calls = []

    def __call__(self, method, path, headers, body):
        self.calls.append({"method": method, "path": path, "headers": dict(headers), "body": body})
        if path == "/rate_limit":
            return {"rate": {"remaining": 42, "limit": 5000}}
        if path == "/repos/Bazza1982/hashi":
            return {
                "id": 123,
                "full_name": "Bazza1982/hashi",
                "private": False,
                "default_branch": "main",
                "html_url": "https://github.com/Bazza1982/hashi",
            }
        raise AssertionError(f"unexpected GitHub request: {method} {path}")


def test_connector_interface_can_execute_and_report_health():
    connector = _FakeConnector()
    action = ConnectorAction(connector_type="github", action="repo.read", resource="repo:hashi")

    assert connector.health_check().status == "healthy"
    assert connector.execute(action).data == {"id": 123}


def test_github_connector_health_uses_rate_limit_and_auth_header():
    transport = _FakeGitHubTransport()
    connector = GitHubConnector(token="ghp-test", transport=transport)

    health = connector.health_check()

    assert health.ok is True
    assert health.status == "healthy"
    assert health.data["rate"]["remaining"] == 42
    assert transport.calls[0]["path"] == "/rate_limit"
    assert transport.calls[0]["headers"]["Authorization"] == "Bearer ghp-test"


def test_github_connector_repo_get_from_resource_returns_repository_metadata():
    connector = GitHubConnector(transport=_FakeGitHubTransport())
    action = ConnectorAction(connector_type="github", action="repo.get", resource="repo:Bazza1982/hashi")

    result = connector.execute(action)

    assert result.ok is True
    assert result.status == "success"
    assert result.data == {
        "id": 123,
        "full_name": "Bazza1982/hashi",
        "private": False,
        "default_branch": "main",
        "html_url": "https://github.com/Bazza1982/hashi",
    }


def test_github_connector_repo_get_from_parameters_supports_dry_run():
    connector = GitHubConnector(transport=_FakeGitHubTransport())
    action = ConnectorAction(
        connector_type="github",
        action="repo.read",
        dry_run=True,
        parameters={"owner": "Bazza1982", "repo": "hashi"},
    )

    result = connector.execute(action)

    assert result.ok is True
    assert result.status == "dry_run"
    assert result.data == {"owner": "Bazza1982", "repo": "hashi"}


def test_github_connector_rejects_unsupported_action():
    connector = GitHubConnector(transport=_FakeGitHubTransport())
    action = ConnectorAction(connector_type="github", action="pr.create", resource="repo:Bazza1982/hashi")

    result = connector.execute(action)

    assert result.ok is False
    assert result.status == "unsupported_action"


def test_connector_registry_reports_health_and_records_ledger_event(tmp_path):
    IdentityService.from_path(tmp_path / "enterprise.sqlite").create_organization(org_id="ORG-001", name="Acme")
    ledger = EnterpriseAuditLedger.from_path(tmp_path / "enterprise.sqlite", org_id="ORG-001")
    registry = ConnectorRegistry([_FakeConnector()])

    summaries = registry.health_checks(ledger=ledger)

    assert [summary.connector_type for summary in summaries] == ["github"]
    assert summaries[0].ok is True
    assert summaries[0].status == "healthy"
    events = ledger.query(event_type="connector")
    assert len(events) == 1
    assert events[0].action == "github.health_check"


def test_connector_registry_converts_health_exceptions_to_unhealthy():
    class BrokenConnector:
        connector_type = "github"

        def health_check(self):
            raise RuntimeError("offline")

        def execute(self, action: ConnectorAction):
            raise AssertionError("not used")

    registry = ConnectorRegistry([BrokenConnector()])

    summaries = registry.health_checks()

    assert summaries[0].ok is False
    assert summaries[0].status == "unhealthy"
    assert summaries[0].message == "offline"


def test_record_connector_event_writes_canonical_ledger_event_and_redacts_parameters(tmp_path):
    IdentityService.from_path(tmp_path / "enterprise.sqlite").create_organization(org_id="ORG-001", name="Acme")
    ledger = EnterpriseAuditLedger.from_path(tmp_path / "enterprise.sqlite", org_id="ORG-001")
    action = ConnectorAction(
        connector_type="github",
        action="pr.create",
        resource="repo:hashi",
        actor_id="usr-1",
        project_id="prj-research",
        task_id="task-1",
        request_id="req-1",
        correlation_id="corr-1",
        parameters={"title": "Add feature", "token": "secret-token"},
    )
    result = ConnectorResult(ok=True, status="success", message="created", data={"url": "https://example.test/pr/1"})

    event = record_connector_event(ledger, action, result, credential_id="cred-github")

    assert event.event_type == "connector"
    assert event.action == "github.pr.create"
    assert event.status == "success"
    assert event.actor_id == "usr-1"
    assert event.project_id == "prj-research"
    assert event.task_id == "task-1"
    assert event.request_id == "req-1"
    assert event.correlation_id == "corr-1"
    assert event.context["connector_type"] == "github"
    assert event.context["credential_id"] == "cred-github"
    assert event.context["parameters"]["title"] == "Add feature"
    assert event.context["parameters"]["token"] == "[REDACTED]"


def _connector_gate_services(tmp_path):
    db_path = tmp_path / "enterprise.sqlite"
    IdentityService.from_path(db_path).create_organization(org_id="ORG-001", name="Acme")
    credentials = ConnectorCredentialStore.from_path(db_path)
    policy = PolicyEvaluator.from_path(db_path, org_id="ORG-001")
    credential = credentials.create_credential(
        org_id="ORG-001",
        connector_type="github",
        display_name="GitHub App",
        secret_ref="vault://github/app",
        scopes=["repo:read", "repo:write"],
        credential_id="cred-github",
    )
    return credentials, policy, credential


def test_evaluate_connector_action_allows_active_credential_without_policy_rule(tmp_path):
    credentials, policy, _ = _connector_gate_services(tmp_path)
    action = ConnectorAction(connector_type="github", action="repo.read", actor_id="usr-1")

    result = evaluate_connector_action(
        policy_evaluator=policy,
        credential_store=credentials,
        action=action,
        credential_id="cred-github",
    )

    assert result.allowed is True
    assert result.reason == "allowed"
    assert result.credential_id == "cred-github"


def test_evaluate_connector_action_denies_revoked_credential(tmp_path):
    credentials, policy, _ = _connector_gate_services(tmp_path)
    credentials.revoke_credential("cred-github")
    action = ConnectorAction(connector_type="github", action="repo.read")

    result = evaluate_connector_action(
        policy_evaluator=policy,
        credential_store=credentials,
        action=action,
        credential_id="cred-github",
    )

    assert result.allowed is False
    assert result.reason == "connector_credential_revoked"


def test_evaluate_connector_action_denies_cross_org_credential(tmp_path):
    credentials, _, _ = _connector_gate_services(tmp_path)
    IdentityService.from_path(tmp_path / "enterprise.sqlite").create_organization(org_id="ORG-002", name="Other")
    other_policy = PolicyEvaluator.from_path(tmp_path / "enterprise.sqlite", org_id="ORG-002")
    action = ConnectorAction(connector_type="github", action="repo.read")

    result = evaluate_connector_action(
        policy_evaluator=other_policy,
        credential_store=credentials,
        action=action,
        credential_id="cred-github",
    )

    assert result.allowed is False
    assert result.reason == "connector_credential_org_mismatch"


def test_evaluate_connector_action_honors_policy_deny(tmp_path):
    credentials, policy, _ = _connector_gate_services(tmp_path)
    rule = policy.add_rule(
        action="connector.execute",
        resource="connector:github:pr.create",
        effect="deny",
        conditions={"connector_action": "pr.create"},
        rule_id="pol-deny-pr",
    )
    action = ConnectorAction(connector_type="github", action="pr.create", actor_id="usr-1")

    result = evaluate_connector_action(
        policy_evaluator=policy,
        credential_store=credentials,
        action=action,
        credential_id="cred-github",
    )

    assert result.allowed is False
    assert result.reason == "connector_action_denied"
    assert result.policy_rule_id == rule.id


def test_evaluate_connector_action_creates_approval_request(tmp_path):
    credentials, policy, _ = _connector_gate_services(tmp_path)
    rule = policy.add_rule(
        action="connector.execute",
        resource="connector:github:pr.merge",
        effect="approval_required",
        rule_id="pol-approve-merge",
    )
    action = ConnectorAction(
        connector_type="github",
        action="pr.merge",
        actor_id="usr-1",
        project_id="prj-research",
        task_id="task-1",
    )

    result = evaluate_connector_action(
        policy_evaluator=policy,
        credential_store=credentials,
        action=action,
        credential_id="cred-github",
    )

    assert result.allowed is False
    assert result.reason == "connector_action_requires_approval"
    assert result.policy_rule_id == rule.id
    assert result.approval_request_id
    approval = policy.get_approval_request(result.approval_request_id)
    assert approval.action == "connector.execute"
    assert approval.context["connector_type"] == "github"
    assert approval.context["project_id"] == "prj-research"
