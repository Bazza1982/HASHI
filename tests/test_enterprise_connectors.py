from __future__ import annotations

import pytest

from orchestrator.enterprise import (
    ConnectorAction,
    ConnectorCredentialStore,
    ConnectorExecutionService,
    ConnectorFactory,
    ConnectorHealth,
    ConnectorRegistry,
    ConnectorResult,
    EnterpriseAuditLedger,
    FeishuWebhookConnector,
    GitHubConnector,
    IdentityService,
    PolicyEvaluator,
    ConnectorSecretResolver,
    DataClassification,
    DataGovernancePolicy,
    SlackWebhookConnector,
    GoogleChatWebhookConnector,
    TeamsWebhookConnector,
)
from orchestrator.enterprise.connectors import evaluate_connector_action, record_connector_event, validate_connector_action


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
        if method == "POST" and path == "/repos/Bazza1982/hashi/issues":
            return {
                "id": 456,
                "number": 7,
                "title": body["title"],
                "html_url": "https://github.com/Bazza1982/hashi/issues/7",
                "state": "open",
            }
        if method == "POST" and path == "/repos/Bazza1982/hashi/pulls":
            return {
                "id": 789,
                "number": 8,
                "title": body["title"],
                "html_url": "https://github.com/Bazza1982/hashi/pull/8",
                "state": "open",
                "draft": body["draft"],
            }
        if method == "PUT" and path == "/repos/Bazza1982/hashi/pulls/8/merge":
            return {
                "sha": "abc123",
                "merged": True,
                "message": "Pull Request successfully merged",
            }
        raise AssertionError(f"unexpected GitHub request: {method} {path}")


class _FakeSlackTransport:
    def __init__(self):
        self.calls = []

    def __call__(self, webhook_url, payload):
        self.calls.append({"webhook_url": webhook_url, "payload": dict(payload)})
        return {"status_code": 200, "text": "ok"}


class _FakeGoogleChatTransport:
    def __init__(self):
        self.calls = []

    def __call__(self, webhook_url, payload):
        self.calls.append({"webhook_url": webhook_url, "payload": dict(payload)})
        return {"status_code": 200, "text": "ok"}


class _FakeTeamsTransport:
    def __init__(self):
        self.calls = []

    def __call__(self, webhook_url, payload):
        self.calls.append({"webhook_url": webhook_url, "payload": dict(payload)})
        return {"status_code": 200, "text": "1"}


class _FakeFeishuTransport:
    def __init__(self):
        self.calls = []

    def __call__(self, webhook_url, payload):
        self.calls.append({"webhook_url": webhook_url, "payload": dict(payload)})
        return {"status_code": 200, "code": 0}


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


def test_github_connector_issue_create_supports_dry_run_without_transport_call():
    transport = _FakeGitHubTransport()
    connector = GitHubConnector(transport=transport)
    action = ConnectorAction(
        connector_type="github",
        action="issue.create",
        resource="repo:Bazza1982/hashi",
        dry_run=True,
        parameters={"title": "Enterprise task", "body": "Details", "labels": ["enterprise"]},
    )

    result = connector.execute(action)

    assert result.ok is True
    assert result.status == "dry_run"
    assert result.data == {
        "owner": "Bazza1982",
        "repo": "hashi",
        "payload": {"title": "Enterprise task", "body": "Details", "labels": ["enterprise"]},
    }
    assert transport.calls == []


def test_github_connector_issue_create_posts_issue_payload():
    transport = _FakeGitHubTransport()
    connector = GitHubConnector(token="ghp-test", transport=transport)
    action = ConnectorAction(
        connector_type="github",
        action="issue.create",
        resource="repo:Bazza1982/hashi",
        parameters={"title": "Enterprise task", "body": "Details", "labels": ["enterprise"]},
    )

    result = connector.execute(action)

    assert result.ok is True
    assert result.status == "success"
    assert result.data["html_url"] == "https://github.com/Bazza1982/hashi/issues/7"
    assert transport.calls[0]["method"] == "POST"
    assert transport.calls[0]["body"] == {"title": "Enterprise task", "body": "Details", "labels": ["enterprise"]}
    assert transport.calls[0]["headers"]["Authorization"] == "Bearer ghp-test"


def test_github_connector_issue_create_requires_title():
    connector = GitHubConnector(transport=_FakeGitHubTransport())
    action = ConnectorAction(connector_type="github", action="issue.create", resource="repo:Bazza1982/hashi")

    result = connector.execute(action)

    assert result.ok is False
    assert result.status == "invalid_parameters"
    assert "title" in result.message


def test_github_connector_pr_create_supports_dry_run_without_transport_call():
    transport = _FakeGitHubTransport()
    connector = GitHubConnector(transport=transport)
    action = ConnectorAction(
        connector_type="github",
        action="pr.create",
        resource="repo:Bazza1982/hashi",
        dry_run=True,
        parameters={
            "title": "Enterprise PR",
            "head": "feature/aai",
            "base": "main",
            "body": "Details",
            "draft": True,
        },
    )

    result = connector.execute(action)

    assert result.ok is True
    assert result.status == "dry_run"
    assert result.data == {
        "owner": "Bazza1982",
        "repo": "hashi",
        "payload": {
            "title": "Enterprise PR",
            "head": "feature/aai",
            "base": "main",
            "body": "Details",
            "draft": True,
        },
    }
    assert transport.calls == []


def test_github_connector_pr_create_posts_pull_request_payload():
    transport = _FakeGitHubTransport()
    connector = GitHubConnector(token="ghp-test", transport=transport)
    action = ConnectorAction(
        connector_type="github",
        action="pr.create",
        resource="repo:Bazza1982/hashi",
        parameters={
            "title": "Enterprise PR",
            "head": "feature/aai",
            "base": "main",
            "body": "Details",
            "draft": True,
        },
    )

    result = connector.execute(action)

    assert result.ok is True
    assert result.status == "success"
    assert result.data["html_url"] == "https://github.com/Bazza1982/hashi/pull/8"
    assert result.data["draft"] is True
    assert transport.calls[0]["method"] == "POST"
    assert transport.calls[0]["path"] == "/repos/Bazza1982/hashi/pulls"
    assert transport.calls[0]["body"] == {
        "title": "Enterprise PR",
        "head": "feature/aai",
        "base": "main",
        "body": "Details",
        "draft": True,
    }
    assert transport.calls[0]["headers"]["Authorization"] == "Bearer ghp-test"


@pytest.mark.parametrize(
    ("parameters", "message"),
    [
        ({}, "title"),
        ({"title": "Enterprise PR"}, "head"),
        ({"title": "Enterprise PR", "head": "feature/aai"}, "base"),
    ],
)
def test_github_connector_pr_create_validates_required_fields(parameters, message):
    connector = GitHubConnector(transport=_FakeGitHubTransport())
    action = ConnectorAction(
        connector_type="github",
        action="pr.create",
        resource="repo:Bazza1982/hashi",
        parameters=parameters,
    )

    result = connector.execute(action)

    assert result.ok is False
    assert result.status == "invalid_parameters"
    assert message in result.message


def test_github_connector_pr_merge_supports_dry_run_without_transport_call():
    transport = _FakeGitHubTransport()
    connector = GitHubConnector(transport=transport)
    action = ConnectorAction(
        connector_type="github",
        action="pr.merge",
        resource="repo:Bazza1982/hashi",
        dry_run=True,
        parameters={
            "pull_number": 8,
            "merge_method": "squash",
            "commit_title": "Enterprise merge",
            "commit_message": "Reviewed by HASHI",
            "sha": "abc123",
        },
    )

    result = connector.execute(action)

    assert result.ok is True
    assert result.status == "dry_run"
    assert result.data == {
        "owner": "Bazza1982",
        "repo": "hashi",
        "pull_number": 8,
        "payload": {
            "merge_method": "squash",
            "commit_title": "Enterprise merge",
            "commit_message": "Reviewed by HASHI",
            "sha": "abc123",
        },
    }
    assert transport.calls == []


def test_github_connector_pr_merge_sends_merge_payload():
    transport = _FakeGitHubTransport()
    connector = GitHubConnector(token="ghp-test", transport=transport)
    action = ConnectorAction(
        connector_type="github",
        action="pr.merge",
        resource="repo:Bazza1982/hashi",
        parameters={
            "pull_number": 8,
            "merge_method": "squash",
            "commit_title": "Enterprise merge",
            "commit_message": "Reviewed by HASHI",
        },
    )

    result = connector.execute(action)

    assert result.ok is True
    assert result.status == "success"
    assert result.data["sha"] == "abc123"
    assert result.data["merged"] is True
    assert transport.calls[0]["method"] == "PUT"
    assert transport.calls[0]["path"] == "/repos/Bazza1982/hashi/pulls/8/merge"
    assert transport.calls[0]["body"] == {
        "merge_method": "squash",
        "commit_title": "Enterprise merge",
        "commit_message": "Reviewed by HASHI",
    }
    assert transport.calls[0]["headers"]["Authorization"] == "Bearer ghp-test"


@pytest.mark.parametrize(
    ("parameters", "message"),
    [
        ({}, "pull_number"),
        ({"pull_number": 0}, "pull_number"),
        ({"pull_number": 8, "merge_method": "force"}, "merge_method"),
    ],
)
def test_github_connector_pr_merge_validates_required_fields(parameters, message):
    connector = GitHubConnector(transport=_FakeGitHubTransport())
    action = ConnectorAction(
        connector_type="github",
        action="pr.merge",
        resource="repo:Bazza1982/hashi",
        parameters=parameters,
    )

    result = connector.execute(action)

    assert result.ok is False
    assert result.status == "invalid_parameters"
    assert message in result.message


def test_github_connector_rejects_unsupported_action():
    connector = GitHubConnector(transport=_FakeGitHubTransport())
    action = ConnectorAction(connector_type="github", action="release.create", resource="repo:Bazza1982/hashi")

    result = connector.execute(action)

    assert result.ok is False
    assert result.status == "unsupported_action"


def test_slack_webhook_connector_health_and_dry_run():
    connector = SlackWebhookConnector(webhook_url="https://hooks.slack.test/services/abc")
    action = ConnectorAction(
        connector_type="slack",
        action="message.send",
        dry_run=True,
        parameters={"text": "Hello enterprise"},
    )

    health = connector.health_check()
    result = connector.execute(action)

    assert health.ok is True
    assert result.ok is True
    assert result.status == "dry_run"
    assert result.data == {"payload": {"text": "Hello enterprise"}}


def test_slack_webhook_connector_posts_message_payload():
    transport = _FakeSlackTransport()
    connector = SlackWebhookConnector(webhook_url="https://hooks.slack.test/services/abc", transport=transport)
    action = ConnectorAction(
        connector_type="slack",
        action="message.send",
        parameters={"text": "Hello enterprise", "blocks": [{"type": "section"}]},
    )

    result = connector.execute(action)

    assert result.ok is True
    assert result.status == "success"
    assert result.data == {"status_code": 200, "text": "ok"}
    assert transport.calls == [
        {
            "webhook_url": "https://hooks.slack.test/services/abc",
            "payload": {"text": "Hello enterprise", "blocks": [{"type": "section"}]},
        }
    ]


def test_slack_webhook_connector_requires_text():
    connector = SlackWebhookConnector(webhook_url="https://hooks.slack.test/services/abc")
    action = ConnectorAction(connector_type="slack", action="message.send")

    result = connector.execute(action)

    assert result.ok is False
    assert result.status == "invalid_parameters"


def test_google_chat_webhook_connector_health_and_dry_run():
    connector = GoogleChatWebhookConnector(webhook_url="https://chat.googleapis.com/v1/spaces/abc/messages?key=test")
    action = ConnectorAction(
        connector_type="google_chat",
        action="message.send",
        dry_run=True,
        parameters={"text": "Hello enterprise"},
    )

    health = connector.health_check()
    result = connector.execute(action)

    assert health.ok is True
    assert result.ok is True
    assert result.status == "dry_run"
    assert result.data == {"payload": {"text": "Hello enterprise"}}


def test_google_chat_webhook_connector_posts_message_payload():
    transport = _FakeGoogleChatTransport()
    connector = GoogleChatWebhookConnector(
        webhook_url="https://chat.googleapis.com/v1/spaces/abc/messages?key=test",
        transport=transport,
    )
    action = ConnectorAction(
        connector_type="google_chat",
        action="message.send",
        parameters={"text": "Hello enterprise", "cards": [{"header": {"title": "HASHI"}}]},
    )

    result = connector.execute(action)

    assert result.ok is True
    assert result.status == "success"
    assert result.data == {"status_code": 200, "text": "ok"}
    assert transport.calls == [
        {
            "webhook_url": "https://chat.googleapis.com/v1/spaces/abc/messages?key=test",
            "payload": {"text": "Hello enterprise", "cards": [{"header": {"title": "HASHI"}}]},
        }
    ]


def test_google_chat_webhook_connector_requires_text():
    connector = GoogleChatWebhookConnector(webhook_url="https://chat.googleapis.com/v1/spaces/abc/messages?key=test")
    action = ConnectorAction(connector_type="google_chat", action="message.send")

    result = connector.execute(action)

    assert result.ok is False
    assert result.status == "invalid_parameters"


def test_teams_webhook_connector_health_and_dry_run():
    connector = TeamsWebhookConnector(webhook_url="https://outlook.office.com/webhook/abc")
    action = ConnectorAction(
        connector_type="teams",
        action="message.send",
        dry_run=True,
        parameters={"text": "Hello enterprise", "title": "HASHI"},
    )

    health = connector.health_check()
    result = connector.execute(action)

    assert health.ok is True
    assert result.ok is True
    assert result.status == "dry_run"
    assert result.data == {"payload": {"text": "Hello enterprise", "title": "HASHI"}}


def test_teams_webhook_connector_posts_message_payload():
    transport = _FakeTeamsTransport()
    connector = TeamsWebhookConnector(webhook_url="https://outlook.office.com/webhook/abc", transport=transport)
    action = ConnectorAction(
        connector_type="teams",
        action="message.send",
        parameters={"text": "Hello enterprise", "sections": [{"activityTitle": "HASHI"}]},
    )

    result = connector.execute(action)

    assert result.ok is True
    assert result.status == "success"
    assert result.data == {"status_code": 200, "text": "1"}
    assert transport.calls == [
        {
            "webhook_url": "https://outlook.office.com/webhook/abc",
            "payload": {"text": "Hello enterprise", "sections": [{"activityTitle": "HASHI"}]},
        }
    ]


def test_teams_webhook_connector_requires_text():
    connector = TeamsWebhookConnector(webhook_url="https://outlook.office.com/webhook/abc")
    action = ConnectorAction(connector_type="teams", action="message.send")

    result = connector.execute(action)

    assert result.ok is False
    assert result.status == "invalid_parameters"


def test_feishu_webhook_connector_health_and_dry_run():
    connector = FeishuWebhookConnector(webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/abc")
    action = ConnectorAction(
        connector_type="feishu",
        action="message.send",
        dry_run=True,
        parameters={"text": "Hello enterprise"},
    )

    health = connector.health_check()
    result = connector.execute(action)

    assert health.ok is True
    assert result.ok is True
    assert result.status == "dry_run"
    assert result.data == {"payload": {"msg_type": "text", "content": {"text": "Hello enterprise"}}}


def test_feishu_webhook_connector_posts_message_payload():
    transport = _FakeFeishuTransport()
    connector = FeishuWebhookConnector(
        webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/abc",
        transport=transport,
    )
    action = ConnectorAction(
        connector_type="feishu",
        action="message.send",
        parameters={"text": "Hello enterprise"},
    )

    result = connector.execute(action)

    assert result.ok is True
    assert result.status == "success"
    assert result.data == {"status_code": 200, "code": 0}
    assert transport.calls == [
        {
            "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/abc",
            "payload": {"msg_type": "text", "content": {"text": "Hello enterprise"}},
        }
    ]


def test_feishu_webhook_connector_requires_text():
    connector = FeishuWebhookConnector(webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/abc")
    action = ConnectorAction(connector_type="feishu", action="message.send")

    result = connector.execute(action)

    assert result.ok is False
    assert result.status == "invalid_parameters"


def test_connector_factory_builds_google_chat_connector_from_secret_ref(tmp_path):
    credentials, _, _ = _connector_gate_services(tmp_path)
    credential = credentials.create_credential(
        org_id="ORG-001",
        connector_type="google_chat",
        display_name="Google Chat Webhook",
        secret_ref="secrets://google_chat_webhook",
        scopes=["message.send"],
        credential_id="cred-google-chat",
    )
    transport = _FakeGoogleChatTransport()
    factory = ConnectorFactory(
        secret_resolver=ConnectorSecretResolver(
            secrets={"google_chat_webhook": "https://chat.googleapis.com/v1/spaces/abc/messages?key=test"}
        ),
        transports={"google_chat": transport},
    )

    connector = factory.build(credential)
    result = connector.execute(
        ConnectorAction(connector_type="google_chat", action="message.send", parameters={"text": "Hello"})
    )

    assert result.ok is True
    assert transport.calls[0]["webhook_url"] == "https://chat.googleapis.com/v1/spaces/abc/messages?key=test"


def test_validate_connector_action_requires_text_for_webhook_message_send():
    action = ConnectorAction(connector_type="feishu", action="message.send", parameters={})

    assert validate_connector_action(action) == "message.send requires non-empty text in parameters"


@pytest.mark.parametrize(
    ("parameters", "message"),
    [
        ({}, "github.pr.merge requires parameter pull_number"),
        ({"pull_number": "1"}, "pull_number must be integer"),
        ({"pull_number": 1, "merge_method": "fast-forward"}, "merge_method must be one of merge, squash, rebase"),
    ],
)
def test_validate_connector_action_uses_action_schema(parameters, message):
    action = ConnectorAction(connector_type="github", action="pr.merge", parameters=parameters)

    assert validate_connector_action(action) == message


def test_connector_factory_builds_github_connector_from_env_secret(tmp_path):
    credentials, _, credential = _connector_gate_services(tmp_path)
    credentials.revoke_credential(credential.id)
    credential = credentials.create_credential(
        org_id="ORG-001",
        connector_type="github",
        display_name="GitHub Env",
        secret_ref="env://GITHUB_TOKEN",
        scopes=["repo:read"],
        credential_id="cred-github-env",
    )
    transport = _FakeGitHubTransport()
    factory = ConnectorFactory(
        secret_resolver=ConnectorSecretResolver(environ={"GITHUB_TOKEN": "ghp-env"}),
        transports={"github": transport},
    )

    connector = factory.build(credential)
    health = connector.health_check()

    assert health.ok is True
    assert transport.calls[0]["headers"]["Authorization"] == "Bearer ghp-env"


def test_connector_factory_build_registry_skips_revoked_credentials(tmp_path):
    credentials, _, credential = _connector_gate_services(tmp_path)
    credentials.revoke_credential(credential.id)
    factory = ConnectorFactory(secret_resolver=ConnectorSecretResolver(secrets={"github_token": "ghp"}))

    registry = factory.build_registry(credentials.list_credentials(org_id="ORG-001", include_revoked=True))

    assert registry.list_types() == []


def test_connector_factory_fails_closed_for_unsupported_connector_type(tmp_path):
    credentials, _, _ = _connector_gate_services(tmp_path)
    credential = credentials.create_credential(
        org_id="ORG-001",
        connector_type="jira",
        display_name="Jira Bot",
        secret_ref="secrets://jira_token",
        scopes=["chat:write"],
        credential_id="cred-jira",
    )
    factory = ConnectorFactory(secret_resolver=ConnectorSecretResolver(secrets={"jira_token": "token"}))

    with pytest.raises(ValueError, match="unsupported connector type"):
        factory.build(credential)


def test_connector_factory_builds_slack_connector_from_secret_ref(tmp_path):
    credentials, _, _ = _connector_gate_services(tmp_path)
    credential = credentials.create_credential(
        org_id="ORG-001",
        connector_type="slack",
        display_name="Slack Bot",
        secret_ref="secrets://slack_webhook",
        scopes=["chat:write"],
        credential_id="cred-slack",
    )
    transport = _FakeSlackTransport()
    factory = ConnectorFactory(
        secret_resolver=ConnectorSecretResolver(secrets={"slack_webhook": "https://hooks.slack.test/services/abc"}),
        transports={"slack": transport},
    )

    connector = factory.build(credential)
    result = connector.execute(
        ConnectorAction(connector_type="slack", action="message.send", parameters={"text": "Hello"})
    )

    assert result.ok is True
    assert transport.calls[0]["webhook_url"] == "https://hooks.slack.test/services/abc"


def test_connector_factory_builds_teams_connector_from_secret_ref(tmp_path):
    credentials, _, _ = _connector_gate_services(tmp_path)
    credential = credentials.create_credential(
        org_id="ORG-001",
        connector_type="teams",
        display_name="Teams Webhook",
        secret_ref="secrets://teams_webhook",
        scopes=["message.send"],
        credential_id="cred-teams",
    )
    transport = _FakeTeamsTransport()
    factory = ConnectorFactory(
        secret_resolver=ConnectorSecretResolver(secrets={"teams_webhook": "https://outlook.office.com/webhook/abc"}),
        transports={"teams": transport},
    )

    connector = factory.build(credential)
    result = connector.execute(
        ConnectorAction(connector_type="teams", action="message.send", parameters={"text": "Hello"})
    )

    assert result.ok is True
    assert transport.calls[0]["webhook_url"] == "https://outlook.office.com/webhook/abc"


def test_connector_factory_builds_feishu_connector_from_secret_ref(tmp_path):
    credentials, _, _ = _connector_gate_services(tmp_path)
    credential = credentials.create_credential(
        org_id="ORG-001",
        connector_type="feishu",
        display_name="Feishu Webhook",
        secret_ref="secrets://feishu_webhook",
        scopes=["message.send"],
        credential_id="cred-feishu",
    )
    transport = _FakeFeishuTransport()
    factory = ConnectorFactory(
        secret_resolver=ConnectorSecretResolver(
            secrets={"feishu_webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/abc"}
        ),
        transports={"feishu": transport},
    )

    connector = factory.build(credential)
    result = connector.execute(
        ConnectorAction(connector_type="feishu", action="message.send", parameters={"text": "Hello"})
    )

    assert result.ok is True
    assert transport.calls[0]["webhook_url"] == "https://open.feishu.cn/open-apis/bot/v2/hook/abc"


def test_connector_factory_fails_closed_when_secret_ref_cannot_resolve(tmp_path):
    _, _, credential = _connector_gate_services(tmp_path)
    factory = ConnectorFactory(secret_resolver=ConnectorSecretResolver())

    with pytest.raises(ValueError, match="vault secret resolver is not configured"):
        factory.build(credential)


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


def _connector_execution_services(tmp_path, *, connector=None):
    credentials, policy, _ = _connector_gate_services(tmp_path)
    ledger = EnterpriseAuditLedger.from_path(tmp_path / "enterprise.sqlite", org_id="ORG-001")
    registry = ConnectorRegistry([connector or _FakeConnector()])
    service = ConnectorExecutionService(
        registry=registry,
        credential_store=credentials,
        policy_evaluator=policy,
        ledger=ledger,
    )
    return service, credentials, policy, ledger


def _slack_execution_services(tmp_path, *, connector=None, data_governance_policy=None):
    db_path = tmp_path / "enterprise.sqlite"
    IdentityService.from_path(db_path).create_organization(org_id="ORG-001", name="Acme")
    credentials = ConnectorCredentialStore.from_path(db_path)
    policy = PolicyEvaluator.from_path(db_path, org_id="ORG-001")
    credentials.create_credential(
        org_id="ORG-001",
        connector_type="slack",
        display_name="Slack Webhook",
        secret_ref="env://SLACK_WEBHOOK_URL",
        scopes=["message.send"],
        credential_id="cred-slack",
    )
    ledger = EnterpriseAuditLedger.from_path(db_path, org_id="ORG-001")
    service = ConnectorExecutionService(
        registry=ConnectorRegistry([connector or SlackWebhookConnector("https://hooks.slack.test/services/abc")]),
        credential_store=credentials,
        policy_evaluator=policy,
        ledger=ledger,
        data_governance_policy=data_governance_policy,
    )
    return service, credentials, policy, ledger


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


def test_connector_execution_service_runs_allowed_action_and_records_audit(tmp_path):
    service, _, _, ledger = _connector_execution_services(tmp_path)
    action = ConnectorAction(
        connector_type="github",
        action="repo.read",
        resource="repo:Bazza1982/hashi",
        actor_id="usr-1",
        project_id="prj-research",
        parameters={"token": "should-redact"},
    )

    execution = service.execute(action, credential_id="cred-github")

    assert execution.gate.allowed is True
    assert execution.result.ok is True
    assert execution.result.status == "success"
    events = ledger.query(event_type="connector")
    assert len(events) == 1
    assert events[0].action == "github.repo.read"
    assert events[0].context["credential_id"] == "cred-github"
    assert events[0].context["gate_reason"] == "allowed"
    assert events[0].context["parameters"]["token"] == "[REDACTED]"


def test_connector_execution_service_blocks_policy_deny_without_calling_connector(tmp_path):
    class FailingConnector(_FakeConnector):
        def execute(self, action: ConnectorAction):
            raise AssertionError("connector should not execute")

    service, _, policy, ledger = _connector_execution_services(tmp_path, connector=FailingConnector())
    policy.add_rule(
        action="connector.execute",
        resource="connector:github:repo.read",
        effect="deny",
        rule_id="pol-deny-repo-read",
    )
    action = ConnectorAction(connector_type="github", action="repo.read")

    execution = service.execute(action, credential_id="cred-github")

    assert execution.gate.allowed is False
    assert execution.result.ok is False
    assert execution.result.status == "connector_action_denied"
    events = ledger.query(event_type="connector")
    assert len(events) == 1
    assert events[0].status == "connector_action_denied"
    assert events[0].context["policy_rule_id"] == "pol-deny-repo-read"


def test_connector_execution_service_records_approval_required_without_calling_connector(tmp_path):
    class FailingConnector(_FakeConnector):
        def execute(self, action: ConnectorAction):
            raise AssertionError("connector should not execute")

    service, _, policy, ledger = _connector_execution_services(tmp_path, connector=FailingConnector())
    policy.add_rule(
        action="connector.execute",
        resource="connector:github:repo.read",
        effect="approval_required",
        rule_id="pol-approval-repo-read",
    )
    action = ConnectorAction(connector_type="github", action="repo.read", actor_id="usr-1")

    execution = service.execute(action, credential_id="cred-github")

    assert execution.gate.allowed is False
    assert execution.result.status == "connector_action_requires_approval"
    assert execution.gate.approval_request_id
    events = ledger.query(event_type="connector")
    assert len(events) == 1
    assert events[0].context["approval_request_id"] == execution.gate.approval_request_id


def test_connector_execution_service_requires_data_egress_approval_without_calling_connector(tmp_path):
    class FailingSlack(SlackWebhookConnector):
        def execute(self, action: ConnectorAction):
            raise AssertionError("connector should not execute")

    service, _, policy, ledger = _slack_execution_services(
        tmp_path,
        connector=FailingSlack(webhook_url="https://hooks.slack.test/services/abc"),
    )
    action = ConnectorAction(
        connector_type="slack",
        action="message.send",
        actor_id="usr-1",
        project_id="prj-research",
        parameters={"text": "Contact alice@example.com"},
    )

    execution = service.execute(action, credential_id="cred-slack")

    assert execution.gate.allowed is True
    assert execution.result.ok is False
    assert execution.result.status == "data_egress_requires_approval"
    assert execution.data_governance.classification == DataClassification.CONFIDENTIAL
    approval = policy.list_approval_requests(status="pending")[0]
    assert approval.action == "data.egress"
    assert approval.context["data_governance"]["classification"] == "confidential"
    assert "alice@example.com" not in str(approval.context)
    events = ledger.query(event_type="connector")
    assert events[0].status == "data_egress_requires_approval"
    assert events[0].context["parameters"]["text"] == "[REDACTED_TEXT]"
    assert "alice@example.com" not in str(events[0].context)


def test_connector_execution_service_denies_restricted_data_egress_without_approval(tmp_path):
    class FailingSlack(SlackWebhookConnector):
        def execute(self, action: ConnectorAction):
            raise AssertionError("connector should not execute")

    service, _, policy, ledger = _slack_execution_services(
        tmp_path,
        connector=FailingSlack(webhook_url="https://hooks.slack.test/services/abc"),
    )
    action = ConnectorAction(
        connector_type="slack",
        action="message.send",
        parameters={"text": "token=super-secret-token"},
    )

    execution = service.execute(action, credential_id="cred-slack")

    assert execution.result.ok is False
    assert execution.result.status == "data_egress_denied"
    assert policy.list_approval_requests() == []
    events = ledger.query(event_type="connector")
    assert events[0].context["data_governance"]["classification"] == "restricted"
    assert events[0].context["parameters"]["text"] == "[REDACTED_TEXT]"
    assert "super-secret-token" not in str(events[0].context)


def test_connector_execution_service_records_redacted_data_governance_for_allowed_message(tmp_path):
    transport = _FakeSlackTransport()
    service, _, _, ledger = _slack_execution_services(
        tmp_path,
        connector=SlackWebhookConnector(webhook_url="https://hooks.slack.test/services/abc", transport=transport),
        data_governance_policy=DataGovernancePolicy(max_auto_egress=DataClassification.CONFIDENTIAL),
    )
    action = ConnectorAction(
        connector_type="slack",
        action="message.send",
        parameters={"text": "Contact alice@example.com"},
    )

    execution = service.execute(action, credential_id="cred-slack")

    assert execution.result.ok is True
    assert transport.calls
    events = ledger.query(event_type="connector")
    assert events[0].context["data_governance"]["decision"] == "allow"
    assert events[0].context["parameters"]["text"] == "[REDACTED_TEXT]"
    assert events[0].context["data"]["text"] == "[REDACTED_TEXT]"
    assert "alice@example.com" not in str(events[0].context)


def test_connector_execution_service_fails_closed_when_connector_not_registered(tmp_path):
    credentials, policy, _ = _connector_gate_services(tmp_path)
    ledger = EnterpriseAuditLedger.from_path(tmp_path / "enterprise.sqlite", org_id="ORG-001")
    service = ConnectorExecutionService(
        registry=ConnectorRegistry(),
        credential_store=credentials,
        policy_evaluator=policy,
        ledger=ledger,
    )

    execution = service.execute(ConnectorAction(connector_type="github", action="repo.read"), credential_id="cred-github")

    assert execution.gate.allowed is True
    assert execution.result.ok is False
    assert execution.result.status == "connector_not_registered"
